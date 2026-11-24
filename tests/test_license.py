"""Self-hosted VS1 license keys (app/license.py) and the /license routes.

Every token here is signed by a throwaway Ed25519 key made for the test; the real
signing key never appears in the repo.
"""

from __future__ import annotations

import base64
import json
import stat
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from app import license as lic
from app import main

KID = "test-1"
NOW = 1_800_000_000
YEAR = 365 * 86400


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def signer():
    key = Ed25519PrivateKey.generate()
    public = _b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return key, {KID: public}


def payload(**over) -> dict:
    body = {"v": 1, "kid": KID, "license_id": str(uuid.uuid4()), "product": lic.PRODUCT,
            "entitlement": "selfhost-12m", "issued_at": NOW, "updates_until": NOW + YEAR,
            "email_hash": "a" * 64}
    body.update(over)
    return body


def token(key: Ed25519PrivateKey, body: dict, *, raw: bytes | None = None) -> str:
    raw = raw if raw is not None else json.dumps(body, separators=(",", ":")).encode()
    return f"VS1.{_b64(raw)}.{_b64(key.sign(lic.DOMAIN + raw))}"


# ------------------------------------------------------------------ verify


def test_valid_token(signer):
    key, keys = signer
    body = payload()
    status = lic.verify(token(key, body), keys=keys, released_at=NOW + 10)
    assert status.state == lic.STATE_VALID
    assert status.license_id == body["license_id"] and status.updates_until == NOW + YEAR


def test_release_after_the_updates_window_still_works(signer):
    key, keys = signer
    status = lic.verify(token(key, payload()), keys=keys, released_at=NOW + YEAR + 1)
    assert status.state == lic.STATE_UPDATES_ENDED
    assert "keeps working" in status.detail


def test_no_release_stamp_means_no_window_check(signer):
    key, keys = signer
    old = payload(issued_at=0, updates_until=1)
    assert lic.verify(token(key, old), keys=keys).state == lic.STATE_VALID


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_is_missing(value, signer):
    assert lic.verify(value, keys=signer[1]).state == lic.STATE_MISSING


def test_unknown_signing_key(signer):
    key, keys = signer
    status = lic.verify(token(key, payload(kid="other")), keys=keys)
    assert status.state == lic.STATE_INVALID and "does not know" in status.detail


def test_no_trusted_keys_yet_means_invalid(signer):
    key, _ = signer
    assert lic.verify(token(key, payload()), keys={}).state == lic.STATE_INVALID


def test_signature_from_another_key(signer):
    _, keys = signer
    stranger = Ed25519PrivateKey.generate()
    status = lic.verify(token(stranger, payload()), keys=keys)
    assert status.state == lic.STATE_INVALID and "signature" in status.detail


def test_tampered_payload(signer):
    key, keys = signer
    good = token(key, payload())
    prefix, _, sig = good.split(".")
    forged = json.dumps(payload(updates_until=NOW + 100 * YEAR), separators=(",", ":")).encode()
    assert lic.verify(f"{prefix}.{_b64(forged)}.{sig}", keys=keys).state == lic.STATE_INVALID


def test_signature_without_the_domain_prefix_is_refused(signer):
    key, keys = signer
    raw = json.dumps(payload(), separators=(",", ":")).encode()
    bare = f"VS1.{_b64(raw)}.{_b64(key.sign(raw))}"
    assert lic.verify(bare, keys=keys).state == lic.STATE_INVALID


@pytest.mark.parametrize("over", [
    {"product": "earshot"},
    {"entitlement": "lifetime"},
    {"v": 2},
    {"v": True},
    {"issued_at": NOW + 10, "updates_until": NOW},
    {"issued_at": "1800000000"},
    {"email_hash": "not-a-hash"},
    {"extra": 1},
])
def test_wrong_fields(signer, over):
    key, keys = signer
    status = lic.verify(token(key, payload(**over)), keys=keys)
    assert status.state == lic.STATE_INVALID and "not a Voxxy license key" in status.detail


def test_missing_field(signer):
    key, keys = signer
    body = payload()
    del body["email_hash"]
    assert lic.verify(token(key, body), keys=keys).state == lic.STATE_INVALID


def test_duplicate_json_keys_are_refused(signer):
    key, keys = signer
    body = payload()
    raw = json.dumps(body, separators=(",", ":"))[:-1] + ',"product":"' + lic.PRODUCT + '"}'
    assert lic.verify(token(key, body, raw=raw.encode()), keys=keys).state == lic.STATE_INVALID


@pytest.mark.parametrize("bad", ["ES1.abc.def", "VS1.abc", "VS1.a.b.c", "VS1.!!!.###", "VS1." + "a" * 5000 + ".b",
                                 "hello world"])
def test_garbage_never_raises(signer, bad):
    assert lic.verify(bad, keys=signer[1]).state == lic.STATE_INVALID


# ------------------------------------------------------------------ file + release stamp


def test_release_time_reads_the_stamp(tmp_path):
    p = tmp_path / "RELEASE.json"
    assert lic.release_time(p) is None
    p.write_text(json.dumps({"version": "0.1.0", "released_at": NOW}))
    assert lic.release_time(p) == NOW
    p.write_text("{not json")
    assert lic.release_time(p) is None


def test_store_writes_only_a_valid_key(signer, tmp_path):
    key, keys = signer
    path = tmp_path / "lic" / "license.txt"
    assert lic.store("VS1.nope.nope", path, keys=keys).state == lic.STATE_INVALID
    assert not path.exists()
    good = token(key, payload())
    assert lic.store(good, path, keys=keys).state == lic.STATE_VALID
    assert path.read_text().strip() == good
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert lic.current(path, keys=keys).state == lic.STATE_VALID
    assert list(path.parent.iterdir()) == [path]  # no temp file left behind


def test_current_without_a_file_is_missing(tmp_path, signer):
    assert lic.current(tmp_path / "none.txt", keys=signer[1]).state == lic.STATE_MISSING


# ------------------------------------------------------------------ routes


@pytest.fixture()
def client(monkeypatch, tmp_path, signer):
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    monkeypatch.setenv("VOX_LICENSE_FILE", str(tmp_path / "license" / "license.txt"))
    monkeypatch.setattr(lic, "TRUSTED_PUBLIC_KEYS", signer[1])
    return TestClient(main.app)


def test_self_edition_starts_missing_then_imports(client, monkeypatch, signer, tmp_path):
    monkeypatch.setenv("VOX_EDITION", "self")
    assert client.get("/license").json()["state"] == "missing"
    assert client.get("/app-config").json()["license"]["state"] == "missing"

    bad = client.post("/license", json={"key": "VS1.nope.nope"})
    assert bad.status_code == 422 and bad.headers["x-vox-error"] == "license_invalid"
    assert not (tmp_path / "license" / "license.txt").exists()

    good = token(signer[0], payload())
    ok = client.post("/license", json={"key": good})
    assert ok.status_code == 200 and ok.json()["state"] == "valid"
    shown = client.get("/app-config").json()["license"]
    assert shown == {"state": "valid", "detail": "Licensed.", "updates_until": NOW + YEAR}
    assert good not in json.dumps(client.get("/app-config").json())  # the key itself is never public


def test_hosted_edition_has_no_license(client, monkeypatch, signer):
    monkeypatch.delenv("VOX_EDITION", raising=False)
    assert client.get("/app-config").json()["license"] is None
    assert client.get("/license").status_code == 404
    assert client.post("/license", json={"key": token(signer[0], payload())}).status_code == 404
