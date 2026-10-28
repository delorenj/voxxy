"""refclip: window selection, speaker gate, export argv. Synthetic signals only: no models, no network."""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")

from voxxy import refclip as rc  # noqa: E402

SR = rc.SAMPLE_RATE


# ---------------------------------------------------------------------------
# synthetic speech-shaped audio
# ---------------------------------------------------------------------------


def synth_sentences(sentences: list[list[tuple[str, float]]], *, word_s: float = 0.34, dip_s: float = 0.11,
                    gap_s: float = 0.5, lead_s: float = 0.4, seed: int = 0):
    """Noise bursts for words, silence for dips/gaps. Returns (audio, words).

    Each sentence is a list of (word, prob). Intra-sentence dips are 110 ms (a detectable
    pause, so mid-sentence cut points exist and the selector has to choose between them);
    sentence gaps are 500 ms (a real sentence break).
    """
    rng = np.random.default_rng(seed)
    chunks = [np.zeros(int(lead_s * SR), dtype=np.float32)]
    words: list[rc.Word] = []
    t = lead_s
    for sent in sentences:
        for k, (text, prob) in enumerate(sent):
            n = int(word_s * SR)
            burst = rng.normal(0, 0.12, n).astype(np.float32) * np.hanning(n).astype(np.float32) ** 0.3
            chunks.append(burst)
            words.append(rc.Word(text, t, t + word_s, prob))
            t += word_s
            last = k == len(sent) - 1
            pause = gap_s if last else dip_s
            chunks.append(rng.normal(0, 1e-4, int(pause * SR)).astype(np.float32))
            t += pause
    return np.concatenate(chunks), words


def sentence(n: int, *, prefix: str = "w", end: str = ".", probs: float = 1.0) -> list[tuple[str, float]]:
    ws = [(f"{prefix}{i}", probs) for i in range(n)]
    ws[0] = (ws[0][0].capitalize(), probs)
    ws[-1] = (ws[-1][0] + end, probs)
    return ws


def analyse(audio, words, **kw):
    feats = rc.frame_features(audio)
    pauses, level, below = rc.detect_pauses(feats.db, len(audio) / SR)
    markers = rc.score_windows(audio, words, pauses, feats, speech_thr_db=level - below, **kw)
    return feats, pauses, markers


# ---------------------------------------------------------------------------
# pauses
# ---------------------------------------------------------------------------


