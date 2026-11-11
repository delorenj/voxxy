"""Per-request reference audio: decode and validate it before any engine sees it.

A caller (Phonograph) may send a speaker's own voice with a synthesis request
instead of naming a registered voice. The clip is biometric data, so core keeps
it in memory only, never writes it to disk, and never puts it (or its
transcript) in a log line. Every message raised here describes the clip's
*shape* (format, channels, seconds, bytes), never its content, so it is safe to
return to the caller and to log.

Limits (env-tunable, defaults match the Phonograph contract):

  VOX_REQUEST_REF_MAX_BYTES    decoded WAV size cap, default 2 MiB (2097152)
  VOX_REQUEST_REF_MAX_SECONDS  duration cap, default 15
  VOX_REQUEST_REF_MIN_SECONDS  duration floor, default 0.5 (rejects empty clips)
"""

from __future__ import annotations

import base64
import binascii
import io
import os
from dataclasses import dataclass
from typing import Optional

import soundfile as sf

REQUEST_REF_MAX_BYTES = int(os.environ.get("VOX_REQUEST_REF_MAX_BYTES", str(2 * 1024 * 1024)))
REQUEST_REF_MAX_SECONDS = float(os.environ.get("VOX_REQUEST_REF_MAX_SECONDS", "15"))
REQUEST_REF_MIN_SECONDS = float(os.environ.get("VOX_REQUEST_REF_MIN_SECONDS", "0.5"))

# base64 turns every 3 bytes into 4 characters; anything longer cannot decode
# to a clip under the byte cap, so it is refused before decoding.
REQUEST_REF_MAX_B64_CHARS = 4 * ((REQUEST_REF_MAX_BYTES + 2) // 3)

MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 48_000

# libsndfile names. WAVEX is a WAV with the extensible header (still RIFF/WAVE).
_WAV_FORMATS = frozenset({"WAV", "WAVEX"})
# Integer PCM only: float, ADPCM, mu-law and friends are refused.
_PCM_SUBTYPES = frozenset({"PCM_U8", "PCM_16", "PCM_24", "PCM_32"})


class ReferenceAudioError(ValueError):
    """The reference clip is not acceptable. The message is safe to return and to log."""


@dataclass(frozen=True, slots=True, repr=False)
class ReferenceAudio:
    """A validated per-request reference clip, held in memory only."""

    wav_bytes: bytes
    sample_rate: int
    duration_s: float
    prompt_text: Optional[str] = None

    def __repr__(self) -> str:
        # Never let the audio or its transcript reach a log line via %r.
        return (
            f"ReferenceAudio(bytes={len(self.wav_bytes)}, sample_rate={self.sample_rate}, "
            f"duration_s={self.duration_s:.2f}, prompt_text={'set' if self.prompt_text else 'none'})"
        )


def decode_reference(
    b64: str,
    *,
    declared_sample_rate: Optional[int] = None,
    prompt_text: Optional[str] = None,
) -> ReferenceAudio:
    """Decode ``reference_audio_b64`` and check it is a usable mono PCM WAV.

    ``declared_sample_rate`` is the caller's ``reference_sample_rate``. The WAV
    header is authoritative; a disagreement means the caller framed the PCM
    wrongly, so it is refused rather than guessed at.
    """
    if len(b64) > REQUEST_REF_MAX_B64_CHARS:
        raise ReferenceAudioError(
            f"reference audio is larger than {REQUEST_REF_MAX_BYTES} bytes"
        )
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ReferenceAudioError("reference_audio_b64 is not valid base64") from None
    if not raw:
        raise ReferenceAudioError("reference audio is empty")
    if len(raw) > REQUEST_REF_MAX_BYTES:
        raise ReferenceAudioError(
            f"reference audio is {len(raw)} bytes; the limit is {REQUEST_REF_MAX_BYTES}"
        )

    try:
        info = sf.info(io.BytesIO(raw))
    except Exception:  # noqa: BLE001 - libsndfile raises several types for "not audio"
        raise ReferenceAudioError("reference audio is not a readable WAV file") from None

    if info.format not in _WAV_FORMATS:
        raise ReferenceAudioError(f"reference audio must be a WAV file, got {info.format}")
    if info.subtype not in _PCM_SUBTYPES:
        raise ReferenceAudioError(
            f"reference audio must be integer PCM (PCM_16 recommended), got {info.subtype}"
        )
    if info.channels != 1:
        raise ReferenceAudioError(f"reference audio must be mono, got {info.channels} channels")

    sample_rate = int(info.samplerate)
    if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
        raise ReferenceAudioError(
            f"reference sample rate {sample_rate} Hz is outside "
            f"{MIN_SAMPLE_RATE}-{MAX_SAMPLE_RATE} Hz"
        )
    if declared_sample_rate is not None and declared_sample_rate != sample_rate:
        raise ReferenceAudioError(
            f"reference_sample_rate {declared_sample_rate} does not match the WAV header "
            f"({sample_rate} Hz)"
        )

    duration_s = info.frames / sample_rate
    if duration_s > REQUEST_REF_MAX_SECONDS:
        raise ReferenceAudioError(
            f"reference audio is {duration_s:.2f} s; the limit is {REQUEST_REF_MAX_SECONDS:g} s"
        )
    if duration_s < REQUEST_REF_MIN_SECONDS:
        raise ReferenceAudioError(
            f"reference audio is {duration_s:.2f} s; at least {REQUEST_REF_MIN_SECONDS:g} s is needed"
        )

    return ReferenceAudio(
        wav_bytes=raw,
        sample_rate=sample_rate,
        duration_s=duration_s,
        prompt_text=prompt_text,
    )


__all__ = [
    "REQUEST_REF_MAX_BYTES",
    "REQUEST_REF_MAX_SECONDS",
    "REQUEST_REF_MIN_SECONDS",
    "ReferenceAudio",
    "ReferenceAudioError",
    "decode_reference",
]
