from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import wave
from pathlib import Path

import httpx
import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_module():
    name = "vox_tts_plugin_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\0\0" * 240)
    return buffer.getvalue()


def _ogg_opus_bytes() -> bytes:
    # Minimal signature fixture: provider validates container + Opus stream id;
    # codec validity itself is verified in the opt-in live test with ffprobe.
    return b"OggS" + (b"\0" * 24) + b"OpusHead" + (b"\0" * 32)


def test_manifest_has_canonical_vox_name() -> None:
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == "vox"
    assert manifest["kind"] == "backend"


def test_provider_metadata_defaults_to_rick_and_is_voice_compatible() -> None:
    module = _load_plugin_module()
    provider = module.VoxTTSProvider(base_url="https://vox.example")

    assert provider.name == "vox"
    assert provider.display_name == "Vox"
    assert provider.default_voice() == "rick"
    assert provider.voice_compatible is True


def test_list_voices_and_health_fail_cleanly() -> None:
    module = _load_plugin_module()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "engines": [{"name": "engine", "ready": True}]})
        if request.url.path == "/voices":
            return httpx.Response(200, json=[{"name": "rick", "display_name": "Rick", "tags": ["fleet"]}])
        raise AssertionError(request.url)

    provider = module.VoxTTSProvider(base_url="https://vox.example", transport=httpx.MockTransport(handler))
    assert provider.is_available() is True
    assert provider.list_voices() == [{"id": "rick", "display": "Rick", "tags": ["fleet"]}]

    unavailable = module.VoxTTSProvider(
        base_url="https://vox.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"detail": "down"})),
    )
    assert unavailable.is_available() is False
    assert unavailable.list_voices() == []


def test_synthesize_ogg_downloads_local_telegram_artifact_and_honors_rick(tmp_path: Path) -> None:
    module = _load_plugin_module()
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/synthesize-url":
            return httpx.Response(200, json={"audio_url": "/audio/abc.ogg", "format": "ogg_opus"})
        if request.url.path == "/audio/abc.ogg":
            return httpx.Response(200, content=_ogg_opus_bytes(), headers={"content-type": "audio/ogg"})
        raise AssertionError(request.url)

    provider = module.VoxTTSProvider(base_url="https://vox.example", transport=httpx.MockTransport(handler))
    written = provider.synthesize("hello", str(tmp_path / "speech.mp3"), format="ogg")

    assert Path(written).suffix == ".ogg"
    assert Path(written).read_bytes() == _ogg_opus_bytes()
    assert calls == [
        ("POST", "/synthesize-url", {"text": "hello", "voice": "rick"}),
        ("GET", "/audio/abc.ogg", None),
    ]


