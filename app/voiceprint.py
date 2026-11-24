"""Speaker half of the consent check: does this clip sound like the same person as that one?

``Embedder`` is the provider interface (tests inject a fake). ``TitanetEmbedder`` runs NeMo TitaNet-large
through sherpa-onnx on CPU (192 numbers per clip, cosine for comparison), loaded lazily once per process.
The pure helpers here are what the gate and the account voiceprint use.

Silence comes out before anything is embedded (``speech_only``, silero VAD as bundled with faster-whisper):
the G5 spike measured 30% equal error on phone pairs without it and 14.5% with it, so it is part of the
model, not an option.

Honest limits, from Wax's own measurements (HeyMa ``wax/voiceprint.py``) and the G5 spike: the same person
scores lower across microphones and channels, and phone audio pulls different people together. So a score is
evidence, not proof, and the thresholds are in the gate's configuration, not here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Protocol

import numpy as np

logger = logging.getLogger("vox.voiceprint")

SAMPLE_RATE = 16_000
MIN_EMBED_SECONDS = 1.0


class Embedder(Protocol):
    model_id: str

    def embed(self, samples: np.ndarray, sample_rate: int) -> np.ndarray:
        """A unit-length speaker embedding of 16 kHz mono float32 ``samples``. May block; the caller threads it."""
        ...


def normalize(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float32)
    length = float(np.linalg.norm(vector))
    return vector / length if length > 0 else vector


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(normalize(a), normalize(b)))


def mean_embedding(*vectors: np.ndarray) -> np.ndarray:
    """The unit-length mean of unit-length embeddings: the fingerprint of one speaker from several clips."""
    return normalize(np.mean([normalize(v) for v in vectors], axis=0))


def speech_only(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Only the speech in 16 kHz mono float32 ``samples``, pauses and hiss cut out and the pieces joined.

    Same settings the calibration spike measured (scripts/g5_calibrate.py ``voiced``). May block; the caller
    threads it. Returns an empty array when there is no speech."""
    if sample_rate != SAMPLE_RATE:
        raise ValueError("speech_only takes 16 kHz audio")
    if len(samples) < SAMPLE_RATE // 2:
        return np.zeros(0, dtype=np.float32)
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    spans = get_speech_timestamps(
        np.ascontiguousarray(samples, dtype=np.float32),
        VadOptions(min_silence_duration_ms=300, speech_pad_ms=100, min_speech_duration_ms=250))
    if not spans:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate([samples[t["start"]:t["end"]] for t in spans]).astype(np.float32)


def load_audio_16k(path: str) -> np.ndarray:
    """Mono float32 at 16 kHz from any file ffmpeg reads. ffmpeg resamples properly and is in the image anyway."""
    from app.audio import _ffmpeg_bin

    proc = subprocess.run(
        [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", path,
         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "pipe:1"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise ValueError(f"could not decode audio: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


class TitanetEmbedder:
    def __init__(self) -> None:
        self._extractor = None
        self._lock = threading.Lock()
        self.model_id = f"sherpa-onnx:{os.environ.get('VOX_SPEAKER_MODEL_FILE', 'nemo_en_titanet_large.onnx')}"

    def load(self) -> None:
        if self._extractor is not None:
            return
        with self._lock:
            if self._extractor is not None:
                return
            import sherpa_onnx

            from app import models

            path = models.ensure_speaker_model()
            threads = int(os.environ.get("VOX_CONSENT_EMBED_THREADS", "2"))
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(path), num_threads=threads, debug=False, provider="cpu")
            if not config.validate():
                raise models.ModelUnavailable(f"sherpa-onnx rejected the speaker model {path}")
            logger.info("loading speaker model %s (cpu, %d threads)", path, threads)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        self.load()
        assert self._extractor is not None
        if len(samples) < MIN_EMBED_SECONDS * sample_rate:
            raise ValueError("clip is too short to embed")
        with self._lock:  # one stream at a time: the extractor is not documented as thread-safe
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=np.ascontiguousarray(samples, dtype=np.float32))
            stream.input_finished()
            vector = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        return normalize(vector)