class TestPauses:
    def test_detects_gaps_between_bursts(self) -> None:
        audio, words = synth_sentences([sentence(4), sentence(4)])
        feats = rc.frame_features(audio)
        pauses, _level, below = rc.detect_pauses(feats.db, len(audio) / SR)
        assert below == rc.PAUSE_BELOW_DB
        # 0.5 s sentence gap sits between word 3 and word 4
        gap_start = words[3].end
        assert any(p.start <= gap_start + 0.1 and p.end >= gap_start + 0.4 for p in pauses)
        assert all(p.length >= rc.PAUSE_MIN_S - 0.01 for p in pauses)

    def test_relaxes_threshold_for_noisy_floor(self) -> None:
        rng = np.random.default_rng(1)
        # Floor only ~22 dB under speech: no pause at 30 dB, so the detector relaxes.
        speech = rng.normal(0, 0.2, SR).astype(np.float32)
        floor = rng.normal(0, 0.016, SR // 2).astype(np.float32)
        audio = np.concatenate([speech, floor, speech, floor, speech, floor, speech])
        feats = rc.frame_features(audio)
        pauses, _level, below = rc.detect_pauses(feats.db, len(audio) / SR)
        assert below < rc.PAUSE_BELOW_DB
        assert len(pauses) >= 2


# ---------------------------------------------------------------------------
# window selection
# ---------------------------------------------------------------------------


class TestWindows:
    def _long(self, **kw):
        sents = [sentence(5, prefix=f"s{i}x") for i in range(14)]
        audio, words = synth_sentences(sents)
        return audio, words, analyse(audio, words, **kw)

    def test_durations_within_bounds(self) -> None:
        _audio, _words, (_f, _p, markers) = self._long()
        assert markers
        assert all(10.0 <= m.duration <= 15.0 for m in markers)

    def test_custom_bounds(self) -> None:
        _a, _w, (_f, _p, markers) = self._long(min_seconds=4.0, max_seconds=6.0)
        assert markers and all(4.0 <= m.duration <= 6.0 for m in markers)

    def test_cutoff_fragment_never_a_window_start(self) -> None:
        # Source opens with a cut-off fragment ("uh was") like arnie.wav, after a pause.
        frag = [("uh", 0.13), ("was", 0.37)]
        sents = [frag] + [sentence(5, prefix=f"s{i}x") for i in range(12)]
        audio, words = synth_sentences(sents)
        _f, _p, markers = analyse(audio, words)
        assert markers
        assert all(m.first_word >= 2 for m in markers)
        assert all(not m.text.lower().startswith(("uh", "was")) for m in markers)

    def test_filler_edges_rejected(self) -> None:
        # Every sentence opens with "um" and closes with "uh": no admissible edge exists.
        sents = [[("um", 0.9)] + sentence(3, prefix=f"s{i}x") + [("uh", 0.9)] for i in range(14)]
        audio, words = synth_sentences(sents)
        _f, _p, markers = analyse(audio, words)
        for m in markers:
            first, last = words[m.first_word], words[m.last_word - 1]
            assert first.bare not in rc.FILLERS and last.bare not in rc.FILLERS

    def test_low_probability_edge_rejected(self) -> None:
        sents = [sentence(5, prefix=f"s{i}x") for i in range(14)]
        audio, words = synth_sentences(sents)
        bad = {5 * 3, 5 * 3 + 4}  # first and last word of sentence 3
        for i in bad:
            words[i].prob = 0.2
        _f, _p, markers = analyse(audio, words)
        for m in markers:
            assert m.first_word not in bad and (m.last_word - 1) not in bad

    def test_sentence_boundaries_preferred(self) -> None:
        _a, words, (_f, _p, markers) = self._long()
        best = markers[0]
        first, last = words[best.first_word], words[best.last_word - 1]
        # words are s{i}x{j}: aligned windows open on j == 0 and close on j == 4
        assert first.word.lower().endswith("x0")
        assert last.word.endswith("4.")
        assert best.breakdown["sentence"] > 0.9

    def test_aligned_beats_mid_sentence_cut(self) -> None:
        _a, words, (_f, _p, markers) = self._long()
        aligned = max(m.score for m in markers if m.breakdown["sentence"] > 0.9)
        misaligned = [m.score for m in markers if m.breakdown["sentence"] < 0.3]
        assert misaligned and aligned > max(misaligned)

    def test_non_overlapping_top_k(self) -> None:
        _a, _w, (_f, _p, markers) = self._long()
        top = rc.select_top(markers, 4)
        assert len(top) >= 2
        for i, a in enumerate(top):
            for b in top[i + 1:]:
                assert a.end <= b.start or b.end <= a.start

    def test_short_source_uses_whole_clip(self) -> None:
        audio, words = synth_sentences([sentence(5), sentence(4, prefix="z")])
        assert len(audio) / SR < 10.0
        _f, _p, markers = analyse(audio, words)
        assert len(markers) == 1
        assert markers[0].start == 0.0 and markers[0].end == pytest.approx(len(audio) / SR)

    def test_garbled_word_rejects_window(self) -> None:
        sents = [sentence(5, prefix=f"s{i}x") for i in range(14)]
        audio, words = synth_sentences(sents)
        words[30].prob = 0.1  # a hallucinated word in the middle of the source
        _f, _p, markers = analyse(audio, words)
        for m in markers:
            if m.first_word <= 30 < m.last_word:
                assert not m.ok and any("garbled" in r for r in m.reasons)

    def test_clipping_penalised(self) -> None:
        sents = [sentence(5, prefix=f"s{i}x") for i in range(14)]
        audio, words = synth_sentences(sents)
        _f, _p, clean = analyse(audio, words)
        loud = np.clip(audio * 40.0, -1.0, 1.0)
        _f, _p, clipped = analyse(loud, words)
        assert max(m.metrics["clip_frac"] for m in clipped) > 0.01
        assert not clipped[0].ok

    def test_markers_serialise(self) -> None:
        _a, _w, (_f, _p, markers) = self._long()
        d = markers[0].to_dict()
        assert set(d) >= {"start", "end", "duration", "text", "score", "breakdown", "ok", "reasons"}
        assert set(d["breakdown"]) >= {"clarity", "sentence", "snr", "background", "density", "filler", "speaker"}
        import json

        json.dumps(d)

    def test_no_words_no_markers(self) -> None:
        audio, _w = synth_sentences([sentence(5)])
        feats = rc.frame_features(audio)
        assert rc.score_windows(audio, [], [], feats) == []


# ---------------------------------------------------------------------------
# speaker gate
# ---------------------------------------------------------------------------


def voiced(f0: float, formants: tuple[float, float, float], seconds: float, rng, *, tilt: float = 1.0):
    """Harmonic source shaped by three formant bumps, re-drawn every 150 ms like phonemes."""
    out = []
    t_total = 0.0
    while t_total < seconds:
        n = int(0.15 * SR)
        t = np.arange(n) / SR
        jitter = 1.0 + rng.normal(0, 0.03)
        shift = rng.normal(0, 90.0, 3)  # per-"phoneme" formant movement (same for every speaker)
        sig = np.zeros(n)
        for k in range(1, int(4000 / f0)):
            freq = k * f0 * jitter
            amp = 0.0
            for fc, sh in zip(formants, shift):
                amp += np.exp(-0.5 * ((freq - (fc + sh)) / 220.0) ** 2)
            sig += (amp / (k ** (0.6 * tilt))) * np.sin(2 * np.pi * freq * t + rng.uniform(0, 6.28))
        out.append((sig * 0.05).astype(np.float32) * np.hanning(n).astype(np.float32) ** 0.2)
        t_total += 0.15
    return np.concatenate(out)


def conversation(plan: list[tuple[str, float]], seed: int = 3):
    rng = np.random.default_rng(seed)
    speakers = {"A": (115.0, (450.0, 1300.0, 2400.0)), "B": (210.0, (750.0, 1800.0, 2900.0))}
    pieces = []
    truth = []
    for who, secs in plan:
        f0, fm = speakers[who]
        pieces.append(voiced(f0, fm, secs, rng))
        pieces.append(rng.normal(0, 1e-4, int(0.4 * SR)).astype(np.float32))
        truth.append((who, secs + 0.4))
    return np.concatenate(pieces), truth


class TestSpeakerGate:
    def _map(self, plan):
        audio, truth = conversation(plan)
        feats = rc.frame_features(audio)
        _p, level, below = rc.detect_pauses(feats.db, len(audio) / SR)
        return rc.speaker_map(feats, speech_thr_db=level - below), truth

    def test_two_speakers_separated_dominant_is_longest(self) -> None:
        smap, truth = self._map([("A", 24), ("B", 14), ("A", 22), ("B", 10)])
        assert smap is not None and smap.n_clusters >= 2
        bounds, t = [], 0.0
        for who, secs in truth:
            bounds.append((t, t + secs, who))
            t += secs

        def who_at(x):
            return next(w for a, b, w in bounds if a <= x < b)

        labels_by_speaker: dict[str, dict[int, int]] = {"A": {}, "B": {}}
        for seg in smap.segments:
            mid = (seg.start + seg.end) / 2
            d = labels_by_speaker[who_at(mid)]
            d[seg.label] = d.get(seg.label, 0) + 1
        a_label = max(labels_by_speaker["A"], key=labels_by_speaker["A"].get)
        b_label = max(labels_by_speaker["B"], key=labels_by_speaker["B"].get)
        assert a_label != b_label
        assert smap.dominant == a_label  # A speaks 46 s of 70
        # purity: most of each speaker's segments carry that speaker's majority label
        assert labels_by_speaker["A"][a_label] / sum(labels_by_speaker["A"].values()) > 0.85
        assert labels_by_speaker["B"][b_label] / sum(labels_by_speaker["B"].values()) > 0.85

    def test_other_speaker_fraction_flags_foreign_windows(self) -> None:
        smap, _truth = self._map([("A", 24), ("B", 14), ("A", 22), ("B", 10)])
        assert smap is not None
        assert smap.other_fraction(2.0, 12.0) < 0.1        # deep inside the first A turn
        assert smap.other_fraction(26.0, 36.0) > 0.5       # inside B's turn

    def test_single_speaker_stays_one_cluster(self) -> None:
        smap, _truth = self._map([("A", 30), ("A", 30)])
        assert smap is not None and smap.n_clusters == 1
        assert smap.other_fraction(0.0, 60.0) == 0.0

    def test_too_little_speech_returns_none(self) -> None:
        audio, _truth = conversation([("A", 0.5)])
        feats = rc.frame_features(audio)
        assert rc.speaker_map(feats, speech_thr_db=-60.0) is None

    def test_speaker_penalty_in_score(self) -> None:
        audio, truth = conversation([("A", 24), ("B", 14), ("A", 22), ("B", 10)])
        feats = rc.frame_features(audio)
        _p, level, below = rc.detect_pauses(feats.db, len(audio) / SR)
        smap = rc.speaker_map(feats, speech_thr_db=level - below)
        words = [rc.Word(f"w{i}", 0.5 + i, 0.9 + i, 1.0) for i in range(60)]
        inside = rc.score_span(audio, words, feats, 1, 12, 1.0, 12.0, speakers=smap)
        foreign = rc.score_span(audio, words, feats, 25, 36, 25.0, 36.0, speakers=smap)
        assert foreign.metrics["other_speaker_frac"] > inside.metrics["other_speaker_frac"]
        assert not foreign.ok and any("other speaker" in r for r in foreign.reasons)


# ---------------------------------------------------------------------------
# MFCC front end
# ---------------------------------------------------------------------------


class TestFrontEnd:
    def test_shapes_and_levels(self) -> None:
        rng = np.random.default_rng(0)
        audio = np.concatenate([rng.normal(0, 0.2, SR), np.zeros(SR)]).astype(np.float32)
        f = rc.frame_features(audio)
        n = (len(audio) - rc.FRAME_WIN) // rc.FRAME_HOP + 1
        assert f.db.shape == (n,) and f.mfcc.shape == (n, 19) and f.peak_db.shape == (n,)
        assert f.db[10] > f.db[-10] + 60  # noise vs digital silence

    def test_tone_is_peaky_noise_is_not(self) -> None:
        rng = np.random.default_rng(0)
        t = np.arange(SR) / SR
        tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        noise = rng.normal(0, 0.2, SR).astype(np.float32)
        assert np.median(rc.frame_features(tone).peak_db) > 22
        assert np.median(rc.frame_features(noise).peak_db) < 16

    def test_music_in_gaps_hurts_background_score(self) -> None:
        sents = [sentence(5, prefix=f"s{i}x") for i in range(14)]
        audio, words = synth_sentences(sents)
        _f, _p, clean = analyse(audio, words)
        t = np.arange(len(audio)) / SR
        bed = (0.002 * (np.sin(2 * np.pi * 330 * t) + np.sin(2 * np.pi * 495 * t))).astype(np.float32)
        _f, _p, musical = analyse(audio + bed, words)
        assert musical[0].breakdown["background"] < clean[0].breakdown["background"] - 0.3


# ---------------------------------------------------------------------------
# verification stage (clip re-transcription), no model involved
# ---------------------------------------------------------------------------


class TestSelectAndVerifyPool:
    @staticmethod
    def mk(start, end, score, verified=False, ok=True):
        return rc.Marker(start=start, end=end, text="t", score=score, breakdown={}, ok=ok,
                         metrics={"verified": 1.0} if verified else {})

    def test_verified_outranks_higher_scoring_unverified(self) -> None:
        unverified = self.mk(0, 11, 0.98)
        verified = self.mk(30, 41, 0.80, verified=True)
        assert rc.select_top([unverified, verified], 2) == [verified, unverified]

    def test_rejected_never_selected_unless_asked(self) -> None:
        bad = self.mk(0, 11, 0.99, verified=True, ok=False)
        good = self.mk(30, 41, 0.6)
        assert rc.select_top([bad, good], 5) == [good]
        assert rc.select_top([bad, good], 5, include_rejected=True) == [good, bad]

    def test_verify_slots_cover_different_regions_and_skip_rejected(self, monkeypatch) -> None:
        seen = []
        # One region at several shifted edges, a second region, and a stage-1 reject.
        a1, a2, a3 = self.mk(10.0, 22.0, 0.98), self.mk(10.9, 22.8, 0.97), self.mk(9.2, 21.5, 0.96)
        b = self.mk(60.0, 71.0, 0.90)
        rej = self.mk(100.0, 111.0, 0.99, ok=False)
        analysis = rc.Analysis(path="x.wav", duration=200, words=[], pauses=[], speech_level_db=-20, pause_below_db=30,
                               speakers=None, markers=[rej, a1, a2, a3, b])

        monkeypatch.setattr(rc, "export_clip", lambda src, dst, s, e: seen.append((s, e)))
        monkeypatch.setattr(rc, "decode_audio", lambda p: None)
        monkeypatch.setattr(rc, "transcribe", lambda audio, **k: [rc.Word("hello", 0, 0.3, 0.9)])
        monkeypatch.setattr(rc, "apply_clip_transcript", lambda m, cw, ww: m.metrics.update(verified=1.0) or m)
        rc.verify_markers(analysis, n=8)
        assert seen == [(10.0, 22.0), (60.0, 71.0)]  # one per region; the reject was never transcribed
        assert analysis.top(2) == [a1, b] and rej not in analysis.top(5)


class TestVerify:
    def _marker(self, **kw):
        base = dict(start=1.0, end=12.0, text="x", score=0.8, breakdown={"sentence": 0.5, "clarity": 1.0,
                    "filler": 1.0, "snr": 0.8, "background": 1.0, "density": 1.0, "duration": 0.1, "speaker": 1.0},
                    metrics={"lead_pause": 0.5, "tail_pause": 0.5, "clip_frac": 0.0, "other_speaker_frac": 0.0})
        base.update(kw)
        return rc.Marker(**base)

    def _words(self, text: str, prob: float = 0.95):
        return [rc.Word(w, i * 0.4, i * 0.4 + 0.3, prob) for i, w in enumerate(text.split())]

    def test_agreement(self) -> None:
        a = self._words("so that had something to do with that")
        assert rc.agreement(a, a) == 1.0
        assert rc.agreement(a, self._words("completely different words here now")) < 0.3

    def test_dangling_last_word_rejected_on_clip_transcript(self) -> None:
        win = self._words("i wanted to escape from that home and")
        m = rc.apply_clip_transcript(self._marker(), self._words("I wanted to escape from that home and"), win)
        assert not m.ok and any("dangling" in r for r in m.reasons)

    def test_clean_clip_transcript_accepted_and_text_replaced(self) -> None:
        win = self._words("i wanted to escape from that home")
        clip = self._words("I wanted to escape from that home.")
        m = rc.apply_clip_transcript(self._marker(), clip, win)
        assert m.ok, m.reasons
        assert m.text == "I wanted to escape from that home."
        assert m.metrics["verified"] == 1.0

    def test_disagreeing_transcripts_rejected(self) -> None:
        win = self._words("alpha bravo charlie delta echo foxtrot golf")
        clip = self._words("one two three four five six seven.")
        m = rc.apply_clip_transcript(self._marker(), clip, win)
        assert not m.ok and any("unstable" in r for r in m.reasons)

    def test_filler_and_fragment_edges_rejected(self) -> None:
        win = self._words("um we shipped the thing")
        m = rc.apply_clip_transcript(self._marker(), self._words("Um we shipped the thing."), win)
        assert not m.ok and any("starts on" in r for r in m.reasons)

    def test_whisper_terminal_period_alone_is_not_sentence_evidence(self) -> None:
        win = self._words("none of it i would have been able to enjoy")
        clip = self._words("None of it I would have been able to enjoy.")
        weak = self._marker(metrics={"lead_pause": 0.0, "tail_pause": 0.0, "clip_frac": 0.0, "other_speaker_frac": 0.0})
        strong = self._marker()
        rc.apply_clip_transcript(weak, clip, win)
        rc.apply_clip_transcript(strong, clip, win)
        assert weak.breakdown["sentence"] < strong.breakdown["sentence"]


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExport:
    def test_argv_is_the_measured_recipe(self) -> None:
        argv = rc.build_export_argv(Path("/in/src.mp3"), Path("/out/clip.wav"), 12.64, 24.94)
        assert argv[0] == "ffmpeg" and "-y" in argv
        assert argv[argv.index("-ss") + 1] == "12.640"
        assert argv[argv.index("-t") + 1] == "12.300"
        assert argv.index("-ss") < argv.index("-i")  # input-side seek
        assert argv[argv.index("-i") + 1] == "/in/src.mp3"
        assert argv[argv.index("-af") + 1] == (
            "highpass=f=55,afade=t=in:d=0.02,afade=t=out:st=12.270:d=0.03,loudnorm=I=-20:TP=-1.5:LRA=11"
        )
        assert argv[argv.index("-ac") + 1] == "1"
        assert argv[argv.index("-ar") + 1] == "24000"
        assert argv[argv.index("-c:a") + 1] == "pcm_s16le"
        assert argv[-1] == "/out/clip.wav"

    def test_never_stream_copies(self) -> None:
        argv = rc.build_export_argv(Path("a.wav"), Path("b.wav"), 0.0, 5.0)
        assert "copy" not in argv and "-c" not in argv

    def test_fade_out_tracks_duration(self) -> None:
        assert "afade=t=out:st=4.970:d=0.03" in rc.export_filter(5.0)

    def test_decode_argv_is_f32_mono_16k(self) -> None:
        argv = rc.build_decode_argv(Path("x.mp3"))
        assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-f") + 1] == "f32le"
        assert argv[argv.index("-ac") + 1] == "1" and argv[-1] == "pipe:1"

    def test_export_rejects_empty_window(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            rc.export_clip(tmp_path / "a.wav", tmp_path / "b.wav", 5.0, 5.0)

    def test_missing_dependency_message_has_exact_fix(self) -> None:
        err = rc._missing("faster-whisper")
        assert 'uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force' in str(err)
