"""VibeVoice-1.5B engine microservice — engine-container copy.

Implements the ``/v1/synthesize`` + ``/healthz`` contract defined in
``engine.contract`` so ``voxxy-core`` can route synthesis requests here via
``RemoteEngineClient``.

VibeVoice is an audio-only voice-cloning model: it does NOT use a transcript
of the reference clip. Any ``prompt_text`` in the request is silently ignored
(logged at DEBUG so operators can confirm the field is being received but not
consumed).
"""

from __future__ import annotations

import base64
import io
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import soundfile as sf
import torch
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from engine.contract import (
    EngineCapabilities,
    EngineHealth,
    EngineSynthesizeRequest,
    EngineSynthesizeResponse,
)
from engine.synth import REF_AUDIO_MAX_SECONDS, VIBEVOICE_SAMPLE_RATE, VibeVoiceSynth

logger = logging.getLogger(__name__)

_synth: VibeVoiceSynth | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global _synth
    _synth = VibeVoiceSynth()
    _synth.load()
    logger.info("vibevoice engine ready")
    yield
    logger.info("vibevoice engine shutting down")


app = FastAPI(title="vox-engine-vibevoice", version="0.1.0", lifespan=lifespan)


@app.get("/healthz", response_model=EngineHealth)
async def healthz() -> EngineHealth:
    loaded = _synth is not None and _synth._model is not None
    return EngineHealth(
        engine="vibevoice",
        ready=loaded,
        model_loaded=loaded,
        vram_used_gb=None,  # deliberately None; do not probe torch at health time
        capabilities=EngineCapabilities(
            accepts_reference_audio=True,
            needs_transcript=False,
            max_ref_seconds=REF_AUDIO_MAX_SECONDS,
            output_sample_rate=VIBEVOICE_SAMPLE_RATE,
            multi_speaker=False,  # v1; future extension
        ),
    )


@app.post("/v1/synthesize", response_model=EngineSynthesizeResponse)
async def synthesize(req: EngineSynthesizeRequest) -> Any:
    assert _synth is not None, "model not loaded"

    # VibeVoice is audio-only; transcript-guided cloning is not supported.
    if req.prompt_text:
        logger.debug(
            "prompt_text received but ignored by vibevoice engine (audio-only cloning): %r",
            req.prompt_text[:80],
        )

    ref_tmp: str | None = None

    try:
        # Decode reference audio to a temp WAV file if provided.
        if req.reference_audio_b64:
            ref_bytes = base64.b64decode(req.reference_audio_b64)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                f.write(ref_bytes)
                ref_tmp = f.name

        wav_np, sample_rate = _synth.generate(
            text=req.text,
            reference_wav_path=ref_tmp,
            cfg=req.cfg,
            steps=req.steps,
        )

    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "INVALID_INPUT", "message": str(exc)}},
        )
    except torch.cuda.OutOfMemoryError:
        logger.exception("CUDA OOM during synthesis")
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "OOM", "message": "CUDA out of memory"}},
        )
    except Exception as exc:
        logger.exception("Synthesis failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "MODEL_ERROR", "message": str(exc)}},
        )
    finally:
        # Always clean up the raw-bytes temp file regardless of outcome.
        # Note: synth.generate() independently cleans up any trimmed-ref temp
        # it creates; we only own the file we wrote here.
        if ref_tmp:
            Path(ref_tmp).unlink(missing_ok=True)

    # Encode WAV to base64 for the wire response.
    buf = io.BytesIO()
    sf.write(buf, wav_np, sample_rate, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()
    wav_b64 = base64.b64encode(wav_bytes).decode("ascii")
    duration_s = len(wav_np) / sample_rate

    return EngineSynthesizeResponse(
        wav_b64=wav_b64,
        sample_rate=sample_rate,
        engine="vibevoice",
        duration_s=duration_s,
        bytes=len(wav_bytes),
    )
