from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import ApiKeyAuthMiddleware


def _build_app(api_key: str | None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ApiKeyAuthMiddleware, api_key_resolver=lambda: api_key)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/audio/{cache_id}.ogg")
    async def audio(cache_id: str) -> dict[str, str]:
        return {"cache_id": cache_id}

    @app.get("/install.sh")
    async def install() -> dict[str, str]:
        return {"script": "install"}

    @app.get("/bin/vox-speak")
    async def vox_speak() -> dict[str, str]:
        return {"script": "vox-speak"}

    @app.get("/voices")
    async def voices() -> list[str]:
        return ["rick"]

    @app.post("/synthesize")
    async def synthesize() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/mcp/")
    async def mcp_root() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_protected_routes_stay_open_when_vox_api_key_unset() -> None:
    client = TestClient(_build_app(None))

    assert client.get("/voices").status_code == 200
    assert client.post("/synthesize").status_code == 200
    assert client.get("/mcp/").status_code == 200


def test_missing_auth_gets_clean_401_when_vox_api_key_is_set() -> None:
    client = TestClient(_build_app("secret-key"))

    response = client.get("/voices")

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_bearer_header_is_accepted() -> None:
    client = TestClient(_build_app("secret-key"))

    response = client.post(
        "/synthesize",
        headers={"Authorization": "Bearer secret-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_x_api_key_header_is_accepted() -> None:
    client = TestClient(_build_app("secret-key"))

    response = client.get(
        "/voices",
        headers={"X-API-Key": "secret-key"},
    )

    assert response.status_code == 200
    assert response.json() == ["rick"]


def test_public_routes_remain_public_when_auth_is_enabled() -> None:
    client = TestClient(_build_app("secret-key"))

    assert client.get("/healthz").status_code == 200
    assert client.get("/audio/abc123.ogg").status_code == 200
    assert client.get("/install.sh").status_code == 200
    assert client.get("/bin/vox-speak").status_code == 200
    assert client.get("/mcp/").status_code == 401
