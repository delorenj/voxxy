"""Fakes for the consent gate: a scripted transcriber and a pitch-keyed speaker embedder.

The gate takes its two models through the ``Providers`` interface, so tests swap in these. The fake embedder
tells "speakers" apart by the pitch of the test tone (``make_wav(hz=...)``), so a test controls the cosine
between two clips by choosing which speaker each one is.
"""

from __future__ import annotations

import math

import numpy as np
import soundfile as sf

from app import consent as consent_mod
from app import voiceprint as vp_mod


def unit(cos_to_base: float) -> np.ndarray:
    """A unit vector whose cosine with the base vector [1, 0, 0, 0] is ``cos_to_base``."""
    return np.array([cos_to_base, math.sqrt(max(0.0, 1 - cos_to_base**2)), 0.0, 0.0], dtype=np.float32)


class FakeTranscriber:
    model_id = "fake-asr"

    def __init__(self) -> None:
        self.text = ""
        self.calls = 0
        self.fail = False

    def say(self, challenge: dict, *, sentence: bool = True, code: bool = True, extra: str = "") -> None:
        """Script what the 'person' says for ``challenge`` (the JSON the challenge route returned)."""
        self.text = " ".join(filter(None, [
            challenge["sentence"] if sentence else "Something else entirely.",
            " ".join(challenge["code"]) if code else "", extra]))

    def load(self) -> None:
        if self.fail:
            raise RuntimeError("model files missing")

    def transcribe(self, samples, sample_rate) -> str:
        self.load()  # like the real one: a model that cannot load cannot transcribe
        self.calls += 1
        return self.text


class FakeEmbedder:
    model_id = "fake-titanet"

    def __init__(self) -> None:
        # pitch (Hz, rounded to 10) -> embedding; 220 Hz is the default tone of make_wav
        self.speakers: dict[int, np.ndarray] = {220: unit(1.0)}
        self.calls = 0

    def load(self) -> None:
        pass

    def embed(self, samples, sample_rate) -> np.ndarray:
        self.calls += 1
        spectrum = np.abs(np.fft.rfft(samples))
        hz = int(round(np.argmax(spectrum) * sample_rate / len(samples) / 10.0) * 10)
        return vp_mod.normalize(self.speakers.get(hz, unit(0.0)))


def fake_load_audio(path: str) -> np.ndarray:
    """``load_audio_16k`` without ffmpeg: soundfile, mono, resampled to 16 kHz by interpolation."""
    data, sr = sf.read(path, dtype="float32")
    if data.ndim == 2:
        data = data.mean(axis=1)
    if sr != 16_000:
        n = int(len(data) * 16_000 / sr)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data).astype(np.float32)
    return data


def fake_providers() -> consent_mod.Providers:
    # speech is identity: the test tones are "speech" in full, and the real VAD would call a pure tone silence
    return consent_mod.Providers(FakeTranscriber(), FakeEmbedder(), speech=lambda samples: samples)