def test_vox_voice_env_pins_one_agent_against_shared_fleet_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``VOX_VOICE`` outranks the voice Hermes passes from ``tts.voice``.

    Fleet agents share one ``config.yaml`` by symlink, so the passed voice
    is the fleet default, not a per-call choice. Pinning ``VOX_VOICE`` in an
    agent's systemd unit is what gives that agent its own voice.
    """
    module = _load_plugin_module()
    calls: list[dict | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content) if request.content else None)
        return httpx.Response(200, content=_wav_bytes(), headers={"content-type": "audio/wav"})

    provider = module.VoxTTSProvider(
        base_url="https://vox.example", transport=httpx.MockTransport(handler)
    )

    monkeypatch.setenv("VOX_VOICE", "mitch")
    provider.synthesize("hello", str(tmp_path / "a.wav"), voice="rick", format="wav")
    assert calls[-1] == {"text": "hello", "voice": "mitch"}
    assert provider.default_voice() == "mitch"

    # Blank env is treated as unset, not as a request for an empty voice.
    monkeypatch.setenv("VOX_VOICE", "   ")
    provider.synthesize("hello", str(tmp_path / "b.wav"), voice="rick", format="wav")
    assert calls[-1] == {"text": "hello", "voice": "rick"}

    monkeypatch.delenv("VOX_VOICE", raising=False)
    provider.synthesize("hello", str(tmp_path / "c.wav"), voice="rick", format="wav")
    assert calls[-1] == {"text": "hello", "voice": "rick"}


def test_synthesize_wav_and_clean_upstream_error(tmp_path: Path) -> None:
    module = _load_plugin_module()
    wav = _wav_bytes()
    provider = module.VoxTTSProvider(
        base_url="https://vox.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wav, headers={"content-type": "audio/wav"})),
    )
    written = provider.synthesize("hello", str(tmp_path / "speech.mp3"), voice="rick", format="wav")
    assert Path(written).suffix == ".wav"
    assert Path(written).read_bytes() == wav

    broken = module.VoxTTSProvider(
        base_url="https://vox.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"detail": "engine unavailable"})),
    )
    with pytest.raises(module.VoxTTSProviderError, match=r"Vox request to /synthesize failed \(503\): engine unavailable"):
        broken.synthesize("hello", str(tmp_path / "broken.wav"), format="wav")


def test_real_hermes_category_plugin_discovery_registers_vox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise Hermes' category scanner, registry, and TTS dispatcher."""
    hermes_source = Path("/home/delorenj/.hermes/hermes-agent")
    monkeypatch.syspath_prepend(str(hermes_source))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    home = Path(os.environ["HERMES_HOME"])
    plugin_dir = home / "plugins" / "tts" / "vox"
    plugin_dir.parent.mkdir(parents=True)
    shutil.copytree(PLUGIN_ROOT, plugin_dir)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["tts/vox"]}, "tts": {"provider": "vox", "voice": "rick", "output_format": "ogg", "vox": {"base_url": "https://fleet.vox.example", "voice": "rick"}}})
    )

    from agent import tts_registry
    from hermes_cli import plugins as plugins_module
    from hermes_cli.plugins import PluginManager
    from tools import tts_tool

    tts_registry._reset_for_tests()
    manager = PluginManager()
    user_manifest_keys = {
        manifest.key or manifest.name
        for manifest in manager._collect_directory_manifests()
        if manifest.source == "user"
    }
    manager.discover_and_load()
    provider = tts_registry.get_provider("vox")

    assert user_manifest_keys == {"tts/vox"}
    assert manager._plugins["tts/vox"].enabled is True
    assert provider is not None
    assert provider.name == "vox"
    assert provider.default_voice() == "rick"
    assert provider._resolve_base_url() == "https://fleet.vox.example"
    # The dispatch contract is exercised without an outbound HTTP request:
    # resolving configured `vox` must invoke this registered provider, never
    # return None for the Edge fallback branch.
    monkeypatch.setattr(provider, "synthesize", lambda text, output_path, **kwargs: output_path)
    # Avoid the dispatcher's recovery refresh replacing the test's already
    # discovered provider instance; discovery itself was exercised above.
    monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda force=False: None)
    assert tts_tool._dispatch_to_plugin_provider(
        "hello", str(tmp_path / "out.ogg"), "vox",
        {"provider": "vox", "voice": "rick", "output_format": "ogg"},
    ) == str(tmp_path / "out.ogg")
    tts_registry._reset_for_tests()


def test_live_vox_synthesis_writes_local_ogg_opus(tmp_path: Path) -> None:
    """Real upstream proof; set VOX_LIVE_TEST=1 to opt into network synthesis."""
    if os.getenv("VOX_LIVE_TEST") != "1":
        pytest.skip("exactly blocked: set VOX_LIVE_TEST=1 to allow live https://vox.delo.sh synthesis")
    module = _load_plugin_module()
    provider = module.VoxTTSProvider(base_url="https://vox.delo.sh", default_voice="rick", timeout=120)
    assert provider.is_available(), "https://vox.delo.sh healthz did not report a ready engine"
    assert any(voice["id"] == "rick" for voice in provider.list_voices())
    output = Path(provider.synthesize("Hermes Vox live synthesis check.", str(tmp_path / "live.mp3"), format="ogg"))
    data = output.read_bytes()
    assert output.suffix == ".ogg"
    assert data[:4] == b"OggS" and b"OpusHead" in data[:128]
