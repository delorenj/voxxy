"""Minimal in-process fake engine speaking the ``/v1/synthesize`` contract.

Used by ``scripts/verify-engine-contract.sh`` (Phase 1 verification) and by the
orchestrator fallback tests. Returns a fixed 1-second 16 kHz silence WAV so
callers can assert shape without pulling in any ML deps.

Run:
    uv run python scripts/fake-engine.py --port 18001
"""

from __future__ import annotations

import argparse
import base64
import io

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.engine_contract import (
    EngineCapabilities,
    EngineHealth,
    EngineSynthesizeRequest,
    EngineSynthesizeResponse,
)

ENGINE_NAME = "fake"
SAMPLE_RATE = 16000
DURATION_S = 1.0

app = FastAPI(title=f"{ENGINE_NAME}-engine", version="0.1.0")


def _silence_wav_bytes() -> bytes:
    samples = np.zeros(int(SAMPLE_RATE * DURATION_S), dtype=np.int16)
    buf = io.BytesIO()
    sf.write(buf, samples, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.get("/healthz", response_model=EngineHealth)
async def healthz() -> EngineHealth:
    return EngineHealth(
        engine=ENGINE_NAME,
        ready=True,
        model_loaded=True,
        vram_used_gb=0.0,
        capabilities=EngineCapabilities(
            accepts_reference_audio=True,
            needs_transcript=False,
            max_ref_seconds=30.0,
            output_sample_rate=SAMPLE_RATE,
            multi_speaker=False,
        ),
    )


@app.post("/v1/synthesize", response_model=EngineSynthesizeResponse)
async def synthesize(req: EngineSynthesizeRequest) -> EngineSynthesizeResponse:
    if not req.text.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "INVALID_INPUT", "message": "empty text"}},
        )
    wav = _silence_wav_bytes()
    return EngineSynthesizeResponse(
        wav_b64=base64.b64encode(wav).decode("ascii"),
        sample_rate=SAMPLE_RATE,
        engine=ENGINE_NAME,
        duration_s=DURATION_S,
        bytes=len(wav),
    )


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18001)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
