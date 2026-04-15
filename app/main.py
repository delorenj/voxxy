"""vox-tts: universal TTS service.

- HTTP API: POST /synthesize → audio/wav
- MCP server mounted at /mcp (streamable HTTP transport) for Hermes/OpenClaw
- Voice profile CRUD at /voices
- Health check at /healthz

Environment:
  VOX_DATABASE_URL     postgres DSN (required)
  VOX_VOICES_DIR       directory holding voice WAVs (default: /data/voices)
  VOX_HF_CACHE         huggingface cache path (inherited from HF_HOME usually)
  VOX_OPTIMIZE=1       enable torch.compile (more VRAM, faster RTF)
  VOX_REF_AUDIO_MAX_SECONDS  max reference audio length (default 30)
  VOX_MAX_LEN          max generation token length (default 2048)
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import soundfile as sf
from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from app.synth import REF_AUDIO_MAX_SECONDS, Synth
from app.voices import VOICES_DIR, Voice, VoiceRepo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vox")

# ---------- globals populated in lifespan ----------
_synth: Synth | None = None
_repo: VoiceRepo | None = None


# ---------- API models ----------

class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Text to synthesize")
    voice: Optional[str] = Field(
        None,
        description="Name of a saved voice profile. Omit for voice-design mode.",
    )
    cfg: float = Field(2.0, ge=1.0, le=5.0)
    steps: int = Field(10, ge=1, le=50)
    normalize: bool = False
    denoise: bool = False


class VoiceOut(BaseModel):
    name: str
    display_name: str
    duration_s: float
    prompt_text: Optional[str] = None
    tags: list[str] = []

    @classmethod
    def from_model(cls, v: Voice) -> "VoiceOut":
        return cls(
            name=v.name,
            display_name=v.display_name,
            duration_s=v.duration_s,
            prompt_text=v.prompt_text,
            tags=v.tags,
        )


# ---------- synthesis helper ----------

async def _synthesize_bytes(
    *,
    text: str,
    voice_name: Optional[str],
    cfg: float,
    steps: int,
    normalize: bool,
    denoise: bool,
) -> bytes:
    assert _synth is not None and _repo is not None

    ref_path: Optional[str] = None
    prompt_text: Optional[str] = None
    if voice_name:
        v = await _repo.get(voice_name)
        if v is None:
            raise HTTPException(404, f"voice '{voice_name}' not found")
        if not v.abs_path.exists():
            raise HTTPException(500, f"voice file missing on disk: {v.abs_path}")
        ref_path = str(v.abs_path)
        prompt_text = v.prompt_text

    wav, sr = _synth.generate(
        text=text,
        reference_wav_path=ref_path,
        prompt_wav_path=ref_path if prompt_text else None,
        prompt_text=prompt_text,
        cfg_value=cfg,
        inference_timesteps=steps,
        normalize=normalize,
        denoise=denoise,
    )

    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf.read()


# ---------- FastMCP (defined first so we can nest its lifespan) ----------

mcp = FastMCP(name="vox", instructions=(
    "Text-to-speech tools backed by VoxCPM2. Use `speak` to synthesize audio "
    "in a named voice. Use `list_voices` to discover what voices are available."
))
mcp_app = mcp.http_app(path="/")  # mount at root of the sub-app so /mcp hits it


# ---------- lifespan (wraps MCP lifespan so its task group boots) ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _synth, _repo
    dsn = os.environ["VOX_DATABASE_URL"]
    VOICES_DIR.mkdir(parents=True, exist_ok=True)

    _repo = await VoiceRepo.connect(dsn)
    _synth = Synth()
    _synth.load()
    logger.info("vox-tts ready: voices_dir=%s ref_cap=%.0fs",
                VOICES_DIR, REF_AUDIO_MAX_SECONDS)
    try:
        async with mcp_app.lifespan(app):
            yield
    finally:
        await _repo.close()


# ---------- FastAPI ----------

app = FastAPI(title="vox-tts", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "model_loaded": _synth is not None and _synth._model is not None}


@app.post("/synthesize", responses={200: {"content": {"audio/wav": {}}}})
async def synthesize(req: SynthesizeRequest) -> Response:
    wav_bytes = await _synthesize_bytes(
        text=req.text, voice_name=req.voice,
        cfg=req.cfg, steps=req.steps,
        normalize=req.normalize, denoise=req.denoise,
    )
    return Response(content=wav_bytes, media_type="audio/wav")


@app.get("/voices", response_model=list[VoiceOut])
async def list_voices() -> list[VoiceOut]:
    assert _repo is not None
    return [VoiceOut.from_model(v) for v in await _repo.list()]


@app.get("/voices/{name}", response_model=VoiceOut)
async def get_voice(name: str) -> VoiceOut:
    assert _repo is not None
    v = await _repo.get(name)
    if v is None:
        raise HTTPException(404, f"voice '{name}' not found")
    return VoiceOut.from_model(v)


@app.post("/voices", response_model=VoiceOut)
async def create_voice(
    name: str = Form(..., description="Slug used as the API key, e.g. 'rick'"),
    display_name: str = Form(...),
    prompt_text: Optional[str] = Form(None),
    tags: Optional[str] = Form(None, description="Comma-separated tags"),
    audio: UploadFile = File(..., description="Reference audio (any format sf can read)"),
) -> VoiceOut:
    """Create or replace a voice profile.

    Uploaded audio is trimmed to REF_AUDIO_MAX_SECONDS and downmixed to mono
    before being stored under VOICES_DIR as <name>.wav.
    """
    assert _repo is not None

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(audio.filename or "up").suffix) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        src_tmp = tmp.name

    try:
        info = sf.info(src_tmp)
        frames = int(min(info.duration, REF_AUDIO_MAX_SECONDS) * info.samplerate)
        data, sr = sf.read(src_tmp, frames=frames)
        if data.ndim == 2:
            data = data.mean(axis=1)
        dest = VOICES_DIR / f"{name}.wav"
        sf.write(dest, data, sr, subtype="PCM_16")
        duration = min(info.duration, REF_AUDIO_MAX_SECONDS)
    finally:
        Path(src_tmp).unlink(missing_ok=True)

    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    v = await _repo.upsert(
        name=name, display_name=display_name, wav_path=dest.name,
        duration_s=duration, source_path=audio.filename,
        prompt_text=prompt_text, tags=tag_list,
    )
    return VoiceOut.from_model(v)


@app.delete("/voices/{name}")
async def delete_voice(name: str):
    assert _repo is not None
    v = await _repo.get(name)
    if v is None:
        raise HTTPException(404, f"voice '{name}' not found")
    await _repo.delete(name)
    try:
        v.abs_path.unlink(missing_ok=True)
    except Exception:
        pass
    return {"deleted": name}


# ---------- FastMCP tools ----------

@mcp.tool
async def speak(
    text: str,
    voice: Optional[str] = None,
    cfg: float = 2.0,
    steps: int = 10,
) -> dict:
    """Synthesize speech and return the audio as base64-encoded WAV bytes.

    Args:
        text: What to say.
        voice: Name of a saved voice profile. Omit for voice-design mode.
        cfg: Classifier-free guidance scale (1.0-5.0).
        steps: Diffusion inference steps (higher = better quality, slower).
    """
    import base64
    wav_bytes = await _synthesize_bytes(
        text=text, voice_name=voice,
        cfg=cfg, steps=steps,
        normalize=False, denoise=False,
    )
    return {
        "audio_wav_b64": base64.b64encode(wav_bytes).decode("ascii"),
        "voice": voice,
        "bytes": len(wav_bytes),
    }


@mcp.tool
async def list_voices_tool() -> list[dict]:
    """List all available saved voice profiles."""
    assert _repo is not None
    return [
        {
            "name": v.name,
            "display_name": v.display_name,
            "duration_s": v.duration_s,
            "tags": v.tags,
        }
        for v in await _repo.list()
    ]


# Mount FastMCP's streamable HTTP app at /mcp so clients can:
#   hermes mcp add vox --url https://vox.delo.sh/mcp
# path="/" above means the MCP endpoint sits at the mount root, not /mcp/mcp.
app.mount("/mcp", mcp_app)
