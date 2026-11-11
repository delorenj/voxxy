"""POST /synthesize routing: per-request references, fail-closed requests, X-Vox-Voice-Source.

The app is driven with a bare TestClient (no lifespan: no postgres, no engines). ``app.main._engine``
is an EngineOrchestrator over fake engines that record every call, and ``_repo`` is in-memory, so each
test can assert both the HTTP answer and exactly which engine was (or was not) handed the request.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app import voices as voices_mod
from app.engines import (
    DEFAULT_ENGINE_LANGUAGES,
    EngineOrchestrator,
    PermanentEngineError,
    RemoteEngineClient,
    SynthResult,
)
from app.voices import Voice

SECRET_TEXT = "patient reports chest pain since tuesday"
SECRET_PROMPT = "the exact words the speaker said"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FakeEngine:
    """Records what it was handed; answers with a fixed WAV or raises ``fail``."""

    def __init__(
        self,
        name: str,
        *,
        accepts_reference: bool = True,
        available: bool = True,
        fail: Optional[Exception] = None,
        wav: bytes = b"RIFF-fake-wav",
    ) -> None:
        self.name = name
        self.accepts_reference = accepts_reference
        self._available = available
        self.fail = fail
        self.wav = wav
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    async def generate(self, **kwargs) -> SynthResult:
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return SynthResult(wav_bytes=self.wav, sample_rate=48_000, engine=self.name)


class FakeRepo:
    def __init__(self, *voices: Voice) -> None:
        self.voices = {v.name: v for v in voices}
        self.lookups: list[str] = []

    async def get(self, name: str) -> Optional[Voice]:
        self.lookups.append(name)
        return self.voices.get(name)

    async def list(self) -> list[Voice]:
        return sorted(self.voices.values(), key=lambda v: v.name)


@pytest.fixture()
def vox(monkeypatch: pytest.MonkeyPatch, tmp_path, make_wav):
    """Wire app.main to fakes; returns a builder ``vox(*engines) -> TestClient``."""
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    monkeypatch.setattr(voices_mod, "VOICES_DIR", tmp_path)
    (tmp_path / "house-f1.wav").write_bytes(make_wav(seconds=10.0))
    (tmp_path / "rick.wav").write_bytes(make_wav(seconds=10.0))
    repo = FakeRepo(
        Voice(name="house-f1", display_name="House F1", wav_path="house-f1.wav", duration_s=10.0,
              prompt_text="house transcript", tags=["house", "clinical_ok"]),
        # Uploaded voices get a vibevoice reference, which makes VibeVoice their preferred engine.
        Voice(name="rick", display_name="Rick", wav_path="rick.wav", duration_s=10.0,
              prompt_text="rick transcript", tags=[], vibevoice_ref_path="rick.wav"),
    )
    monkeypatch.setattr(main, "_repo", repo)

    def build(*engines) -> TestClient:
        monkeypatch.setattr(main, "_engine", EngineOrchestrator(
            list(engines), reference_engines=["voxcpm"], engine_languages=dict(DEFAULT_ENGINE_LANGUAGES),
        ))
        client = TestClient(main.app)
        client.repo = repo  # type: ignore[attr-defined]
        return client

    return build


@pytest.fixture()
def clip(make_wav) -> bytes:
    return make_wav(seconds=12.0, sr=16_000)


def _clone_body(clip: bytes, **extra) -> dict:
    return {
        "text": SECRET_TEXT,
        "reference_audio_b64": _b64(clip),
        "reference_sample_rate": 16_000,
        "prompt_text": SECRET_PROMPT,
        **extra,
    }


# ---------------------------------------------------------------- voice source on every success

def test_no_voice_is_stock(vox) -> None:
    voxcpm = FakeEngine("voxcpm")
    r = vox(voxcpm, FakeEngine("elevenlabs", accepts_reference=False)).post("/synthesize", json={"text": "hi"})
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "voxcpm"
    assert r.headers["x-vox-voice-source"] == "stock"
    assert voxcpm.calls[0]["reference_wav_path"] is None


def test_registered_voice_is_registered(vox, tmp_path) -> None:
    voxcpm = FakeEngine("voxcpm")
    r = vox(voxcpm).post("/synthesize", json={"text": "hi", "voice": "house-f1"})
    assert r.status_code == 200
    assert r.headers["x-vox-voice-source"] == "registered"
    call = voxcpm.calls[0]
    assert call["reference_wav_path"] == str(tmp_path / "house-f1.wav")
    assert call["prompt_text"] == "house transcript"
    assert "reference_audio" not in call


def test_registered_voice_served_by_elevenlabs_is_stock(vox) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    client = vox(FakeEngine("voxcpm", available=False), eleven)
    r = client.post("/synthesize", json={"text": "hi", "voice": "house-f1"})
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "elevenlabs"
    assert r.headers["x-vox-voice-source"] == "stock"


def test_unknown_voice_is_a_404_not_a_wrapped_500(vox) -> None:
    r = vox(FakeEngine("voxcpm")).post("/synthesize", json={"text": "hi", "voice": "nobody"})
    assert r.status_code == 404


def test_synthesize_url_reports_voice_source(vox, clip, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.audio_codec, "to_ogg_opus", lambda wav, input_format: b"OggS-fake")
    monkeypatch.setattr(main.audio_cache, "put", lambda data: "abc123")
    monkeypatch.setenv("VOX_PUBLIC_BASE_URL", "https://vox.example")
    r = vox(FakeEngine("voxcpm")).post("/synthesize-url", json=_clone_body(clip, require_reference=True))
    assert r.status_code == 200
    assert r.headers["x-vox-voice-source"] == "reference"
    assert r.json()["voice_source"] == "reference"
    assert r.json()["audio_url"] == "https://vox.example/audio/abc123.ogg"


# ---------------------------------------------------------------- per-request reference

def test_reference_is_sent_inline_and_overrides_the_named_voice(vox, clip) -> None:
    voxcpm = FakeEngine("voxcpm")
    client = vox(voxcpm)
    r = client.post("/synthesize", json=_clone_body(clip, voice="no-such-voice", seed=42))
    assert r.status_code == 200, r.text
    assert r.headers["x-vox-voice-source"] == "reference"
    assert client.repo.lookups == []  # the named voice is not even looked up
    call = voxcpm.calls[0]
    assert call["reference_audio"] == clip
    assert call["reference_sample_rate"] == 16_000
    assert call["prompt_text"] == SECRET_PROMPT
    assert call["reference_wav_path"] is None
    assert call["seed"] == 42


def test_reference_without_transcript_is_reference_only_mode(vox, clip) -> None:
    voxcpm = FakeEngine("voxcpm")
    body = _clone_body(clip)
    del body["prompt_text"]
    r = vox(voxcpm).post("/synthesize", json=body)
    assert r.status_code == 200
    assert voxcpm.calls[0]["prompt_text"] is None


def test_reference_never_reaches_vibevoice(vox, clip) -> None:
    vibevoice = FakeEngine("vibevoice")
    voxcpm = FakeEngine("voxcpm")
    # VibeVoice first in the chain and the named voice prefers it: still never handed the clip.
    r = vox(vibevoice, voxcpm).post("/synthesize", json=_clone_body(clip, voice="rick"))
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "voxcpm"
    assert vibevoice.calls == []


def test_require_reference_fails_closed_when_the_engine_is_down(vox, clip) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    r = vox(FakeEngine("voxcpm", available=False), eleven).post(
        "/synthesize", json=_clone_body(clip, require_reference=True))
    assert r.status_code == 503
    assert r.headers["x-vox-error"] == "engines_unavailable"
    assert eleven.calls == []


def test_require_reference_fails_closed_when_the_engine_errors(vox, clip) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    voxcpm = FakeEngine("voxcpm", fail=RuntimeError("engine voxcpm 500: OOM: CUDA out of memory"))
    r = vox(voxcpm, eleven).post("/synthesize", json=_clone_body(clip, require_reference=True))
    assert r.status_code == 503
    assert eleven.calls == []


def test_require_reference_with_only_stock_engines_is_503(vox, clip) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    r = vox(eleven).post("/synthesize", json=_clone_body(clip, require_reference=True))
    assert r.status_code == 503
    assert r.headers["x-vox-error"] == "no_eligible_engine"
    assert eleven.calls == []


def test_reference_without_require_degrades_to_stock_without_the_clip(vox, clip) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    r = vox(FakeEngine("voxcpm", available=False), eleven).post("/synthesize", json=_clone_body(clip))
    assert r.status_code == 200
    assert r.headers["x-vox-voice-source"] == "stock"
    call = eleven.calls[0]
    assert "reference_audio" not in call
    assert call["prompt_text"] is None and call["reference_wav_path"] is None


def test_engine_4xx_on_a_reference_is_400_without_fallback(vox, clip) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    voxcpm = FakeEngine("voxcpm", fail=PermanentEngineError("engine voxcpm 400: INVALID_INPUT: bad clip"))
    r = vox(voxcpm, eleven).post("/synthesize", json=_clone_body(clip))
    assert r.status_code == 400
    assert eleven.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hi", "require_reference": True},
        {"text": "hi", "prompt_text": "words"},
        {"text": "hi", "reference_sample_rate": 16_000},
        {"text": "hi", "engines": []},
        {"text": "hi", "language": "not a tag"},
    ],
    ids=["require-without-clip", "prompt-without-clip", "rate-without-clip", "empty-allowlist", "bad-language"],
)
def test_contradictory_or_malformed_requests_are_422(vox, body: dict) -> None:
    voxcpm = FakeEngine("voxcpm")
    r = vox(voxcpm).post("/synthesize", json=body)
    assert r.status_code == 422
    assert voxcpm.calls == []


@pytest.mark.parametrize(
    "bad",
    [
        lambda w: "%%%not-base64%%%",
        lambda w: _b64(w(channels=2)),
        lambda w: _b64(w(seconds=16.0)),
        lambda w: _b64(w(subtype="FLOAT")),
        lambda w: _b64(w(fmt="FLAC", subtype="PCM_16")),
        lambda w: _b64(b"\x01\x02" * 4000),
    ],
    ids=["not-base64", "stereo", "too-long", "float", "flac", "raw-pcm"],
)
def test_require_reference_with_a_bad_clip_is_rejected_not_fallen_back(vox, make_wav, bad) -> None:
    voxcpm = FakeEngine("voxcpm")
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    body = {"text": "hi", "reference_audio_b64": bad(make_wav), "require_reference": True}
    r = vox(voxcpm, eleven).post("/synthesize", json=body)
    assert r.status_code == 422
    assert r.headers["x-vox-error"] == "invalid_reference"
    assert r.json()["detail"][0]["loc"] == ["body", "reference_audio_b64"]
    assert voxcpm.calls == [] and eleven.calls == []


def test_sample_rate_mismatch_is_422(vox, clip) -> None:
    r = vox(FakeEngine("voxcpm")).post("/synthesize", json=_clone_body(clip, reference_sample_rate=24_000))
    assert r.status_code == 422


# ---------------------------------------------------------------- engines allowlist

def test_allowlist_fails_closed_instead_of_falling_back(vox) -> None:
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    r = vox(FakeEngine("voxcpm", available=False), eleven).post(
        "/synthesize", json={"text": "hi", "voice": "house-f1", "engines": ["voxcpm"]})
    assert r.status_code == 503
    assert eleven.calls == []


def test_allowlist_picks_the_named_engine(vox) -> None:
    voxcpm = FakeEngine("voxcpm")
    eleven = FakeEngine("elevenlabs", accepts_reference=False)
    r = vox(voxcpm, eleven).post("/synthesize", json={"text": "hi", "engines": ["elevenlabs"]})
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "elevenlabs"
    assert voxcpm.calls == []


def test_allowlist_naming_nothing_configured_is_503(vox) -> None:
    r = vox(FakeEngine("voxcpm")).post("/synthesize", json={"text": "hi", "engines": ["kokoro"]})
    assert r.status_code == 503
    assert r.headers["x-vox-error"] == "no_eligible_engine"


# ---------------------------------------------------------------- language

def test_spanish_skips_vibevoice_even_when_the_voice_prefers_it(vox) -> None:
    vibevoice = FakeEngine("vibevoice")
    voxcpm = FakeEngine("voxcpm")
    r = vox(vibevoice, voxcpm).post("/synthesize", json={"text": "hola", "voice": "rick", "language": "es-MX"})
    assert r.status_code == 200
    assert r.headers["x-vox-engine"] == "voxcpm"
    assert r.headers["x-vox-voice-source"] == "registered"
    assert vibevoice.calls == []


def test_english_may_still_use_vibevoice(vox) -> None:
    vibevoice = FakeEngine("vibevoice")
    r = vox(vibevoice, FakeEngine("voxcpm")).post("/synthesize", json={"text": "hi", "voice": "rick", "language": "en"})
    assert r.headers["x-vox-engine"] == "vibevoice"


def test_language_no_allowed_engine_speaks_is_503(vox) -> None:
    vibevoice = FakeEngine("vibevoice")
    r = vox(vibevoice).post("/synthesize", json={"text": "hola", "language": "es", "engines": ["vibevoice"]})
    assert r.status_code == 503
    assert vibevoice.calls == []


# ---------------------------------------------------------------- hygiene: nothing echoed, nothing logged

def test_validation_errors_do_not_echo_the_body(vox, clip) -> None:
    b64 = _b64(clip)
    r = vox(FakeEngine("voxcpm")).post("/synthesize", json=_clone_body(clip, cfg=99))
    assert r.status_code == 422
    assert b64[:64] not in r.text
    assert SECRET_TEXT not in r.text and SECRET_PROMPT not in r.text
    assert r.json()["detail"][0]["loc"] == ["body", "cfg"]


def test_logs_never_carry_text_transcript_or_clip(vox, clip, caplog: pytest.LogCaptureFixture) -> None:
    """Drive success and failure paths through a real RemoteEngineClient whose engine echoes the
    request back in its error bodies (as a FastAPI 422 does); none of it may reach a log line."""
    b64 = _b64(clip)
    replies = iter([
        httpx.Response(422, json={"detail": [{"input": {"text": SECRET_TEXT, "prompt_text": SECRET_PROMPT}}]}),
        httpx.Response(500, json={"error": {"code": "MODEL_ERROR", "message": "RuntimeError: boom"}}),
    ])

    def engine(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/synthesize":
            return next(replies, None) or httpx.Response(200, json={
                "wav_b64": _b64(b"RIFF-ok"), "sample_rate": 48_000, "engine": "voxcpm", "bytes": 7,
            })
        return httpx.Response(404)

    remote = RemoteEngineClient("voxcpm", "http://engine", transport=httpx.MockTransport(engine))
    client = vox(remote)
    caplog.set_level(logging.DEBUG)

    first = client.post("/synthesize", json=_clone_body(clip, require_reference=True))      # engine 422 -> 400
    remote._health_cache = None
    second = client.post("/synthesize", json=_clone_body(clip, require_reference=True))     # engine 500 -> 503
    remote._health_cache = None
    third = client.post("/synthesize", json=_clone_body(clip, require_reference=True))      # ok
    bad = client.post("/synthesize", json={"text": SECRET_TEXT, "reference_audio_b64": _b64(b"junk" * 50),
                                           "prompt_text": SECRET_PROMPT, "require_reference": True})
    assert [first.status_code, second.status_code, third.status_code, bad.status_code] == [400, 503, 200, 422]
    assert SECRET_TEXT not in first.text  # the engine's echo is not relayed to the caller either

    logged = "\n".join(
        f"{rec.getMessage()}\n{rec.exc_text or ''}" for rec in caplog.records
    )
    assert "synth.completed" in logged and "voice_source=reference" in logged
    assert SECRET_TEXT not in logged
    assert SECRET_PROMPT not in logged
    assert b64[:64] not in logged and b64[-64:] not in logged


def test_reference_payload_reaches_the_engine_intact(vox, clip) -> None:
    seen: list[dict] = []

    def engine(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "wav_b64": _b64(b"RIFF-ok"), "sample_rate": 48_000, "engine": "voxcpm", "bytes": 7,
        })

    remote = RemoteEngineClient("voxcpm", "http://engine", transport=httpx.MockTransport(engine))
    r = vox(remote).post("/synthesize", json=_clone_body(clip, seed=7, steps=6))
    assert r.status_code == 200 and r.content == b"RIFF-ok"
    body = seen[0]
    assert base64.b64decode(body["reference_audio_b64"]) == clip
    assert body["reference_sample_rate"] == 16_000
    assert body["prompt_text"] == SECRET_PROMPT
    assert body["seed"] == 7 and body["steps"] == 6
