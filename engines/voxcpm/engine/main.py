"""VoxCPM2 engine microservice — engine-container copy.

Implements the ``/v1/synthesize`` + ``/healthz`` contract defined in
``engine.contract`` so ``voxxy-core`` can route synthesis requests here via
``RemoteEngineClient``.

Concurrency: ``Synth.generate`` is blocking and GPU-bound. It runs in a worker
thread (``asyncio.to_thread``), one call at a time behind ``_gpu_lock``, so the
event loop stays free and ``/healthz`` answers while a long utterance renders.
When generate ran on the loop, core's 2 s health probe timed out mid-synthesis,
core marked the engine unhealthy for 10 s, and the next requests went to
ElevenLabs in a stock voice.

Logging: never the request body, the text, the transcript or the reference
audio. Lengths and byte counts only.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import tempfile
import threading
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
from engine.synth import REF_AUDIO_MAX_SECONDS, Synth

logger = logging.getLogger(__name__)

_synth: Synth | None = None

# One generate on the GPU at a time. A threading.Lock (not asyncio.Lock) is held
# inside the worker thread for exactly as long as the GPU work runs, so a
# request cancelled while it waits can never release the lock under a running
# generate. Waiters queue on executor threads, never on the event loop.
_gpu_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    global _synth
    _synth = Synth()
    _synth.load()
    logger.info("voxcpm engine ready")
    yield
    logger.info("voxcpm engine shutting down")


app = FastAPI(title="vox-engine-voxcpm", version="0.2.0", lifespan=lifespan)


def _model_loaded() -> bool:
    return _synth is not None and _synth._model is not None


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.get("/healthz", response_model=EngineHealth)
async def healthz() -> EngineHealth:
    # Never touches the GPU lock or torch: it must answer while a synthesis runs.
    loaded = _model_loaded()
    return EngineHealth(
        engine="voxcpm",
        ready=loaded,
        model_loaded=loaded,
        vram_used_gb=None,  # deliberately None; do not probe torch at health time
        capabilities=EngineCapabilities(
            accepts_reference_audio=True,
            needs_transcript=False,
            max_ref_seconds=REF_AUDIO_MAX_SECONDS,
            output_sample_rate=16000,
            multi_speaker=False,
        ),
    )


def _write_temp_wav(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        f.write(data)
        return f.name


def _check_readable(path: str) -> None:
    """An unreadable clip is the caller's fault: 400, not a 500 from inside the model.

    A 500 would make core mark this engine unhealthy and send everyone else's
    requests to the fallback for the next 10 s.
    """
    try:
        sf.info(path)
    except Exception as exc:  # noqa: BLE001 - libsndfile raises several types
        raise ValueError(f"reference audio is not readable audio ({type(exc).__name__})") from None


def _synthesize_blocking(req: EngineSynthesizeRequest) -> EngineSynthesizeResponse:
    """Decode the reference, generate under the GPU lock, encode. Worker thread only."""
    assert _synth is not None
    ref_tmp: str | None = None
    prompt_tmp: str | None = None
    try:
        # Decode reference audio to a temp WAV file if provided.
        if req.reference_audio_b64:
            ref_bytes = base64.b64decode(req.reference_audio_b64)
            ref_tmp = _write_temp_wav(ref_bytes)
            _check_readable(ref_tmp)
            # Transcript-guided cloning: VoxCPM wants prompt_wav_path +
            # prompt_text together. Core sends one clip, so the same bytes are
            # the prompt (the model card's "Ultimate Cloning" recipe). Without
            # a transcript only the reference is used.
            if req.prompt_text:
                prompt_tmp = _write_temp_wav(ref_bytes)

        with _gpu_lock:
            wav_np, sample_rate = _synth.generate(
                text=req.text,
                reference_wav_path=ref_tmp,
                prompt_wav_path=prompt_tmp if req.prompt_text else None,
                prompt_text=req.prompt_text if prompt_tmp else None,
                cfg_value=req.cfg,
                inference_timesteps=req.steps,
                normalize=False,
                denoise=False,
                seed=req.seed,
            )
    finally:
        # Always clean up temp files regardless of outcome.
        for tmp in (ref_tmp, prompt_tmp):
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    buf = io.BytesIO()
    sf.write(buf, wav_np, sample_rate, format="WAV", subtype="PCM_16")
    wav_bytes = buf.getvalue()
    return EngineSynthesizeResponse(
        wav_b64=base64.b64encode(wav_bytes).decode("ascii"),
        sample_rate=sample_rate,
        engine="voxcpm",
        duration_s=len(wav_np) / sample_rate,
        bytes=len(wav_bytes),
    )


@app.post("/v1/synthesize", response_model=EngineSynthesizeResponse)
async def synthesize(req: EngineSynthesizeRequest) -> Any:
    if not _model_loaded():
        return _error(503, "UNAVAILABLE", "model not loaded")
    try:
        return await asyncio.to_thread(_synthesize_blocking, req)
    except ValueError as exc:
        return _error(400, "INVALID_INPUT", str(exc)[:300])
    except torch.cuda.OutOfMemoryError:
        logger.exception("CUDA OOM during synthesis (text_len=%d)", len(req.text))
        return _error(500, "OOM", "CUDA out of memory")
    except Exception as exc:  # noqa: BLE001
        # The traceback carries the exception, never the request: no text,
        # transcript or audio is part of any message raised on this path.
        logger.exception("synthesis failed (%s, text_len=%d)", type(exc).__name__, len(req.text))
        return _error(500, "MODEL_ERROR", f"{type(exc).__name__}: {str(exc)[:300]}")
