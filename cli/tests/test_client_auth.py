from __future__ import annotations

import httpx
import pytest

from voxxy.client import VoxClient, VoxUnauthorized


class HeaderRecorder:
    def __init__(self) -> None:
        self.seen: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.headers)
        return httpx.Response(200, json={"status": "ok", "engines": []})


def test_vox_client_adds_auth_headers_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "env-key")
    recorder = HeaderRecorder()

    client = VoxClient("https://vox.delo.sh", transport=httpx.MockTransport(recorder))
    client.healthz()

    headers = recorder.seen[0]
    assert headers["authorization"] == "Bearer env-key"
    assert headers["x-api-key"] == "env-key"


def test_vox_client_explicit_api_key_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "env-key")
    recorder = HeaderRecorder()

    client = VoxClient(
        "https://vox.delo.sh",
        api_key="arg-key",
        transport=httpx.MockTransport(recorder),
    )
    client.healthz()

    headers = recorder.seen[0]
    assert headers["authorization"] == "Bearer arg-key"
    assert headers["x-api-key"] == "arg-key"


def test_vox_client_api_key_none_disables_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "env-key")
    recorder = HeaderRecorder()

    client = VoxClient(
        "https://vox.delo.sh",
        api_key=None,
        transport=httpx.MockTransport(recorder),
    )
    client.healthz()

    headers = recorder.seen[0]
    assert "authorization" not in headers
    assert "x-api-key" not in headers


def test_vox_client_maps_401_to_vox_unauthorized() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text="nope"))
    client = VoxClient("https://vox.delo.sh", transport=transport)

    with pytest.raises(VoxUnauthorized):
        client.list_voices()


def test_fetch_audio_does_not_forward_auth_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "env-key")
    seen: dict[str, str | None] = {}

    def fake_get(url: str, timeout: object) -> httpx.Response:
        seen["url"] = url
        seen["timeout"] = str(timeout)
        return httpx.Response(200, content=b"OGGDATA")

    monkeypatch.setattr(httpx, "get", fake_get)
    client = VoxClient("https://vox.delo.sh")

    audio = client.fetch_audio("https://cdn.example.com/audio/demo.ogg")

    assert audio == b"OGGDATA"
    assert seen["url"] == "https://cdn.example.com/audio/demo.ogg"
