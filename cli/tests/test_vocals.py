"""Demucs vocal isolation: helper plumbing, cache reuse, auto-fallback in refclip, and CLI wiring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from typer.testing import CliRunner

import voxxy.commands.voice_clone as vc_cmd
from voxxy import refclip as rc
from voxxy import vocals as vc
from voxxy.app import app

runner = CliRunner()


def _marker(ok: bool, reasons: list[str]) -> rc.Marker:
    return rc.Marker(
        start=1.0, end=13.0, text="Hello world.", score=0.75 if ok else 0.45,
        breakdown={"clarity": 1.0, "sentence": 1.0, "snr": 0.8, "background": 0.9 if ok else 0.1,
                   "density": 1.0, "filler": 1.0, "duration": 0.5, "speaker": 1.0},
        ok=ok, reasons=reasons,
    )


def test_needs_vocal_isolation_detects_musical_rejections() -> None:
    clean = rc.Analysis(
        path="a.mp3", duration=30.0, words=[], pauses=[], speech_level_db=-20.0,
        pause_below_db=30.0, speakers=None, markers=[_marker(True, [])],
    )
    assert not vc.needs_vocal_isolation(clean)

    musical = rc.Analysis(
        path="b.mp3", duration=30.0, words=[], pauses=[], speech_level_db=-20.0,
        pause_below_db=20.0, speakers=None, markers=[_marker(False, ["noisy/musical gaps (bg 0.03)"])],
    )
    assert vc.needs_vocal_isolation(musical)


def test_isolate_vocals_runs_helper_without_ld_library_path_and_caches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/libtorch/lib")
    src = tmp_path / "music.mp3"
    src.write_bytes(b"ID3" + b"x" * 2000)
    calls: list[tuple] = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        out = Path(argv[argv.index(str(vc.HELPER)) + 2])
        out.write_bytes(b"RIFF" + b"\x00" * 2048)
        return SimpleNamespace(returncode=0, stderr="isolated vocals")

    cached = vc.isolate_vocals(src, run=fake_run)
    assert cached.is_file() and len(calls) == 1
    assert "LD_LIBRARY_PATH" not in calls[0][1]["env"]
    assert calls[0][0][:3] == ["uv", "run", "--quiet"]

    dst = tmp_path / "out_vocals.wav"
    res = vc.isolate_vocals(src, dst, run=fake_run)
    assert res == dst and dst.is_file()
    assert len(calls) == 1  # second call served from cache


def test_isolate_vocals_failure_raises_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    src = tmp_path / "music.mp3"
    src.write_bytes(b"ID3" + b"x" * 2000)

    def boom(argv, **kw):
        return SimpleNamespace(returncode=1, stderr="RuntimeError: demucs failed\n")

    with pytest.raises(vc.VocalIsolationUnavailable, match="demucs failed"):
        vc.isolate_vocals(src, run=boom)


def test_analyze_auto_isolates_when_music_blocks_all_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("VOXXY_ISOLATE_VOCALS", "1")
    src = tmp_path / "kurz.mp3"
    src.write_bytes(b"ID3" + b"x" * 2000)
    vocal_wav = tmp_path / "isolated.wav"
    vocal_wav.write_bytes(b"RIFF" + b"\x00" * 2048)

    monkeypatch.setattr(vc, "isolate_vocals", lambda p, **_kw: vocal_wav)
    seen_files: list[Path] = []

    def fake_pass(audio_file: Path, *, source_path: Path, vocals_isolated: bool, **_kw):
        seen_files.append(audio_file)
        if not vocals_isolated:
            return rc.Analysis(
                path=str(source_path), duration=30.0,
                words=[rc.Word(f"w{i}", float(i), float(i) + 0.5) for i in range(30)],
                pauses=[], speech_level_db=-20.0, pause_below_db=20.0, speakers=None,
                markers=[_marker(False, ["noisy/musical gaps (bg 0.03)"])],
            )
        return rc.Analysis(
            path=str(source_path), duration=30.0,
            words=[rc.Word(f"w{i}", float(i), float(i) + 0.5) for i in range(30)],
            pauses=[], speech_level_db=-20.0, pause_below_db=30.0, speakers=None,
            markers=[_marker(True, [])], audio_path=str(audio_file), vocals_isolated=True,
        )

    monkeypatch.setattr(rc, "_analyze_pass", fake_pass)
    analysis = rc.analyze(src)
    assert seen_files == [src, vocal_wav]
    assert analysis.vocals_isolated is True
    assert analysis.export_source == vocal_wav
    assert len(analysis.top(1)) == 1


def test_cli_voice_isolate_command(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "song.mp3"
    src.write_bytes(b"ID3" + b"x" * 2000)

    def fake_isolate(path, dst=None, **_kw):
        out = dst or path.with_name(f"{path.stem}_vocals.wav")
        out.write_bytes(b"RIFF" + b"\x00" * 2048)
        return out

    monkeypatch.setattr(vc_cmd.vc, "isolate_vocals", fake_isolate)
    res = runner.invoke(app, ["voice", "isolate", str(src), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["vocals"].endswith("song_vocals.wav")
    assert Path(payload["vocals"]).is_file()
