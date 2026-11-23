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


def test_env_api_key_prefers_the_value_then_reads_the_ref_once(monkeypatch) -> None:
    import subprocess

    from voxxy import client as client_mod

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="from-vault\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(client_mod, "_REF_CACHE", {})
    ref = "op://DeLoSecrets/item/credential"
    assert client_mod.env_api_key({"VOX_API_KEY": " direct ", "VOX_API_KEY_REF": ref}) == "direct"
    assert calls == []
    assert client_mod.env_api_key({"VOX_API_KEY_REF": ref}) == "from-vault"
    assert client_mod.env_api_key({"VOX_API_KEY_REF": ref}) == "from-vault"
    assert calls == [["op", "read", "--no-newline", ref]]
    assert client_mod.env_api_key({}) is None


def test_env_api_key_is_none_when_the_ref_cannot_be_read(monkeypatch) -> None:
    import subprocess

    from voxxy import client as client_mod

    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no"))
    monkeypatch.setattr(client_mod, "_REF_CACHE", {})
    assert client_mod.env_api_key({"VOX_API_KEY_REF": "op://x/y/z"}) is None


def test_env_api_key_falls_back_to_the_config_ref(monkeypatch, tmp_path) -> None:
    import subprocess

    from voxxy import client as client_mod
    from voxxy import config as config_mod

    cfg = tmp_path / "config.toml"
    cfg.write_text('api_key_ref = "op://DeLoSecrets/host/credential"\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    monkeypatch.delenv("VOX_API_KEY_REF", raising=False)
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=f"key-for:{argv[-1]}", stderr=""))
    monkeypatch.setattr(client_mod, "_REF_CACHE", {})
    assert client_mod.env_api_key() == "key-for:op://DeLoSecrets/host/credential"
