"""Demucs vocal-stem isolation for music-backed voice sources.

Sources with wall-to-wall background music (documentaries, narrated explainers like
explainer videos, trailers, game cutscenes) fail :mod:`voxxy.refclip`'s background and pause
gates on every window, or leak background music into the exported reference clip.

Like :mod:`voxxy.speakergate`, separation runs in a standalone PEP 723 script
(:mod:`voxxy.demucs_separate`, ``uv run --script``) so the CLI never depends on torch
or demucs. Separated vocal WAVs are cached next to transcripts and ECAPA embeddings under
``~/.cache/voxxy/refclip/`` so re-runs, ``--pick N`` and ``voice add --auto`` after
``voice candidates`` reuse the isolated stem immediately.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_MODEL = "htdemucs"
HELPER = Path(__file__).with_name("demucs_separate.py")
SEPARATE_TIMEOUT_S = 1800


class VocalIsolationUnavailable(RuntimeError):
    """The Demucs helper could not run; the message says why."""


def _cache_path(src: Path, model: str = DEFAULT_MODEL) -> Path:
    st = src.stat()
    raw = f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|demucs|{model}"
    key = hashlib.sha1(raw.encode()).hexdigest()[:20]
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "voxxy" / "refclip"
    return base / f"{key}.{model}.vocals.wav"


def cached_vocals(src: Path, model: str = DEFAULT_MODEL) -> Optional[Path]:
    """Return the cached isolated vocal WAV for ``src`` if already computed, else None."""
    try:
        cache = _cache_path(src, model=model)
        if cache.is_file() and cache.stat().st_size > 1000:
            return cache
    except OSError:
        pass
    return None


def helper_argv(src: Path, out: Path, *, model: str = DEFAULT_MODEL, device: str = "auto") -> list[str]:
    return [
        "uv", "run", "--quiet", "--script", str(HELPER),
        str(src), str(out), "--model", model, "--device", device,
    ]


def needs_vocal_isolation(analysis: Any) -> bool:
    """True when no window passed the gates and background music/noise is a culprit."""
    if not getattr(analysis, "markers", None):
        return False
    if analysis.top(1):
        return False
    if any(
        any("noisy/musical gaps" in r for r in m.reasons)
        for m in analysis.markers
    ):
        return True
    return getattr(analysis, "pause_below_db", 30.0) < 30.0


def isolate_vocals(
    src: Path,
    dst: Optional[Path] = None,
    *,
    model: str = DEFAULT_MODEL,
    device: str = "auto",
    use_cache: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    run: Callable[..., Any] = subprocess.run,
) -> Path:
    """Isolate the vocal stem of ``src`` into the refclip cache (and ``dst`` if given).

    Raises :class:`VocalIsolationUnavailable` when uv, torch, demucs or ffmpeg fails.
    """
    cache = _cache_path(src, model=model)
    if not (use_cache and cache.is_file() and cache.stat().st_size > 1000):
        if not HELPER.is_file():
            raise VocalIsolationUnavailable(f"missing helper {HELPER}")
        if progress:
            progress(f"isolating vocals (Demucs {model})")
        cache.parent.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        with tempfile.NamedTemporaryFile(dir=cache.parent, suffix=".vocals.wav", delete=False) as tf:
            tmp = Path(tf.name)
        try:
            try:
                proc = run(
                    helper_argv(src, tmp, model=model, device=device),
                    capture_output=True, text=True, timeout=SEPARATE_TIMEOUT_S, env=env,
                )
            except FileNotFoundError as exc:
                raise VocalIsolationUnavailable("uv is not installed") from exc
            except subprocess.TimeoutExpired as exc:
                raise VocalIsolationUnavailable(f"Demucs timed out after {SEPARATE_TIMEOUT_S}s") from exc
            if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
                tail = "\n".join((proc.stderr or "").strip().splitlines()[-4:]) or f"exit {proc.returncode}"
                raise VocalIsolationUnavailable(tail)
            tmp.replace(cache)
        finally:
            tmp.unlink(missing_ok=True)

    if dst is not None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.resolve() != cache.resolve():
            shutil.copy2(cache, dst)
        return dst
    return cache
