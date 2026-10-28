"""Reference-clip analysis: find the cleanest 10-15 s of speech in a longer recording.

Why this exists. Measured on ``arnold`` (2026-10-28): the single biggest quality
lever for voxcpm cloning is giving it the exact transcript of the reference clip
(``prompt_text``), which switches the model from voice-clone to continuation mode
(ECAPA speaker similarity 0.59 -> 0.72 for a clean sentence-boundary cut -> 0.82-0.85
with the transcript). That only works if the clip starts and ends on whole
sentences (no cut-off word fragments, no "uh"), is one speaker, and has no music.
This module finds such a clip automatically and exports it with the exact recipe
that produced the measured results.

Pipeline (each stage is a pure function so tests drive it with synthetic data):

  decode_audio          ffmpeg -> 16 kHz mono float32 (NOT faster-whisper's PyAV
                        decode, which breaks with "unexpected keyword argument
                        metadata_errors")
  transcribe            faster-whisper word timestamps (+ on-disk cache)
  frame_features        per-10 ms: dB energy, spectral flatness, MFCC
  detect_pauses         frames 30 dB under the p90 speech level, >= 80 ms
  speaker_map           ~2 s segments -> MFCC+CMN embedding -> cluster -> dominant
  score_windows         every contiguous word span that starts after a pause and
                        ends before one, duration in [min, max], scored
  verify_markers        re-transcribe the finalists' own exported clips (punctuated,
                        more accurate) and re-judge edges, fillers, agreement
  select_top            non-overlapping top-K
  export_clip           the ffmpeg recipe (argv builder + runner)

Heavy dependencies (numpy, scipy, faster-whisper) are imported inside functions so
``voxxy health`` and friends never pay for them; a missing one raises
:class:`CloneDepsMissing` with the exact fix.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

SAMPLE_RATE = 16000
DEFAULT_WHISPER_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
DEFAULT_MIN_SECONDS = 10.0
DEFAULT_MAX_SECONDS = 15.0

INSTALL_HINT = 'uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force'

FRAME_WIN = 400  # 25 ms @ 16 kHz
FRAME_HOP = 160  # 10 ms
FRAME_S = FRAME_HOP / SAMPLE_RATE
NFFT = 512

PAUSE_BELOW_DB = 30.0                    # pause = this far under the p90 speech level
PAUSE_FALLBACK_DB = (25.0, 20.0, 15.0)   # noisy sources: relax until enough pauses exist
PAUSE_MIN_S = 0.08
LEAD_S = 0.12                            # silence kept before the first word of a clip
TRAIL_S = 0.15                           # silence kept after the last word
BOUNDARY_TOL_S = 0.15                    # whisper word times drift by about this much vs energy
MIN_WORD_PROB = 0.5                      # edge words below this are treated as fragments
MIN_ANY_WORD_PROB = 0.30                 # any word below this inside a clip = garbled / hallucinated

FILLERS = frozenset({"uh", "um", "er", "erm", "uhm", "umm", "uhh", "hmm", "mm", "mmm", "hm", "eh"})
# A clip must not END on a word that grammatically needs a follower ("... would have").
# Whisper turbo often emits lowercase text with no punctuation, so this and the
# pause length are the only sentence-boundary evidence for many sources.
DANGLING = frozenset({
    "a", "an", "the", "to", "of", "and", "but", "or", "if", "with", "in", "for", "because", "so", "that",
    "as", "at", "by", "than", "then", "i", "my", "your", "his", "her", "our", "their", "its", "is", "was",
    "are", "were", "be", "been", "have", "has", "had", "would", "could", "should", "will", "can", "from",
    "into", "about", "like", "when", "while", "which", "who", "what", "also", "just", "not", "no",
})
SENTENCE_END = (".", "?", "!", "…")
CLAUSE_END = (",", ";", ":", "—")

# Combined-score weights (sum to 1.0). Sentence boundaries and word clarity lead
# because they are what the measurements showed matter; SNR/background keep music
# and noisy rooms out; duration only nudges toward the long end of the range.
WEIGHTS = {
    "clarity": 0.20,
    "sentence": 0.16,
    "snr": 0.15,
    "background": 0.14,
    "density": 0.13,
    "filler": 0.10,
    "duration": 0.04,
    "speaker": 0.08,
}
# Gates: a window failing any of these is `ok=False` regardless of score.
MAX_OTHER_SPEAKER_FRAC = 0.10
MAX_FILLERS = 2
MAX_CLIP_FRAC = 0.01
MIN_MEAN_PROB = 0.55
MIN_BACKGROUND = 0.35
MIN_DENSITY_FRAC = 0.45
MIN_WORDS_PER_SEC = 1.2   # fewer transcribed words per second than this = the audio is mostly not speech (laughs, effects, music)
MIN_OK_SCORE = 0.50


class CloneDepsMissing(RuntimeError):
    """A heavy optional dependency is not installed; the message carries the fix."""


def _missing(what: str) -> CloneDepsMissing:
    return CloneDepsMissing(
        f"missing optional dependency '{what}' (needed by voxxy voice clone/candidates/--auto).\n"
        f"Fix:  {INSTALL_HINT}"
    )


def _np():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise _missing("numpy") from exc
    return np


def _scipy_fft():
    try:
        from scipy import fft
    except ImportError as exc:  # pragma: no cover
        raise _missing("scipy") from exc
    return fft


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass
class Word:
    word: str          # as whisper emitted it (punctuation kept; used for sentence boundaries)
    start: float
    end: float
    prob: float = 1.0

    @property
    def bare(self) -> str:
        return re.sub(r"[^\w'-]+", "", self.word.lower()).strip("-'")


@dataclass
class Pause:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Marker:
    """One candidate reference clip (all times in seconds of the source)."""

    start: float
    end: float
    text: str
    score: float
    breakdown: dict[str, float]
    metrics: dict[str, float] = field(default_factory=dict)
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    first_word: int = 0   # word index range [first_word, last_word) in the transcript
    last_word: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
            "score": round(self.score, 4),
            "ok": self.ok,
            "reasons": list(self.reasons),
            "breakdown": {k: round(v, 4) for k, v in self.breakdown.items()},
            "metrics": {k: round(v, 4) for k, v in self.metrics.items()},
        }


@dataclass
class FrameFeatures:
    db: Any            # (T,) frame energy in dB
    peak_db: Any       # (T,) spectral peakiness in dB (~10 = noise, 25+ = sustained tones/music)
    mfcc: Any          # (T, 19) cepstra c1..c19


@dataclass
class SpeakerSegment:
    start: float
    end: float
    label: int
    speech_s: float


@dataclass
class SpeakerMap:
    segments: list[SpeakerSegment]
    dominant: int
    n_clusters: int

    def other_fraction(self, start: float, end: float) -> float:
        """Share of the window's speaker segments that belong to a non-dominant speaker."""
        total = other = 0.0
        for seg in self.segments:
            overlap = min(end, seg.end) - max(start, seg.start)
            if overlap < min(1.0, 0.5 * (end - start)):
                continue
            total += overlap
            if seg.label != self.dominant:
                other += overlap
        return other / total if total > 0 else 0.0


