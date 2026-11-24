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
    hz: float = 220.0,
) -> bytes:
    """A short tone (220 Hz unless ``hz``), encoded in memory. Shape knobs only; content is irrelevant,
    except that the consent tests' fake speaker model tells "speakers" apart by the tone's pitch."""
    n = int(round(seconds * sr))
    tone = (0.2 * np.sin(2 * np.pi * hz * np.arange(n) / sr)).astype(np.float32)
    data = tone if channels == 1 else np.stack([tone] * channels, axis=1)
    buf = io.BytesIO()
    sf.write(buf, data, sr, format=fmt, subtype=subtype)
    return buf.getvalue()


@pytest.fixture
def make_wav():
    """``make_wav(seconds=2.0, sr=16000, channels=1, subtype="PCM_16", fmt="WAV", hz=220.0) -> bytes``."""
    return _wav


@pytest.fixture(autouse=True)
def _first_voice_rule_off_unless_a_test_turns_it_on(monkeypatch):
    """Production reviews every hosted account's first voice (VOX_CONSENT_REVIEW_FIRST=1). Most consent tests
    exercise the speaker rules behind it, so they run with it off; tests of the rule itself set it to 1."""
    monkeypatch.setenv("VOX_CONSENT_REVIEW_FIRST", "0")
