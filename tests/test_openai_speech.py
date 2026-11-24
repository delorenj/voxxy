"""POST /v1/audio/speech and GET /v1/models: the OpenAI-compatible surface.

Same harness as test_synthesize_routing: bare TestClient, fake engines, in-memory voice repo. ffmpeg is real
(it is in the image), so formats and speed are checked on actual decoded audio.
"""

from __future__ import annotations

import io
import shutil

import pytest
import soundfile as sf

from app import main
from tests.test_synthesize_routing import FakeEngine, vox  # noqa: F401  (vox is a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture()
def wav(make_wav) -> bytes:
    return make_wav(seconds=2.0, sr=24_000)


def _speak(client, **body):
    return client.post("/v1/audio/speech", json={"input": "hello there", **body})


def test_wav_is_returned_as_is_with_engine_header(vox, wav) -> None:
    voxcpm = FakeEngine("voxcpm", wav=wav)
    r = _speak(vox(voxcpm, FakeEngine("elevenlabs", accepts_reference=False)), response_format="wav", voice="house-f1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["x-vox-engine"] == "voxcpm"
    assert r.headers["x-vox-voice"] == "house-f1"
    assert "x-vox-voice-fallback" not in r.headers
    assert r.content == wav
    assert voxcpm.calls[0]["prompt_text"] == "house transcript"


@pytest.mark.parametrize("fmt,ctype,magic", [
    ("mp3", "audio/mpeg", None),
    ("opus", "audio/opus", b"OggS"),
    ("flac", "audio/flac", b"fLaC"),
    ("aac", "audio/aac", None),
    ("pcm", "audio/pcm", None),
])
def test_formats_transcode(vox, wav, fmt, ctype, magic) -> None:
    r = _speak(vox(FakeEngine("voxcpm", wav=wav)), response_format=fmt)
    assert r.status_code == 200
    assert r.headers["content-type"] == ctype
    assert len(r.content) > 200
    if magic:
        assert r.content.startswith(magic)
    if fmt == "pcm":  # raw s16le mono 24 kHz: 2 s = 96 000 bytes
        assert abs(len(r.content) - 96_000) < 2_000


def test_default_format_is_mp3(vox, wav) -> None:
    r = _speak(vox(FakeEngine("voxcpm", wav=wav)))
    assert r.headers["content-type"] == "audio/mpeg"


def test_speed_changes_duration(vox, wav) -> None:
    client = vox(FakeEngine("voxcpm", wav=wav))
    slow = _speak(client, response_format="wav", speed=0.5)
    fast = _speak(client, response_format="wav", speed=2.0)
    d_slow = sf.info(io.BytesIO(slow.content)).duration
    d_fast = sf.info(io.BytesIO(fast.content)).duration
    assert d_slow == pytest.approx(4.0, abs=0.2)
    assert d_fast == pytest.approx(1.0, abs=0.2)


@pytest.mark.parametrize("voice", ["alloy", "NOVA", "not-a-vox-voice"])
def test_openai_stock_or_unknown_voice_uses_default_and_says_so(vox, wav, voice) -> None:
    voxcpm = FakeEngine("voxcpm", wav=wav)
    r = _speak(vox(voxcpm), voice=voice, response_format="wav")
    assert r.status_code == 200
    assert r.headers["x-vox-voice"] == "house-f1"  # the code default; the fleet's compose sets its own
    assert "default voice 'house-f1'" in r.headers["x-vox-voice-fallback"]
    assert voxcpm.calls[0]["prompt_text"] == "house transcript"


def test_openai_voice_object_with_id(vox, wav) -> None:
    r = _speak(vox(FakeEngine("voxcpm", wav=wav)), voice={"id": "house-f1"}, response_format="wav")
    assert r.headers["x-vox-voice"] == "house-f1"


def test_omitted_voice_is_default_without_fallback_note(vox, wav) -> None:
    r = _speak(vox(FakeEngine("voxcpm", wav=wav)), response_format="wav")
    assert r.headers["x-vox-voice"] == "house-f1"
    assert "x-vox-voice-fallback" not in r.headers


def test_unrestricted_request_falls_back_to_elevenlabs(vox, wav) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False, wav=wav)
    r = _speak(vox(FakeEngine("voxcpm", available=False), eleven), model="tts-1", response_format="wav")
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "elevenlabs"


@pytest.mark.parametrize("extra", [
    {"json": {"model": "vox-local"}},
    {"json": {"model": "vox-local-hd"}},
    {"json": {"model": "tts-1"}, "headers": {"X-Vox-Require-Local": "1"}},
])
def test_require_local_refuses_the_elevenlabs_fallback(vox, wav, extra) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False, wav=wav)
    client = vox(FakeEngine("voxcpm", available=False), eleven)
    r = client.post("/v1/audio/speech", json={"input": "hi", "response_format": "wav", **extra["json"]},
                    headers=extra.get("headers", {}))
    assert r.status_code == 503
    assert r.headers["x-vox-error"] == "engines_unavailable"
    assert r.json()["error"]["code"] == "engines_unavailable"
    assert eleven.calls == []


def test_require_local_succeeds_when_a_local_engine_is_up(vox, wav) -> None:
    r = _speak(vox(FakeEngine("voxcpm", wav=wav), FakeEngine("elevenlabs", accepts_reference=False)),
               model="vox-local", response_format="wav")
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "voxcpm"


@pytest.mark.parametrize("body", [
    {"input": ""},
    {"input": "   "},
    {"input": "x" * 4097},
    {"input": "hi", "response_format": "ogg"},
    {"input": "hi", "speed": 9},
    {"model": "vox"},
])
def test_bad_requests_get_an_openai_error_envelope(vox, body) -> None:
    r = vox(FakeEngine("voxcpm")).post("/v1/audio/speech", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_extra_openai_fields_are_ignored(vox, wav) -> None:
    r = _speak(vox(FakeEngine("voxcpm", wav=wav)), response_format="wav", instructions="cheerful", stream_format="audio")
    assert r.status_code == 200


def test_models_lists_vox_and_vox_local(vox) -> None:
    r = vox(FakeEngine("voxcpm")).get("/v1/models")
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["vox-local", "vox"]


def test_auth_applies_to_v1_routes(vox, wav, monkeypatch) -> None:
    client = vox(FakeEngine("voxcpm", wav=wav))
    monkeypatch.setenv("VOX_API_KEY", "s3cret")
    assert _speak(client).status_code == 401
    assert client.get("/v1/models").status_code == 401
    ok = client.post(
        "/v1/audio/speech", json={"input": "hi", "response_format": "wav"},
        headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    ok2 = client.get("/v1/models", headers={"X-API-Key": "s3cret"})
    assert ok2.status_code == 200
