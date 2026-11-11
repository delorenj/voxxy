"""Make ``import app`` work under plain ``pytest tests`` (not just ``python -m pytest``)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _wav(
    seconds: float = 2.0,
    sr: int = 16_000,
    channels: int = 1,
    subtype: str = "PCM_16",
    fmt: str = "WAV",
) -> bytes:
    """A short 220 Hz tone, encoded in memory. Shape knobs only; content is irrelevant."""
    n = int(round(seconds * sr))
    tone = (0.2 * np.sin(2 * np.pi * 220.0 * np.arange(n) / sr)).astype(np.float32)
    data = tone if channels == 1 else np.stack([tone] * channels, axis=1)
    buf = io.BytesIO()
    sf.write(buf, data, sr, format=fmt, subtype=subtype)
    return buf.getvalue()


@pytest.fixture
def make_wav():
    """``make_wav(seconds=2.0, sr=16000, channels=1, subtype="PCM_16", fmt="WAV") -> bytes``."""
    return _wav
