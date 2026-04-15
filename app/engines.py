"""Synthesis engines with a pluggable interface and fallback orchestration.

Two engines are shipped today:

- ``VoxCPMEngine``: local VoxCPM2 via the existing ``Synth`` wrapper. Primary.
- ``ElevenLabsEngine``: remote ElevenLabs TTS. Fallback, kicks in when the
  primary OOMs, raises, or times out.

The :class:`EngineOrchestrator` is the thing ``main.py`` calls. It owns the
try-primary-then-fallback policy, records which engine actually served, and
keeps callers dumb about the retry logic. Adding a third engine later (e.g.
a second local model, or a different cloud vendor) means implementing
:class:`SynthEngine` and handing it to the orchestrator in its preferred
order.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx
import soundfile as sf

from app.synth import Synth

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SynthResult:
    wav_bytes: bytes
    sample_rate: int
    engine: str  # identifies which engine served, e.g. "voxcpm" or "elevenlabs"


class SynthEngine(Protocol):
    """Contract every synthesis engine implements.

    ``name`` surfaces in telemetry and in the ``engine`` field of the tool
    response so callers can detect when a fallback engaged. ``available()`` is
    a cheap pre-flight to skip engines that can't possibly serve (e.g. no API
    key). ``generate`` is the actual work and must raise on failure so the
    orchestrator can try the next engine.
    """

    name: str

    def available(self) -> bool: ...

    async def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
        voice_id: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> SynthResult: ...


class VoxCPMEngine:
    """Local VoxCPM2 via the existing Synth wrapper.

    Wraps the blocking generate() in ``asyncio.to_thread`` so the event loop
    stays responsive. Synth itself already handles memory containment and
    reference-audio trimming; we're just an adapter.
    """

    name = "voxcpm"

    def __init__(self, synth: Synth) -> None:
        self._synth = synth

    def available(self) -> bool:
        # Synth is always considered available once loaded; errors surface at
        # generate() time. The orchestrator handles those.
        return self._synth is not None and self._synth._model is not None

    async def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
        voice_id: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> SynthResult:
        # voice_id is the ElevenLabs-side concept; local engine ignores it.
        def _run():
            wav, sr = self._synth.generate(
                text=text,
                reference_wav_path=reference_wav_path,
                prompt_wav_path=reference_wav_path if prompt_text else None,
                prompt_text=prompt_text,
                cfg_value=cfg,
                inference_timesteps=steps,
                normalize=False,
                denoise=False,
            )
            buf = io.BytesIO()
            sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
            return buf.getvalue(), sr

        wav_bytes, sr = await asyncio.to_thread(_run)
        return SynthResult(wav_bytes=wav_bytes, sample_rate=sr, engine=self.name)


# ElevenLabs "eleven_turbo_v2_5" is the current cheap+fast model. PCM output
# gives us a WAV we can hand to the same transcoder pipeline as VoxCPM output.
# Changing the output_format to mp3 would save bandwidth at the cost of an
# extra transcode hop.
ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5")
ELEVENLABS_OUTPUT_FORMAT = os.environ.get(
    "ELEVENLABS_OUTPUT_FORMAT", "pcm_24000"
)  # pcm_24000 returns raw 16-bit PCM at 24kHz; we wrap it in WAV framing


class ElevenLabsEngine:
    """Remote ElevenLabs TTS. Activates when ELEVENLABS_API_KEY is set.

    Returns WAV bytes so the downstream transcoder doesn't need a special
    case. When ``voice_id`` is not passed, falls back to
    ``ELEVENLABS_DEFAULT_VOICE`` env var (Adam by default).
    """

    name = "elevenlabs"

    def __init__(self) -> None:
        self._api_key = os.environ.get("ELEVENLABS_API_KEY")
        self._default_voice = os.environ.get(
            "ELEVENLABS_DEFAULT_VOICE", "pNInz6obpgDQGcFmaJgB"
        )  # "Adam"
        self._timeout = float(os.environ.get("ELEVENLABS_TIMEOUT_SECONDS", "20"))

    def available(self) -> bool:
        return bool(self._api_key)

    async def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
        voice_id: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> SynthResult:
        if not self.available():
            raise RuntimeError("ELEVENLABS_API_KEY not set")

        vid = voice_id or self._default_voice
        url = f"{ELEVENLABS_API_BASE}/text-to-speech/{vid}"
        params = {"output_format": ELEVENLABS_OUTPUT_FORMAT}
        headers = {
            "xi-api-key": self._api_key,
            "accept": "audio/wav",
            "content-type": "application/json",
        }
        payload = {"text": text, "model_id": ELEVENLABS_MODEL_ID}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(url, params=params, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"elevenlabs {resp.status_code}: {resp.text[:200]}"
                )
            audio = resp.content

        # pcm_24000 is raw PCM without headers; wrap in WAV container so the
        # transcoder stays format-agnostic. Other output_formats (mp3_*) return
        # already-framed bytes and bypass this wrapping.
        if ELEVENLABS_OUTPUT_FORMAT.startswith("pcm_"):
            sr = int(ELEVENLABS_OUTPUT_FORMAT.split("_")[1])
            import numpy as np
            pcm = np.frombuffer(audio, dtype=np.int16)
            buf = io.BytesIO()
            sf.write(buf, pcm, sr, format="WAV", subtype="PCM_16")
            audio = buf.getvalue()
            return SynthResult(wav_bytes=audio, sample_rate=sr, engine=self.name)

        # Non-PCM paths: bytes are already a container; let the transcoder
        # probe. sample_rate is best-effort informational only.
        return SynthResult(wav_bytes=audio, sample_rate=0, engine=self.name)


class EngineOrchestrator:
    """Try engines in order. First success wins; log every fallback."""

    def __init__(self, engines: list[SynthEngine]) -> None:
        self._engines = engines

    async def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
        voice_id: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> SynthResult:
        last_exc: Optional[BaseException] = None
        for engine in self._engines:
            if not engine.available():
                logger.info("engine %s skipped (unavailable)", engine.name)
                continue
            try:
                result = await engine.generate(
                    text=text,
                    reference_wav_path=reference_wav_path,
                    prompt_text=prompt_text,
                    voice_id=voice_id,
                    cfg=cfg,
                    steps=steps,
                )
                logger.info("engine %s served (%d bytes)", engine.name, len(result.wav_bytes))
                return result
            except Exception as exc:  # noqa: BLE001 - deliberately broad
                last_exc = exc
                logger.warning("engine %s failed: %s", engine.name, exc)
                continue
        # All engines failed. Raise the last error for the caller to translate.
        raise RuntimeError(
            f"all synthesis engines failed; last error: {last_exc!r}"
        )
