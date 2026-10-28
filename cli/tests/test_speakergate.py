"""ECAPA one-voice gate: the numeric core on synthetic embeddings, and the helper plumbing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from voxxy import refclip as rc
from voxxy import speakergate as sg


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def _map(rows: list[np.ndarray], voiced=None) -> sg.VoiceMap:
    emb = np.stack([_unit(r) for r in rows])
    return sg.VoiceMap(
        starts=np.arange(len(rows), dtype=float), emb=emb,
        voiced=np.ones(len(rows), dtype=bool) if voiced is None else np.asarray(voiced), win=2.0,
    )


def _voice(rng, base, noise=0.5):
    return base + noise * rng.standard_normal(base.shape)


def test_one_voice_is_coherent_and_two_voices_are_not() -> None:
    rng = np.random.default_rng(0)
    a, b = rng.standard_normal(32), rng.standard_normal(32)
    one = _map([_voice(rng, a) for _ in range(20)])
    mixed = _map([_voice(rng, a if i % 2 == 0 else b) for i in range(20)])
    c1, c2 = one.coherence(0, 21), mixed.coherence(0, 21)
    assert c1 and c2 and c1["mean"] > 0.7 and c2["mean"] < 0.35
    assert sg.verdict(c1, 0.8) == []
    assert any("several voices" in r for r in sg.verdict(c2, 0.8))


def test_too_few_voiced_windows_cannot_be_verified() -> None:
    rng = np.random.default_rng(1)
    a = rng.standard_normal(16)
    vm = _map([_voice(rng, a) for _ in range(6)], voiced=[1, 0, 0, 0, 1, 0])
    assert vm.coherence(0, 8) is None
    assert sg.verdict(None, None) == ["too little speech to verify the speaker"]


def test_dominant_voice_is_the_one_most_windows_agree_on() -> None:
    rng = np.random.default_rng(2)
    main, other = rng.standard_normal(32), rng.standard_normal(32)
    rows = [_voice(rng, main) for _ in range(30)] + [_voice(rng, other) for _ in range(8)]
    vm = _map(rows)
    assert vm.dominance(0, 30) > 0.8
    assert vm.dominance(31, 38) < 0.4
    assert any("not the main voice" in r for r in sg.verdict({"mean": 0.6, "p10": 0.5, "min": 0.4, "n": 8.0}, 0.05))


def _marker(start: float, end: float) -> rc.Marker:
    return rc.Marker(
        start=start, end=end, text="x", score=0.9,
        breakdown={"clarity": 1.0, "sentence": 1.0, "snr": 1.0, "background": 1.0, "density": 1.0, "filler": 1.0,
                   "duration": 1.0, "speaker": 1.0},
        metrics={"clip_frac": 0.0, "other_speaker_frac": 0.0},
    )


def test_apply_voice_gate_rejects_dialogue_and_ranks_the_clean_voice_first() -> None:
    rng = np.random.default_rng(3)
    a, b = rng.standard_normal(32), rng.standard_normal(32)
    rows = [_voice(rng, a) for _ in range(20)] + [_voice(rng, a if i % 2 else b) for i in range(20)]
    vm = _map(rows)
    mixed, clean = _marker(22, 38), _marker(1, 17)
    markers = [mixed, clean]
    rc.apply_voice_gate(markers, vm)
    assert markers[0] is clean and clean.ok and clean.metrics["voice_gated"] == 1.0
    assert not mixed.ok and any("several voices" in r for r in mixed.reasons)
    assert clean.breakdown["voice"] > mixed.breakdown["voice"]


def test_few_words_per_second_is_not_clean_speech() -> None:
    # Energy says "voiced" through laughter and effects; whisper heard 8 words in 15 s.
    np_ = np
    audio = (0.3 * np_.sin(np_.linspace(0, 2000, 16000 * 15))).astype("float32")
    feats = rc.frame_features(audio)
    words = [rc.Word(f"w{i}", 1.0 + i * 0.5, 1.3 + i * 0.5, 0.95) for i in range(8)]
    m = rc.score_span(audio, words, feats, 0, 8, 0.0, 15.0)
    assert not m.ok and any("few words" in r for r in m.reasons)
    assert m.metrics["word_rate"] == pytest.approx(8 / 15)


def test_embed_source_runs_the_helper_without_ld_library_path_and_caches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/libtorch/lib")
    src = tmp_path / "s.wav"
    src.write_bytes(b"RIFF")
    calls: list[tuple] = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        out = Path(argv[argv.index(str(sg.HELPER)) + 2])
        rng = np.random.default_rng(4)
        np.savez(out, starts=np.arange(10.0), emb=rng.standard_normal((10, 8)), rms=np.full(10, 0.1), win=2.0)
        return SimpleNamespace(returncode=0, stderr="")

    vm = sg.embed_source(src, run=fake_run)
    assert len(vm.starts) == 10 and len(calls) == 1
    assert "LD_LIBRARY_PATH" not in calls[0][1]["env"] and calls[0][0][:3] == ["uv", "run", "--quiet"]
    sg.embed_source(src, run=fake_run)
    assert len(calls) == 1  # second call came from the cache


def test_embed_source_failure_is_a_gate_unavailable_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    src = tmp_path / "s.wav"
    src.write_bytes(b"RIFF")

    def boom(argv, **kw):
        return SimpleNamespace(returncode=1, stderr="Traceback...\nImportError: torch\n")

    with pytest.raises(sg.GateUnavailable, match="ImportError: torch"):
        sg.embed_source(src, run=boom)

    def no_uv(argv, **kw):
        raise FileNotFoundError("uv")

    with pytest.raises(sg.GateUnavailable, match="uv is not installed"):
        sg.embed_source(src, run=no_uv)