@dataclass
class Analysis:
    path: str
    duration: float
    words: list[Word]
    pauses: list[Pause]
    speech_level_db: float
    pause_below_db: float
    speakers: Optional[SpeakerMap]
    markers: list[Marker]   # every scored window, best first
    audio: Any = field(default=None, repr=False)
    feats: Optional[FrameFeatures] = field(default=None, repr=False)
    voices: Any = field(default=None, repr=False)   # speakergate.VoiceMap, or None when the ECAPA gate could not run
    voice_gate_note: str = ""                        # why it did not run

    def top(self, k: int = 5, *, include_rejected: bool = False) -> list[Marker]:
        return select_top(self.markers, k, include_rejected=include_rejected)


# ---------------------------------------------------------------------------
# decode + transcribe
# ---------------------------------------------------------------------------


def build_decode_argv(src: Path, sample_rate: int = SAMPLE_RATE) -> list[str]:
    """ffmpeg argv that streams ``src`` as mono float32 little-endian PCM on stdout."""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", str(src), "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "f32le", "pipe:1",
    ]


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        from voxxy.audio import FfmpegMissing
        raise FfmpegMissing(
            "ffmpeg not found on PATH (Debian/Ubuntu: sudo apt install ffmpeg; macOS: brew install ffmpeg)"
        )


def decode_audio(src: Path, sample_rate: int = SAMPLE_RATE):
    """Decode any audio/video file to a float32 mono numpy array via an ffmpeg pipe."""
    np = _np()
    _require_ffmpeg()
    proc = subprocess.run(build_decode_argv(src, sample_rate), capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"ffmpeg could not decode {src}: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


_MODEL_CACHE: dict[str, Any] = {}


def whisper_model_name() -> str:
    return os.environ.get("VOXXY_WHISPER_MODEL") or DEFAULT_WHISPER_MODEL


def _load_whisper(name: str):
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise _missing("faster-whisper") from exc
    threads = int(os.environ.get("VOXXY_WHISPER_THREADS") or min(8, os.cpu_count() or 4))
    model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads)
    _MODEL_CACHE[name] = model
    return model


def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "voxxy" / "refclip"


def _cache_key(path: Path, model: str, language: Optional[str]) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{model}|{language or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def transcribe(
    audio, *, model: Optional[str] = None, language: Optional[str] = None, beam_size: int = 5,
) -> list[Word]:
    """Word-timestamped transcript of a 16 kHz mono float32 array.

    ``condition_on_previous_text=False`` because on long music/noise stretches the
    default feeds hallucinations forward and the whole tail turns to garbage.
    """
    name = model or whisper_model_name()
    lang = language or os.environ.get("VOXXY_WHISPER_LANGUAGE") or None
    wm = _load_whisper(name)
    segments, _info = wm.transcribe(
        audio, word_timestamps=True, beam_size=beam_size, language=lang,
        condition_on_previous_text=False, vad_filter=False,
    )
    out: list[Word] = []
    for seg in segments:
        for w in (seg.words or []):
            text = (w.word or "").strip()
            if not text:
                continue
            out.append(Word(text, float(w.start), float(w.end), float(getattr(w, "probability", 1.0) or 0.0)))
    return out


def transcribe_file(path: Path, audio=None, *, use_cache: bool = True) -> list[Word]:
    """Transcribe a file, caching the words on disk keyed by file identity + model."""
    name = whisper_model_name()
    lang = os.environ.get("VOXXY_WHISPER_LANGUAGE") or None
    cache_file = _cache_dir() / f"{_cache_key(path, name, lang)}.json"
    if use_cache and cache_file.is_file():
        try:
            return [Word(**w) for w in json.loads(cache_file.read_text())]
        except (OSError, ValueError, TypeError):
            pass
    audio = decode_audio(path) if audio is None else audio
    words = transcribe(audio, model=name, language=lang)
    if use_cache:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps([asdict(w) for w in words]))
        except OSError:
            pass
    return words


def words_text(words: Sequence[Word]) -> str:
    return re.sub(r"\s+", " ", " ".join(w.word.strip() for w in words)).strip()


# ---------------------------------------------------------------------------
# frame features
# ---------------------------------------------------------------------------


