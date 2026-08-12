from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from voxxy.client import SynthUrlResponse, VoxUnauthorized
from voxxy import http_tts


class FakeVoxClient:
    last_init: tuple[str, str | None] | None = None

    def __init__(self, base_url: str, *, api_key: str | None = None, **_: object) -> None:
        type(self).last_init = (base_url, api_key)

    def synthesize_wav(self, *, text: str, voice: str | None, cfg: float, steps: int) -> bytes:
        assert text
        assert cfg == 2.0
        assert steps == 10
        return f"WAV:{voice or 'default'}".encode()

    def synthesize_url(self, *, text: str, voice: str | None, cfg: float, steps: int) -> SynthUrlResponse:
        assert text
        return SynthUrlResponse(
            audio_url="https://vox.delo.sh/audio/demo.ogg",
            engine="voxcpm",
            duration_s=1.25,
            bytes=7,
            format="ogg_opus",
        )

    def fetch_audio(self, url: str) -> bytes:
        assert url.endswith("demo.ogg")
        return b"OGGDATA"


class FakeUnauthorizedClient(FakeVoxClient):
    def synthesize_wav(self, *, text: str, voice: str | None, cfg: float, steps: int) -> bytes:
        raise VoxUnauthorized("Unauthorized: POST /synthesize -> 401")


def test_run_writes_wav_and_prints_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(http_tts, "VoxClient", FakeVoxClient)
    out = tmp_path / "demo.wav"

    rc = http_tts.run([
        "--text",
        "hello world",
        "--out",
        str(out),
        "--json",
    ])

    assert rc == 0
    assert out.read_bytes() == b"WAV:default"
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "wav"
    assert payload["path"] == str(out)
    assert payload["mode"] == "synthesize-wav"


def test_run_uses_env_defaults_for_url_and_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_tts, "VoxClient", FakeVoxClient)
    monkeypatch.setenv("VOX_URL", "https://vox.delo.sh")
    monkeypatch.setenv("VOX_API_KEY", "secret")
    monkeypatch.setenv("VOX_VOICE", "rick")
    out = tmp_path / "demo.wav"

    rc = http_tts.run([
        "--text",
        "hello world",
        "--out",
        str(out),
    ])

    assert rc == 0
    assert FakeVoxClient.last_init == ("https://vox.delo.sh", "secret")
    assert out.read_bytes() == b"WAV:rick"


def test_run_uses_synthesize_url_for_ogg_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(http_tts, "VoxClient", FakeVoxClient)
    out = tmp_path / "demo.ogg"

    rc = http_tts.run([
        "--text",
        "hello world",
        "--voice",
        "rick",
        "--out",
        str(out),
        "--json",
    ])

    assert rc == 0
    assert out.read_bytes() == b"OGGDATA"
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "synthesize-url-fetch"
    assert payload["engine"] == "voxcpm"
    assert payload["audio_url"].endswith("demo.ogg")


def test_run_transcodes_for_mp3_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(http_tts, "VoxClient", FakeVoxClient)
    monkeypatch.setattr(http_tts, "_transcode_wav_bytes", lambda wav, fmt: b"MP3DATA")
    out = tmp_path / "demo.mp3"

    rc = http_tts.run([
        "--text",
        "hello world",
        "--out",
        str(out),
        "--format",
        "mp3",
        "--json",
    ])

    assert rc == 0
    assert out.read_bytes() == b"MP3DATA"
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "mp3"
    assert payload["mode"] == "synthesize-wav-transcode"


def test_run_reports_missing_ffmpeg_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(http_tts, "VoxClient", FakeVoxClient)

    def boom(wav: bytes, fmt: str) -> bytes:
        raise http_tts.MissingFfmpegError("ffmpeg not found")

    monkeypatch.setattr(http_tts, "_transcode_wav_bytes", boom)
    out = tmp_path / "demo.flac"

    rc = http_tts.run([
        "--text",
        "hello world",
        "--out",
        str(out),
        "--format",
        "flac",
    ])

    assert rc == 1
    assert "ffmpeg not found" in capsys.readouterr().err


def test_run_reports_auth_failures_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(http_tts, "VoxClient", FakeUnauthorizedClient)
    out = tmp_path / "demo.wav"

    rc = http_tts.run([
        "--text",
        "hello world",
        "--out",
        str(out),
    ])

    assert rc == 1
    err = capsys.readouterr().err
    assert "authentication failed" in err
    assert "VOX_API_KEY" in err


def test_read_text_supports_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = io.StringIO("hello from stdin")
    stdin.isatty = lambda: False  # type: ignore[method-assign]

    result = http_tts._read_text(text=None, text_file=None, stdin=stdin)

    assert result == "hello from stdin"
