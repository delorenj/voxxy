"""Speaker-identity gate for reference-clip candidates: ECAPA embeddings, one voice per clip.

The MFCC speaker map in :mod:`voxxy.refclip` only knows "the most talkative voice", and on a
real scene compilation (Brian Griffin talking to Quagmire, Stewie, Peter, all voiced by one
actor) it reported a single speaker for every window. ECAPA (the same speaker model the
vox-tts scoring harness grades clones with) separates them:

  coherence   how alike the 2 s sub-windows INSIDE a candidate are (mean and 10th-percentile
              pairwise cosine). One voice talking for 13 s: mean 0.49-0.55, p10 0.34-0.44.
              A dialogue: mean 0.18-0.45, p10 <= 0.24 (measured on a Family Guy monologue vs a
              Family Guy dialogue compilation, 2026-10-28).
  dominance   how close the candidate is to the source's dominant voice (the centroid of the
              windows that agree with each other): "the voice this video is about".

Embedding runs in a separate PEP 723 script (:mod:`voxxy.ecapa_embed`, `uv run --script`) so
this CLI never depends on torch. If uv, torch or the model are unavailable the gate degrades
to ``None``: callers keep the MFCC verdict and refuse to pick unattended where it matters.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

WIN_S = 2.0
HOP_S = 1.0
MIN_WINDOWS = 3

# Gates, calibrated on the measurements in the module docstring.
COHERENCE_MIN_MEAN = 0.45
COHERENCE_MIN_P10 = 0.30
DOMINANCE_MIN = 0.20  # below this the clip is a minor voice in the source, not its subject

HELPER = Path(__file__).with_name("ecapa_embed.py")
EMBED_TIMEOUT_S = 1800


class GateUnavailable(RuntimeError):
    """The ECAPA helper could not run; the message says why."""


def _np():
    import numpy as np

    return np


@dataclass
class VoiceMap:
    starts: Any        # (N,) window start seconds
    emb: Any           # (N, D) L2-normalised
    voiced: Any        # (N,) bool: loud enough to carry speech
    win: float = WIN_S
    centroid: Any = None

    def __post_init__(self) -> None:
        if self.centroid is None:
            self.centroid = dominant_centroid(self.emb, self.voiced)

    def _select(self, start: float, end: float):
        np = _np()
        s = np.asarray(self.starts)
        return (s >= start - 0.01) & (s + self.win <= end + 0.01) & np.asarray(self.voiced)

    def coherence(self, start: float, end: float) -> Optional[dict[str, float]]:
        """Pairwise-cosine stats of the voiced sub-windows inside [start, end]; None when too few."""
        np = _np()
        m = self._select(start, end)
        if int(m.sum()) < MIN_WINDOWS:
            return None
        x = np.asarray(self.emb)[m]
        sims = (x @ x.T)[np.triu_indices(len(x), 1)]
        return {
            "mean": float(sims.mean()), "p10": float(np.percentile(sims, 10)),
            "min": float(sims.min()), "n": float(len(x)),
        }

    def dominance(self, start: float, end: float) -> Optional[float]:
        """Cosine of the span's mean embedding to the source's dominant voice."""
        np = _np()
        m = self._select(start, end)
        if not m.any() or self.centroid is None:
            return None
        c = np.asarray(self.emb)[m].mean(axis=0)
        n = np.linalg.norm(c)
        return float(c @ self.centroid / n) if n > 0 else None


def dominant_centroid(emb, voiced, iters: int = 4):
    """Centroid of the voice most windows agree on: mean, keep the upper half by similarity, repeat."""
    np = _np()
    emb = np.asarray(emb)
    voiced = np.asarray(voiced, dtype=bool)
    if emb.size == 0 or not voiced.any():
        return None
    c = emb[voiced].mean(axis=0)
    c = c / max(np.linalg.norm(c), 1e-9)
    for _ in range(iters):
        sim = emb @ c
        keep = voiced & (sim >= np.median(sim[voiced]))
        if not keep.any():
            break
        c = emb[keep].mean(axis=0)
        c = c / max(np.linalg.norm(c), 1e-9)
    return c


def _voiced_mask(rms):
    np = _np()
    rms = np.asarray(rms, dtype=float)
    if rms.size == 0:
        return rms > 0
    return rms >= max(0.003, 0.2 * float(np.percentile(rms, 75)))


def _cache_path(src: Path) -> Path:
    import hashlib

    st = src.stat()
    raw = f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|ecapa|{WIN_S}|{HOP_S}"
    key = hashlib.sha1(raw.encode()).hexdigest()[:20]
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "voxxy" / "refclip"
    return base / f"{key}.ecapa.npz"


def helper_argv(src: Path, out: Path) -> list[str]:
    return ["uv", "run", "--quiet", "--script", str(HELPER), str(src), str(out), "--win", str(WIN_S), "--hop", str(HOP_S)]


def load_voice_map(path: Path) -> VoiceMap:
    np = _np()
    with np.load(path) as d:
        return VoiceMap(starts=d["starts"], emb=d["emb"], voiced=_voiced_mask(d["rms"]), win=float(d["win"]))


def embed_source(
    src: Path, *, use_cache: bool = True, progress: Optional[Callable[[str], None]] = None,
    run: Callable[..., Any] = subprocess.run,
) -> VoiceMap:
    """ECAPA voice map of ``src`` (cached next to the transcripts). Raises :class:`GateUnavailable`."""
    cache = _cache_path(src)
    if use_cache and cache.is_file():
        try:
            return load_voice_map(cache)
        except Exception:  # noqa: BLE001 - a corrupt cache entry is just recomputed
            cache.unlink(missing_ok=True)
    if not HELPER.is_file():
        raise GateUnavailable(f"missing helper {HELPER}")
    if progress:
        progress("embedding speakers (ECAPA)")
    cache.parent.mkdir(parents=True, exist_ok=True)
    # The torch wheel the helper installs loads its own libs; a stray LD_LIBRARY_PATH (libtorch
    # from a source build) shadows them and fails with "undefined symbol".
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    with tempfile.TemporaryDirectory(prefix="voxxy-ecapa-") as tmp:
        out = Path(tmp) / "emb.npz"
        try:
            proc = run(helper_argv(src, out), capture_output=True, text=True, env=env, timeout=EMBED_TIMEOUT_S)
        except FileNotFoundError as exc:
            raise GateUnavailable("uv is not installed (needed to run the ECAPA helper)") from exc
        except subprocess.TimeoutExpired as exc:
            raise GateUnavailable(f"ECAPA helper timed out after {EMBED_TIMEOUT_S}s") from exc
        if proc.returncode != 0 or not out.is_file():
            tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise GateUnavailable(f"ECAPA helper failed: {tail[0][:200]}")
        shutil.copyfile(out, cache)
    return load_voice_map(cache)


def verdict(coh: Optional[dict[str, float]], dom: Optional[float]) -> list[str]:
    """Reasons a window fails the one-voice gate (empty = passes)."""
    reasons: list[str] = []
    if coh is None:
        return ["too little speech to verify the speaker"]
    if coh["mean"] < COHERENCE_MIN_MEAN or coh["p10"] < COHERENCE_MIN_P10:
        reasons.append(f"several voices (coherence {coh['mean']:.2f}, p10 {coh['p10']:.2f})")
    if dom is not None and dom < DOMINANCE_MIN:
        reasons.append(f"not the main voice of the source (dominance {dom:.2f})")
    return reasons


def breakdown_value(coh: Optional[dict[str, float]], dom: Optional[float]) -> float:
    """0..1 ranking value: coherent AND dominant beats merely passing."""
    if coh is None:
        return 0.0
    c = max(0.0, min(1.0, (coh["mean"] - COHERENCE_MIN_MEAN) / 0.15 + 0.5))
    d = 1.0 if dom is None else max(0.0, min(1.0, (dom - DOMINANCE_MIN) / 0.4))
    return 0.7 * c + 0.3 * d