def _mel_filterbank(n_mels: int = 24, nfft: int = NFFT, sr: int = SAMPLE_RATE, fmin: float = 80.0, fmax: float = 7600.0):
    np = _np()

    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel2hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    hz_pts = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    bins = np.fft.rfftfreq(nfft, 1.0 / sr)
    fb = np.zeros((n_mels, len(bins)), dtype=np.float32)
    for m in range(1, n_mels + 1):
        lo, ctr, hi = hz_pts[m - 1], hz_pts[m], hz_pts[m + 1]
        up = (bins - lo) / max(ctr - lo, 1e-9)
        down = (hi - bins) / max(hi - ctr, 1e-9)
        fb[m - 1] = np.maximum(0.0, np.minimum(up, down))
    return fb


def frame_features(audio, *, chunk_frames: int = 4096) -> FrameFeatures:
    """dB energy, spectral peakiness and MFCC (c1..c19) for every 10 ms frame.

    Peakiness, not spectral flatness, is the tonality cue: a quiet frame's spectrum
    slopes steeply (all low frequencies) so its flatness is near zero even for plain
    room noise. Peakiness is how far the tallest bin stands above its own
    neighbourhood, which a slope does not fake. Noise sits near 10 dB; a held note
    or a music bed is 25+.
    """
    np = _np()
    fft = _scipy_fft()
    try:
        from scipy.ndimage import uniform_filter1d
    except ImportError as exc:  # pragma: no cover
        raise _missing("scipy") from exc
    audio = np.asarray(audio, dtype=np.float32)
    if len(audio) < FRAME_WIN:
        audio = np.pad(audio, (0, FRAME_WIN - len(audio)))
    view = np.lib.stride_tricks.sliding_window_view(audio, FRAME_WIN)[::FRAME_HOP]
    n = view.shape[0]
    window = np.hamming(FRAME_WIN).astype(np.float32)
    fb = _mel_filterbank()
    db = np.empty(n, dtype=np.float32)
    peak = np.empty(n, dtype=np.float32)
    mfcc = np.empty((n, 19), dtype=np.float32)
    for s in range(0, n, chunk_frames):
        frames = np.array(view[s:s + chunk_frames])
        e = min(s + chunk_frames, n)
        db[s:e] = 10.0 * np.log10(np.mean(frames ** 2, axis=1) + 1e-10)
        spec = np.abs(np.fft.rfft(frames * window, NFFT, axis=1)) ** 2 + 1e-12
        logspec = 10.0 * np.log10(spec[:, 3:181])
        local = uniform_filter1d(logspec, size=17, axis=1, mode="nearest")
        peak[s:e] = np.max(logspec - local, axis=1)
        logmel = np.log(spec @ fb.T + 1e-10)
        mfcc[s:e] = fft.dct(logmel, type=2, axis=1, norm="ortho")[:, 1:20]
    return FrameFeatures(db=db, peak_db=peak, mfcc=mfcc)


def speech_level(db) -> float:
    """p90 of frame energies: robust "how loud is speech" without being the peak."""
    np = _np()
    return float(np.percentile(db, 90))


def find_pauses(
    db, *, below_db: float = PAUSE_BELOW_DB, min_len: float = PAUSE_MIN_S, level: Optional[float] = None,
) -> list[Pause]:
    """Runs of >= ``min_len`` seconds where energy is ``below_db`` under the speech level."""
    np = _np()
    level = speech_level(db) if level is None else level
    quiet = np.asarray(db) < (level - below_db)
    pauses: list[Pause] = []
    n = len(quiet)
    min_frames = max(1, int(round(min_len / FRAME_S)))
    i = 0
    while i < n:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < n and quiet[j]:
            j += 1
        if j - i >= min_frames:
            # Frame k is centred at k*hop + win/2; the run covers [centre_i - hop/2, centre_(j-1) + hop/2].
            start = (i * FRAME_HOP + FRAME_WIN / 2) / SAMPLE_RATE - FRAME_S / 2
            end = ((j - 1) * FRAME_HOP + FRAME_WIN / 2) / SAMPLE_RATE + FRAME_S / 2
            pauses.append(Pause(max(0.0, start), end))
        i = j
    return pauses


def detect_pauses(db, duration: float) -> tuple[list[Pause], float, float]:
    """(pauses, speech_level, below_db actually used).

    Starts strict (30 dB). Noisy sources have a floor closer than 30 dB to speech, so
    no frame ever qualifies; relax stepwise until there is roughly one pause per 8 s.
    """
    level = speech_level(db)
    want = max(1, int(duration / 8.0))
    best: tuple[list[Pause], float] = ([], PAUSE_BELOW_DB)
    for below in (PAUSE_BELOW_DB, *PAUSE_FALLBACK_DB):
        pauses = find_pauses(db, below_db=below, level=level)
        if len(pauses) > len(best[0]):
            best = (pauses, below)
        if len(pauses) >= want:
            return pauses, level, below
    return best[0], level, best[1]


# ---------------------------------------------------------------------------
# speaker gate: segment-level MFCC (+CMN) embeddings, clustered
# ---------------------------------------------------------------------------

SEG_S = 2.0
SEG_HOP_S = 1.0
MIN_SEG_SPEECH_FRAMES = 60


def _coherence(starts: Sequence[float], labels, lag_s: float) -> float:
    """How much more temporally consistent a labelling is than chance, in [-inf, 1].

    Compares segments one non-overlapping step apart. Real speakers hold the floor for
    seconds, so their labels persist (observed flip rate far under the random
    expectation ``1 - sum(p^2)``); a split of one speaker's phonetic/loudness variation
    flips almost like a coin. This is what tells "two people" from "one person, two
    kinds of sound", which no merge-height or silhouette rule managed on real audio.
    """
    np = _np()
    by_start = {round(t, 3): int(l) for t, l in zip(starts, labels)}
    flips = pairs = 0
    for t, l in by_start.items():
        other = by_start.get(round(t + lag_s, 3))
        if other is None:
            continue
        pairs += 1
        flips += int(other != l)
    if pairs < 8:
        return -1.0
    counts = np.bincount(np.asarray(labels, dtype=int))
    p = counts[counts > 0] / len(labels)
    baseline = 1.0 - float(np.sum(p ** 2))
    return 1.0 - (flips / pairs) / max(baseline, 1e-9)


