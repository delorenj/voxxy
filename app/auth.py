from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# The web app: the shell at "/" and every static asset under this prefix are public, since the page
# is what asks for the key. The data routes it calls stay protected.
PUBLIC_STATIC_PREFIX = "/ui/"

PUBLIC_EXACT_PATHS = frozenset({
    "/",
    "/healthz",
    "/app-config",
    "/install.sh",
    "/bin/vox-speak",
})


def resolve_api_key_from_env(env_var: str = "VOX_API_KEY") -> str | None:
    raw = os.environ.get(env_var)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def resolve_operator_key_from_env(env_var: str = "VOX_OPERATOR_KEY") -> str | None:
    """The operator key (review routes only). Unset means those routes answer 503.

    A key equal to ``VOX_API_KEY`` would make the ordinary key an operator key, so it counts as unset
    (startup refuses it outright, see ``operator_key_conflict``)."""
    key = resolve_api_key_from_env(env_var)
    return None if key is not None and key == resolve_api_key_from_env() else key


def operator_key_conflict() -> bool:
    """True when ``VOX_OPERATOR_KEY`` is set to the same value as ``VOX_API_KEY`` (a misconfiguration)."""
    operator = resolve_api_key_from_env("VOX_OPERATOR_KEY")
    return operator is not None and operator == resolve_api_key_from_env()


# Routes only the operator may use. The middleware lets the operator key through on these (and only
# these) when it differs from VOX_API_KEY; the routes themselves then demand the operator key.
OPERATOR_PREFIX = "/consent/reviews"


def is_operator_path(path: str) -> bool:
    normalized = path.rstrip("/")
    return normalized == OPERATOR_PREFIX or normalized.startswith(OPERATOR_PREFIX + "/")


OPEN_PRINCIPAL = "open"


def _same(a: str, b: str) -> bool:
    """Constant-time equality that cannot raise on non-ASCII header text."""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def matched_key(headers: Headers, expected_key: str) -> str | None:
    """The header value (Bearer first, then X-API-Key) that equals ``expected_key``, or None.

    Only a value that actually passed the check is ever returned, so nothing derived from a header that
    failed it (an identity, a hash) can reach the rest of the app.
    """
    bearer_token = extract_bearer_token(headers.get("authorization"))
    if bearer_token and _same(bearer_token, expected_key):
        return bearer_token
    candidate = (headers.get("x-api-key") or "").strip()
    if candidate and _same(candidate, expected_key):
        return candidate
    return None


def key_principal(key: str) -> str:
    """The principal for an API key: first 16 hex of its sha256. The raw key is never stored or logged."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def root_principal() -> str:
    """The principal of the root key (``VOX_API_KEY``), or ``open`` when auth is off."""
    key = resolve_api_key_from_env()
    return OPEN_PRINCIPAL if key is None else key_principal(key)


def principal_for(headers: Headers) -> str:
    """Who is calling, for consent: first 16 hex of sha256 of the key that PASSED the check, or ``open`` when
    auth is off. A request that carries no passing key has no principal: 401 (the middleware already stops
    these; this is the fail-closed backstop, and it is what stops a made-up Bearer value from becoming an
    identity when the real key rides in X-API-Key).

    THE one place identity is derived. Per-customer keys do not exist yet (every caller holds the one
    shared key, so today every caller is the same principal); when they do, only this function changes.
    """
    expected = resolve_api_key_from_env()
    if expected is None:
        return OPEN_PRINCIPAL
    key = matched_key(headers, expected)
    if key is None:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
    return key_principal(key)


def extract_bearer_token(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def is_public_request(method: str, path: str) -> bool:
    if method.upper() not in {"GET", "HEAD"}:
        return False

    normalized = path if path == "/" else path.rstrip("/")
    if normalized in PUBLIC_EXACT_PATHS:
        return True
    if normalized.startswith("/audio/") and normalized.endswith(".ogg"):
        return True
    if path.startswith(PUBLIC_STATIC_PREFIX):
        return True
    # Listening on a sink is read-only: the stream carries /audio URLs, which are
    # already public. Desk listeners (a Mac's `voxxy listen`, HeyMa's speak-here
    # toggle) need no key; sending to a sink (play, say) does.
    parts = normalized.split("/")
    return len(parts) in (3, 4) and parts[1] == "sink" and bool(parts[2]) and \
        (len(parts) == 3 or parts[3] == "events")


def headers_have_valid_api_key(headers: Headers, expected_key: str) -> bool:
    return matched_key(headers, expected_key) is not None


class ApiKeyAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_key_resolver: Callable[[], str | None] = resolve_api_key_from_env,
    ) -> None:
        self.app = app
        self._api_key_resolver = api_key_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        api_key = self._api_key_resolver()
        method = str(scope.get("method", "GET"))
        path = str(scope.get("path", "/"))
        if api_key is None or is_public_request(method, path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        if headers_have_valid_api_key(headers, api_key):
            await self.app(scope, receive, send)
            return
        operator_key = resolve_operator_key_from_env()
        if operator_key and is_operator_path(path) and headers_have_valid_api_key(headers, operator_key):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
