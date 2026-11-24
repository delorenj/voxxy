"""Self-hosted license keys (``VS1``): offline Ed25519 verification, never a phone-home.

Format, after Earshot's ``ES1`` (pricing doc 7.2)::

    VS1.<base64url(payload JSON)>.<base64url(Ed25519 signature over DOMAIN + payload)>

    payload = {"v": 1, "kid": "...", "license_id": "<uuid4>", "product": "voxxy-selfhost",
               "entitlement": "selfhost-12m", "issued_at": <unix s>, "updates_until": <unix s>,
               "email_hash": "<sha256 hex of the buyer's normalised email>"}

The license gates the bundle's updates, not the model: soft enforcement. Voxxy always
starts and always speaks; the status (valid, missing, invalid, or valid but this release
came out after the buyer's updates window) is shown in ``/app-config``, ``GET /license``
and ``voxxy license show``, and logged at start.

The license lives in one file on the data volume (``VOX_LICENSE_FILE``, default
``/data/license/license.txt``); ``POST /license`` replaces it after verifying it.
The public keys a license may be signed with are :data:`TRUSTED_PUBLIC_KEYS`. It is
empty until the signing key exists, so every license reads ``invalid`` until then.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

PREFIX = "VS1"
DOMAIN = b"voxxy-license-v1\n"
PRODUCT = "voxxy-selfhost"
ENTITLEMENTS = {"selfhost-12m"}
MAX_TOKEN_BYTES = 4096
FIELDS = {"v", "kid", "license_id", "product", "entitlement", "issued_at", "updates_until", "email_hash"}

# kid -> base64url raw Ed25519 public key. Public values only; the private key stays in the vault.
# Empty until the Voxxy signing key is created (an operator step), so no license verifies yet.
TRUSTED_PUBLIC_KEYS: dict[str, str] = {}

DEFAULT_LICENSE_FILE = "/data/license/license.txt"
DEFAULT_RELEASE_FILE = Path(__file__).resolve().parent.parent / "RELEASE.json"

STATE_VALID = "valid"
STATE_UPDATES_ENDED = "updates_ended"  # valid, but this release came out after updates_until
STATE_MISSING = "missing"
STATE_INVALID = "invalid"


@dataclass(frozen=True)
class LicenseStatus:
    state: str
    detail: str
    license_id: Optional[str] = None
    updates_until: Optional[int] = None
    issued_at: Optional[int] = None

    def as_dict(self) -> dict:
        return asdict(self)


def _unb64(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", value):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def release_time(path: Path = DEFAULT_RELEASE_FILE) -> Optional[int]:
    """When this build was released (``RELEASE.json`` written by the bundle build); None outside a bundle."""
    try:
        value = json.loads(path.read_text()).get("released_at")
    except (OSError, ValueError):
        return None
    return value if isinstance(value, int) else None


def verify(token: str, *, keys: Optional[dict[str, str]] = None, released_at: Optional[int] = None) -> LicenseStatus:
    """Check a VS1 token offline. Never raises; a bad token is ``invalid`` with a plain reason."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    keys = TRUSTED_PUBLIC_KEYS if keys is None else keys
    token = (token or "").strip()
    if not token:
        return LicenseStatus(STATE_MISSING, "No license key has been added.")
    try:
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES:
            raise ValueError("too long")
        prefix, encoded, signature_b64 = token.split(".")
        if prefix != PREFIX:
            raise ValueError("not a Voxxy license key")
        raw, signature = _unb64(encoded), _unb64(signature_b64)
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not (isinstance(payload, dict) and set(payload) == FIELDS and type(payload["v"]) is int and payload["v"] == 1
                and payload["product"] == PRODUCT and payload["entitlement"] in ENTITLEMENTS
                and type(payload["issued_at"]) is int and type(payload["updates_until"]) is int
                and 0 <= payload["issued_at"] <= payload["updates_until"] <= 253402300799
                and isinstance(payload["license_id"], str) and isinstance(payload["kid"], str)
                and isinstance(payload["email_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", payload["email_hash"])
                and len(signature) == 64):
            raise ValueError("not a Voxxy license key")
        public = keys.get(payload["kid"])
        if public is None:
            raise ValueError("signed with a key this version of Voxxy does not know")
        Ed25519PublicKey.from_public_bytes(_unb64(public)).verify(signature, DOMAIN + raw)
    except InvalidSignature:
        return LicenseStatus(STATE_INVALID, "This license key's signature does not check out.")
    except (ValueError, UnicodeError, TypeError, binascii.Error) as exc:
        reason = str(exc) if "Voxxy" in str(exc) or "signed with" in str(exc) else "not a Voxxy license key"
        return LicenseStatus(STATE_INVALID, f"This license key is not valid: {reason}.")
    common = {"license_id": payload["license_id"], "updates_until": payload["updates_until"],
              "issued_at": payload["issued_at"]}
    if released_at is not None and released_at > payload["updates_until"]:
        return LicenseStatus(STATE_UPDATES_ENDED,
                             "Your license is valid, but this version came out after your 12 months of updates "
                             "ended. It keeps working; renew to keep getting new versions.", **common)
    return LicenseStatus(STATE_VALID, "Licensed.", **common)


def license_path() -> Path:
    return Path(os.environ.get("VOX_LICENSE_FILE") or DEFAULT_LICENSE_FILE)


def current(path: Optional[Path] = None, *, keys: Optional[dict[str, str]] = None) -> LicenseStatus:
    path = path or license_path()
    try:
        token = path.read_text()
    except FileNotFoundError:
        token = ""
    except OSError as exc:
        return LicenseStatus(STATE_INVALID, f"The license file could not be read ({exc.strerror}).")
    return verify(token, keys=keys, released_at=release_time())


def store(token: str, path: Optional[Path] = None, *, keys: Optional[dict[str, str]] = None) -> LicenseStatus:
    """Verify ``token`` and, only if it is valid (or valid with updates ended), write it as the license."""
    status = verify(token, keys=keys, released_at=release_time())
    if status.state in (STATE_VALID, STATE_UPDATES_ENDED):
        path = path or license_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}{int(time.time() * 1000)}")
        tmp.write_text(token.strip() + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    return status