def speaker_map(
    feats: FrameFeatures, *, speech_thr_db: float, seg_s: float = SEG_S, hop_s: float = SEG_HOP_S,
    max_clusters: int = 4, min_coherence: float = 0.45,
) -> Optional[SpeakerMap]:
    """Cluster ~2 s segments by speaker and return the dominant one.

    Frame-level clustering does not work (it clusters phonemes, not people), so each
    embedding summarises a whole segment: the mean cepstrum over its speech frames.
    Cepstral mean normalisation is applied twice, and both are needed: frame cepstra
    have the file's mean speech cepstrum removed (the mic/room transfer function sits
    in every frame), then the segment embeddings are standardised across segments
    before clustering. Raw cepstra without that mostly encode the mic.

    Clustering is Ward linkage (average linkage collapses to one giant cluster plus
    outliers on real audio). The number of clusters is the largest k <= max_clusters
    whose labelling is temporally coherent (see ``_coherence``); if none is, the
    source is treated as one speaker. Returns None when there is too little speech.
    """
    np = _np()
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
    except ImportError as exc:  # pragma: no cover
        raise _missing("scipy") from exc

    db, mfcc = np.asarray(feats.db), np.asarray(feats.mfcc)
    speech = db >= speech_thr_db
    if speech.sum() < 2 * MIN_SEG_SPEECH_FRAMES:
        return None
    cmn = mfcc - mfcc[speech].mean(axis=0, keepdims=True)

    per_sec = int(round(1.0 / FRAME_S))
    seg_len, hop = int(seg_s * per_sec), int(hop_s * per_sec)
    spans, embs, speech_secs = [], [], []
    for s in range(0, max(1, len(db) - seg_len + 1), hop):
        e = min(s + seg_len, len(db))
        mask = speech[s:e]
        if mask.sum() < MIN_SEG_SPEECH_FRAMES:
            continue
        embs.append(cmn[s:e][mask].mean(axis=0))
        spans.append((s * FRAME_S, e * FRAME_S))
        speech_secs.append(float(mask.sum()) * FRAME_S)
    if len(embs) < 4:
        return None

    X = np.asarray(embs, dtype=np.float64)
    X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-6)
    starts = [a for a, _ in spans]
    labels = np.ones(len(X), dtype=int)
    if len(X) >= 12:
        Z = linkage(X, method="ward")
        for k in range(min(max_clusters, len(X) // 3), 1, -1):
            cand = fcluster(Z, t=k, criterion="maxclust")
            if _coherence(starts, cand, seg_s) >= min_coherence:
                labels = cand
                break

    time_by_label: dict[int, float] = {}
    for lab, secs in zip(labels, speech_secs):
        time_by_label[int(lab)] = time_by_label.get(int(lab), 0.0) + secs
    dominant = max(time_by_label, key=time_by_label.get)
    segments = [SpeakerSegment(a, b, int(lab), sec) for (a, b), lab, sec in zip(spans, labels, speech_secs)]
    return SpeakerMap(segments=segments, dominant=int(dominant), n_clusters=len(time_by_label))


# ---------------------------------------------------------------------------
# candidate windows
# ---------------------------------------------------------------------------


@dataclass
class _Boundary:
    gap: int          # index of the word that follows the gap (0..n)
    start_cut: float  # where a clip STARTING at this gap begins
    end_cut: float    # where a clip ENDING at this gap ends
    pause: Pause


def _boundaries(words: Sequence[Word], pauses: Sequence[Pause], duration: float) -> list[_Boundary]:
    """Every word gap that coincides with an energy pause, with the cut points to use."""
    if not pauses:
        return []
    starts = [p.start for p in pauses]
    out: list[_Boundary] = []
    n = len(words)
    for g in range(n + 1):
        lo = words[g - 1].end - BOUNDARY_TOL_S if g > 0 else 0.0
        hi = words[g].start + BOUNDARY_TOL_S if g < n else duration + 1.0
        # Pauses whose start <= hi; walk back while they still reach lo.
        idx = bisect.bisect_right(starts, hi) - 1
        best: Optional[Pause] = None
        while idx >= 0 and pauses[idx].end >= lo:
            p = pauses[idx]
            if best is None or p.length > best.length:
                best = p
            idx -= 1
        if best is None:
            continue
        half = best.length / 2.0
        out.append(_Boundary(
            gap=g,
            start_cut=max(0.0, best.end - min(LEAD_S, half)),
            end_cut=min(duration, best.start + min(TRAIL_S, half)),
            pause=best,
        ))
    return out


def _is_filler(w: Word) -> bool:
    return w.bare in FILLERS


def _edge_ok(w: Word, *, closing: bool = False) -> bool:
    """A clip may not begin/end on a filler, a low-confidence word, or a cut-off fragment.

    ``closing`` additionally refuses a final word that grammatically needs a
    follower ("... that would have"), unless whisper punctuated it as a sentence end.
    Stage 1 does not use it (see ``score_windows``: whole-file word boundaries are not
    trustworthy enough to veto on); ``verify_markers`` applies it to the clip's own
    re-transcription, which is.
    """
    if _is_filler(w) or w.prob < MIN_WORD_PROB:
        return False
    tail = w.word.rstrip()
    if tail.endswith(("-", "—")):
        return False  # "wan-" style truncation
    if closing and w.bare in DANGLING and not tail.endswith(SENTENCE_END):
        return False
    return bool(w.bare)


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else float(x)


def _pause_signal(length: float) -> float:
    """How sentence-like a pause is: 0 under ~120 ms, 1 from ~520 ms."""
    return _clip01((length - 0.12) / 0.40)


def _sentence_score(words: Sequence[Word], i: int, j: int, lead_pause: float, tail_pause: float) -> float:
    """1.0 when the clip opens on a sentence start and closes on a sentence end.

    Evidence is whisper's punctuation when it has any, else the pause length at the
    cut (a long pause is a sentence/clause break; a 100 ms dip is just a breath).
    """
    prev = words[i - 1].word.rstrip() if i > 0 else ""
    start = 1.0 if prev.endswith(SENTENCE_END) else max(_pause_signal(lead_pause), 0.7 if i == 0 else 0.0)
    tail = words[j - 1].word.rstrip()
    if tail.endswith(SENTENCE_END):
        end = 1.0
    elif tail.endswith(CLAUSE_END):
        end = max(0.4, _pause_signal(tail_pause))
    else:
        end = _pause_signal(tail_pause)
        if words[j - 1].bare in DANGLING:
            end = min(end, 0.2)  # "... and" / "... would have": probably a mis-timed word or a real cut
    if j == len(words):
        end = max(end, 0.7)  # the source itself ends here: that is a real stop, not a cut
    return 0.5 * start + 0.5 * end


def combined_score(breakdown: dict[str, float], clip_frac: float, other_speaker_frac: float) -> float:
    """Weighted sum of the breakdown, then the clipping and foreign-speech multipliers."""
    score = sum(WEIGHTS[k] * breakdown.get(k, 0.0) for k in WEIGHTS)
    score *= 1.0 - 0.5 * _clip01(clip_frac / 0.01)
    score *= 1.0 - 0.8 * _clip01(other_speaker_frac / 0.5)
    score *= 0.6 + 0.4 * breakdown.get("voice", 1.0)  # ECAPA one-voice evidence, when the gate ran
    return score


def score_windows(
    audio,
    words: Sequence[Word],
    pauses: Sequence[Pause],
    feats: FrameFeatures,
    *,
    speakers: Optional[SpeakerMap] = None,
    speech_thr_db: Optional[float] = None,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> list[Marker]:
    """Enumerate and score every admissible window; ok windows first, best first."""
    np = _np()
    audio = np.asarray(audio)
    duration = len(audio) / SAMPLE_RATE
    db = np.asarray(feats.db)
    level = speech_level(db)
    thr = speech_thr_db if speech_thr_db is not None else level - PAUSE_BELOW_DB
    n = len(words)
    if n == 0:
        return []

    spans: list[tuple] = []  # (first_word, last_word_excl, start_cut, end_cut, lead_pause, tail_pause)
    if duration < min_seconds:
        spans.append((0, n, 0.0, duration, 0.0, 0.0))  # short source: the whole clip is all there is
    else:
        bounds = _boundaries(words, pauses, duration)
        for bi, b in enumerate(bounds):
            if b.gap >= n or not _edge_ok(words[b.gap]):
                continue
            for e in bounds[bi + 1:]:
                length = e.end_cut - b.start_cut
                if length > max_seconds:
                    break
                if length < min_seconds or e.gap <= b.gap or not _edge_ok(words[e.gap - 1]):
                    continue
                spans.append((b.gap, e.gap, b.start_cut, e.end_cut, b.pause.length, e.pause.length))

    markers = [
        score_span(audio, words, feats, gi, gj, cs, ce, speakers=speakers, speech_thr_db=thr,
                   min_seconds=min_seconds, max_seconds=max_seconds, lead_pause=lp, tail_pause=tp)
        for gi, gj, cs, ce, lp, tp in spans
    ]
    markers.sort(key=lambda m: (not m.ok, -m.score))
    return markers


def score_span(
    audio, words: Sequence[Word], feats: FrameFeatures, gi: int, gj: int, cs: float, ce: float,
    *, speakers: Optional[SpeakerMap] = None, speech_thr_db: Optional[float] = None,
    min_seconds: float = DEFAULT_MIN_SECONDS, max_seconds: float = DEFAULT_MAX_SECONDS,
    lead_pause: float = 0.0, tail_pause: float = 0.0,
) -> Marker:
    """Score words[gi:gj] cut at [cs, ce]; see WEIGHTS / the gate constants."""
    np = _np()
    db = np.asarray(feats.db)
    peak = np.asarray(feats.peak_db)
    level = speech_level(db)
    thr = speech_thr_db if speech_thr_db is not None else level - PAUSE_BELOW_DB
    global_floor = float(np.percentile(db, 10))

    wsel = list(words[gi:gj])
    f0 = int(cs / FRAME_S)
    f1 = max(f0 + 1, int(ce / FRAME_S))
    dbw, pkw = db[f0:f1], peak[f0:f1]
    voiced = dbw >= thr
    voiced_frac = float(voiced.mean()) if len(dbw) else 0.0
    speech_db = float(np.median(dbw[voiced])) if voiced.any() else level
    quiet = ~voiced
    floor_db = float(np.median(dbw[quiet])) if quiet.sum() >= 5 else global_floor
    rel = floor_db - speech_db  # negative; more negative = cleaner gaps
    snr_db = -rel

    peak_gap = float(np.median(pkw[quiet])) if quiet.sum() >= 5 else 10.0
    energy_pen = _clip01((rel + 38.0) / 20.0)
    # Tonal (music/hum) gaps only count when they are audible at all, not -70 dB hiss.
    tonal_pen = _clip01((peak_gap - 17.0) / 10.0) * _clip01((rel + 55.0) / 15.0)
    background = 1.0 - max(energy_pen, tonal_pen)

    mean_prob = float(np.mean([w.prob for w in wsel])) if wsel else 0.0
    worst = min(wsel, key=lambda w: w.prob) if wsel else None
    fillers = sum(1 for w in wsel if _is_filler(w))
    seg = np.asarray(audio)[int(cs * SAMPLE_RATE):int(ce * SAMPLE_RATE)]
    clip_frac = float(np.mean(np.abs(seg) >= 0.97)) if len(seg) else 0.0
    other = speakers.other_fraction(cs, ce) if speakers is not None else 0.0
    dur = ce - cs
    # Energy alone calls laughter, gunshots and crowd noise "voiced". Whisper finds no words
    # in them, so a window with few transcribed words per second is not clean target speech.
    word_rate = len(wsel) / dur if dur > 0 else 0.0

    breakdown = {
        "clarity": _clip01((mean_prob - 0.6) / 0.35),
        "sentence": _sentence_score(words, gi, gj, lead_pause, tail_pause) if wsel else 0.0,
        "snr": _clip01((snr_db - 20.0) / 25.0),
        "background": background,
        "density": _clip01((voiced_frac - 0.5) / 0.35) * _clip01(word_rate / 2.0),
        "filler": max(0.0, 1.0 - 0.34 * fillers),
        "duration": _clip01((dur - min_seconds) / max(max_seconds - min_seconds, 1e-6)),
        "speaker": _clip01(1.0 - other / 0.15),
    }
    score = combined_score(breakdown, clip_frac, other)
    breakdown["clipping"] = 1.0 - _clip01(clip_frac / 0.01)

    reasons: list[str] = []
    if not wsel:
        reasons.append("no words")
    if other > MAX_OTHER_SPEAKER_FRAC:
        reasons.append(f"other speaker {other:.0%}")
    if fillers > MAX_FILLERS:
        reasons.append(f"{fillers} fillers")
    if clip_frac > MAX_CLIP_FRAC:
        reasons.append(f"clipping {clip_frac:.2%}")
    if worst is not None and worst.prob < MIN_ANY_WORD_PROB:
        reasons.append(f"garbled word '{worst.bare}' ({worst.prob:.2f})")
    if wsel and mean_prob < MIN_MEAN_PROB:
        reasons.append(f"low word confidence {mean_prob:.2f}")
    if background < MIN_BACKGROUND:
        reasons.append(f"noisy/musical gaps (bg {background:.2f})")
    if voiced_frac < MIN_DENSITY_FRAC:
        reasons.append(f"sparse speech {voiced_frac:.0%}")
    if wsel and word_rate < MIN_WORDS_PER_SEC:
        reasons.append(f"few words for the audio ({word_rate:.1f}/s): effects or non-speech")
    if score < MIN_OK_SCORE:
        reasons.append(f"score {score:.2f} < {MIN_OK_SCORE}")

    return Marker(
        start=cs, end=ce, text=words_text(wsel), score=float(score), breakdown=breakdown,
        metrics={
            "snr_db": snr_db, "voiced_frac": voiced_frac, "mean_prob": mean_prob, "fillers": float(fillers),
            "clip_frac": clip_frac, "other_speaker_frac": other, "gap_peak_db": peak_gap,
            "lead_pause": lead_pause, "tail_pause": tail_pause, "word_rate": word_rate,
        },
        ok=not reasons, reasons=reasons, first_word=gi, last_word=gj,
    )


def _verified(m: Marker) -> bool:
    return m.metrics.get("verified", 0.0) > 0.0


def select_top(markers: Sequence[Marker], k: int = 5, *, include_rejected: bool = False) -> list[Marker]:
    """Best ``k`` mutually non-overlapping markers (ok ones only unless asked).

    Verified markers (re-judged against their own clip transcript) rank ahead of
    unverified ones: a stage-1 score of 0.98 that never met the clip-transcript checks
    (dangling end word, filler, transcript agreement) must not outrank a window that did.
    """
    pool = [m for m in markers if m.ok or include_rejected]
    pool.sort(key=lambda m: (not m.ok, not _verified(m), -m.score))
    chosen: list[Marker] = []
    for m in pool:
        if all(m.end <= c.start or m.start >= c.end for c in chosen):
            chosen.append(m)
            if len(chosen) >= k:
                break
    return chosen


def marker_for_span(analysis: Analysis, start: float, end: float) -> Marker:
    """Score an explicit user-chosen span (``--start/--end``) like any candidate."""
    words = analysis.words
    sel = [i for i, w in enumerate(words) if start <= (w.start + w.end) / 2 <= end]
    if not sel:
        return Marker(start, end, "", 0.0, {}, ok=False, reasons=["no words in span"])
    def pause_near(t: float) -> float:
        near = [p.length for p in analysis.pauses if p.start - BOUNDARY_TOL_S <= t <= p.end + BOUNDARY_TOL_S]
        return max(near, default=0.0)

    marker = score_span(
        analysis.audio, words, analysis.feats, sel[0], sel[-1] + 1, start, end,
        speakers=analysis.speakers, speech_thr_db=analysis.speech_level_db - analysis.pause_below_db,
        lead_pause=pause_near(start), tail_pause=pause_near(end),
    )
    if analysis.voices is not None:
        apply_voice_gate([marker], analysis.voices)
    return marker


# ---------------------------------------------------------------------------
# top-level analysis
# ---------------------------------------------------------------------------


def apply_voice_gate(markers: list[Marker], voices) -> None:
    """Judge every window with the ECAPA one-voice gate (see :mod:`voxxy.speakergate`), in place.

    Failing windows are rejected with a reason; passing ones get a ``voice`` breakdown value
    that scales the score, so the cleanest single voice outranks one that merely passes.
    """
    from voxxy import speakergate as sg

    for m in markers:
        coh = voices.coherence(m.start, m.end)
        dom = voices.dominance(m.start, m.end)
        if coh is not None:
            m.metrics.update({"coherence": coh["mean"], "coherence_p10": coh["p10"]})
        if dom is not None:
            m.metrics["dominance"] = dom
        m.metrics["voice_gated"] = 1.0
        reasons = sg.verdict(coh, dom)
        m.breakdown["voice"] = sg.breakdown_value(coh, dom)
        if reasons:
            m.reasons.extend(r for r in reasons if r not in m.reasons)
            m.ok = False
        m.score = combined_score(
            m.breakdown, m.metrics.get("clip_frac", 0.0), m.metrics.get("other_speaker_frac", 0.0),
        )
    markers.sort(key=lambda m: (not m.ok, -m.score))


def analyze(
    path: Path,
    *,
    min_seconds: float = DEFAULT_MIN_SECONDS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    words: Optional[list[Word]] = None,
    use_cache: bool = True,
    verify: bool = True,
    verify_top: int = 8,
    voice_gate: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> Analysis:
    """Decode, transcribe, find pauses, gate speakers, score every window, verify the finalists.

    ``voice_gate`` adds the ECAPA one-voice check (needs uv; degrades with a note, never fails).
    """
    say = progress or (lambda _m: None)
    say("decoding audio")
    audio = decode_audio(path)
    duration = len(audio) / SAMPLE_RATE
    if words is None:
        say(f"transcribing {duration:.0f}s with {whisper_model_name()}")
        words = transcribe_file(path, audio, use_cache=use_cache)
    say("analysing pauses and speakers")
    feats = frame_features(audio)
    pauses, level, below = detect_pauses(feats.db, duration)
    spk = speaker_map(feats, speech_thr_db=level - below)
    markers = score_windows(
        audio, words, pauses, feats, speakers=spk, speech_thr_db=level - below,
        min_seconds=min_seconds, max_seconds=max_seconds,
    )
    analysis = Analysis(
        path=str(path), duration=duration, words=words, pauses=pauses, speech_level_db=level,
        pause_below_db=below, speakers=spk, markers=markers, audio=audio, feats=feats,
    )
    if voice_gate and markers and os.environ.get("VOXXY_VOICE_GATE", "").lower() not in ("0", "off", "false", "no"):
        from voxxy import speakergate as sg

        try:
            analysis.voices = sg.embed_source(path, use_cache=use_cache, progress=progress)
            apply_voice_gate(markers, analysis.voices)
        except sg.GateUnavailable as exc:
            analysis.voice_gate_note = str(exc)
            say(f"ECAPA voice gate skipped: {exc}")
    if verify and markers:
        verify_markers(analysis, n=verify_top, progress=progress)
    return analysis


def markers_payload(analysis: Analysis, markers: Sequence[Marker]) -> dict[str, Any]:
    """JSON-serialisable markers file (what `--markers FILE` / workdir markers.json hold)."""
    spk = analysis.speakers
    return {
        "source": analysis.path,
        "duration": round(analysis.duration, 3),
        "words": len(analysis.words),
        "pauses": len(analysis.pauses),
        "pause_below_db": analysis.pause_below_db,
        "speakers": None if spk is None else {"clusters": spk.n_clusters, "dominant": spk.dominant},
        "voice_gate": "ecapa" if analysis.voices is not None else (analysis.voice_gate_note or "off"),
        "candidates": [m.to_dict() for m in markers],
    }


# ---------------------------------------------------------------------------
# stage 2: verify the finalists against their own re-transcription
# ---------------------------------------------------------------------------

AGREEMENT_FLOOR = 0.70   # window words vs. words heard in the exported clip
TERMINAL = (".", "?", "!")


def _norm_words(words: Sequence[Word]) -> list[str]:
    return [w.bare for w in words if w.bare]


def agreement(a: Sequence[Word], b: Sequence[Word]) -> float:
    """Word-level similarity in [0, 1] between two transcripts of the same audio."""
    import difflib

    wa, wb = _norm_words(a), _norm_words(b)
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb).ratio()


def apply_clip_transcript(marker: Marker, clip_words: Sequence[Word], window_words: Sequence[Word]) -> Marker:
    """Re-judge a candidate using what whisper hears in the exported clip itself.

    Transcribing the clip in isolation is both more accurate and punctuated (the
    whole-file pass often is not), so it settles the questions stage 1 can only
    guess at: does the clip end on a sentence, on a dangling word, on a filler? Does
    it say the same thing the whole-file pass thought it did? ``marker.text`` becomes
    the clip transcript, which is the exact string used as prompt_text.
    """
    if not clip_words:
        marker.ok = False
        marker.reasons.append("clip re-transcription is empty")
        return marker
    first, last = clip_words[0], clip_words[-1]
    tail = last.word.rstrip()
    agree = agreement(window_words, clip_words)
    fillers = sum(1 for w in clip_words if _is_filler(w))
    mean_prob = sum(w.prob for w in clip_words) / len(clip_words)

    b = marker.breakdown
    # Whisper capitalises the first word and punctuates the last word of ANY clip, so
    # neither is evidence of a sentence boundary on its own: it only lifts the
    # pause-length evidence from stage 1 (a long pause is the reliable cue).
    start_ev = _pause_signal(marker.metrics.get("lead_pause", 0.0))
    end_ev = _pause_signal(marker.metrics.get("tail_pause", 0.0))
    if tail.endswith(TERMINAL) or tail.endswith("…"):
        end_ev = max(end_ev, 0.6)
    elif tail.endswith(CLAUSE_END):
        end_ev = max(end_ev, 0.3)
    b["sentence"] = 0.5 * max(start_ev, 0.0) + 0.5 * end_ev
    b["clarity"] = _clip01((mean_prob - 0.6) / 0.35)
    b["filler"] = max(0.0, 1.0 - 0.34 * fillers)
    b["agreement"] = agree
    marker.metrics.update({"mean_prob": mean_prob, "fillers": float(fillers), "agreement": agree, "verified": 1.0})
    marker.score = combined_score(b, marker.metrics.get("clip_frac", 0.0), marker.metrics.get("other_speaker_frac", 0.0))
    marker.score *= 0.5 + 0.5 * agree

    # Drop the stage-1 verdicts that the clip transcript supersedes; keep the audio-derived ones.
    reasons = [r for r in marker.reasons if not r.startswith(("score ", "low word", "garbled")) and "fillers" not in r]
    worst = min(clip_words, key=lambda w: w.prob)
    if not _edge_ok(first):
        reasons.append(f"clip starts on fragment/filler '{first.bare}'")
    if not _edge_ok(last, closing=True):
        reasons.append(f"clip ends on fragment/filler/dangling word '{last.bare}'")
    if agree < AGREEMENT_FLOOR:
        reasons.append(f"unstable transcript (agreement {agree:.2f})")
    if worst.prob < MIN_ANY_WORD_PROB:
        reasons.append(f"garbled word '{worst.bare}' ({worst.prob:.2f})")
    if mean_prob < MIN_MEAN_PROB:
        reasons.append(f"low word confidence {mean_prob:.2f}")
    if fillers > MAX_FILLERS:
        reasons.append(f"{fillers} fillers")
    if marker.score < MIN_OK_SCORE:
        reasons.append(f"score {marker.score:.2f} < {MIN_OK_SCORE}")
    marker.reasons = reasons
    marker.ok = not reasons
    marker.text = words_text(clip_words)
    return marker


def _overlap_frac(a: Marker, b: Marker) -> float:
    """Shared time as a fraction of the shorter window (0 = disjoint, 1 = one contains the other)."""
    inter = min(a.end, b.end) - max(a.start, b.start)
    return max(0.0, inter) / max(min(a.duration, b.duration), 1e-9)


MAX_VERIFY_OVERLAP = 0.5


def verify_markers(analysis: Analysis, *, n: int = 10, progress: Optional[Callable[[str], None]] = None) -> None:
    """Export and re-transcribe the best ``n`` distinct windows, then re-rank in place.

    Cost is n short transcriptions (~1-2 s each on CPU), not another pass over the
    whole source. A window that shares more than half its time with an already-chosen
    one is skipped (shifting both edges by a word is the same clip), so the n slots
    cover different regions. Windows stage 1 already rejected are not worth a
    transcription.
    """
    import tempfile

    pool = [
        m for m in analysis.markers
        if m.ok and m.metrics.get("other_speaker_frac", 0.0) <= MAX_OTHER_SPEAKER_FRAC
    ]
    chosen: list[Marker] = []
    for m in sorted(pool, key=lambda x: -x.score):
        if all(_overlap_frac(m, c) <= MAX_VERIFY_OVERLAP for c in chosen):
            chosen.append(m)
        if len(chosen) >= n:
            break
    with tempfile.TemporaryDirectory(prefix="voxxy-verify-") as tmp:
        for i, m in enumerate(chosen, 1):
            if progress:
                progress(f"verifying candidate {i}/{len(chosen)}")
            clip = Path(tmp) / f"c{i}.wav"
            try:
                export_clip(Path(analysis.path), clip, m.start, m.end)
                clip_words = transcribe(decode_audio(clip), beam_size=1)
            except CloneDepsMissing:
                raise
            except Exception:  # noqa: BLE001 - an unverifiable candidate keeps its stage-1 score
                continue
            apply_clip_transcript(m, clip_words, analysis.words[m.first_word:m.last_word])
    analysis.markers.sort(key=lambda m: (not m.ok, not _verified(m), -m.score))


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def export_filter(duration: float) -> str:
    """The measured-good clean-up chain (see module docstring)."""
    fade_out_at = max(0.0, duration - 0.03)
    return (
        "highpass=f=55,"
        "afade=t=in:d=0.02,"
        f"afade=t=out:st={fade_out_at:.3f}:d=0.03,"
        "loudnorm=I=-20:TP=-1.5:LRA=11"
    )


def build_export_argv(src: Path, dst: Path, start: float, end: float) -> list[str]:
    """ffmpeg argv for the clean cut. Pure: tests assert on it, no subprocess.

    Always re-encodes (``-c:a pcm_s16le``): stream-copy cuts land on packet
    boundaries and truncate the clip. ``-ss/-t`` sit before ``-i`` so the cut is
    sample-accurate on a re-encode and fast on long sources.
    """
    duration = end - start
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(src), "-vn",
        "-af", export_filter(duration),
        "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
        str(dst),
    ]


def export_clip(src: Path, dst: Path, start: float, end: float) -> Path:
    """Cut ``[start, end]`` of ``src`` into a mono 24 kHz PCM16 WAV at ``dst``."""
    if end <= start:
        raise ValueError(f"empty clip window {start}-{end}")
    _require_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(build_export_argv(src, dst, start, end), capture_output=True, text=True)
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size < 1000:
        raise RuntimeError(f"ffmpeg export failed:\n{(proc.stderr or '')[-400:]}")
    return dst


def clip_prompt_text(clip: Path, fallback: str = "") -> str:
    """The exact words audible in the exported clip (re-transcribed), else ``fallback``.

    Re-transcribing, rather than reusing the window's words, is deliberate: the
    export fades its edges and may include a little more or less than whisper's
    word boundaries, and prompt_text must match what the model hears or
    continuation mode drifts.
    """
    try:
        text = words_text(transcribe(decode_audio(clip)))
    except CloneDepsMissing:
        raise
    except Exception:  # noqa: BLE001 - the window words are a close second
        return fallback
    return text or fallback
