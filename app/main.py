"""vox-tts: universal TTS service.

- HTTP API: POST /synthesize → audio/wav  (engine chain, returns winning bytes)
- HTTP API: POST /synthesize-url → JSON   (engine fallback + URL return)
- HTTP API: POST /v1/audio/speech → audio  (OpenAI-compatible; GET /v1/models)
- Web app: GET / (shell) and /ui/... (static assets), GET /app-config (edition + auth flags)
- Audio cache: GET /audio/{id}.ogg        (Telegram-ready OGG/Opus)
- Sink relay: /sink/{key}/...             (play on the human's machine, not the caller's)
- MCP at /mcp/ (streamable HTTP) for Hermes/OpenClaw/Claude Code
- Voice profile CRUD at /voices
- Health check at /healthz

Every successful synthesis answers ``X-Vox-Engine`` (which engine served) and
``X-Vox-Voice-Source`` (``reference`` | ``registered`` | ``stock``: whose voice
it is). A request may carry its own reference clip (``reference_audio_b64``),
pin engines (``engines``), forbid the stock fallback (``require_reference``)
and name its ``language``; see SynthesizeRequest and app/engines.py.

Environment:
  VOX_DATABASE_URL     postgres DSN (required)
  VOX_VOICES_DIR       directory holding voice WAVs (default: /data/voices)
  VOX_AUDIO_CACHE_DIR  directory for cached OGG blobs (default: /data/audio-cache)
  VOX_AUDIO_TTL_SECONDS  cache lifetime (default: 3600)
  VOX_ENGINES          comma-separated name=url pairs for remote engine sidecars
  VOX_API_KEY          optional shared key protecting HTTP/MCP data-plane routes
  VOX_EDITION          "hosted" (default, strict: own voice or described voices only) or
                       "self" (operator's own responsibility; the web app offers clip upload)
  VOX_DESIGN_ENGINE    engine that makes described voices (default: voxcpm)
  VOX_DEFAULT_SINK     default sink name for the speak_sink MCP tool (optional)
  VOX_REF_AUDIO_MAX_SECONDS  max reference audio length kept on upload (default 30)
  VOX_REQUEST_REF_MAX_BYTES / _MAX_SECONDS / _MIN_SECONDS  per-request clip
                       limits (default 2 MiB / 15 s / 0.5 s; app/reference.py)
  VOX_REFERENCE_ENGINES  engines allowed to clone a per-request clip (default voxcpm)
  VOX_ENGINE_LANGUAGES   per-engine language override, e.g. "vibevoice=en,zh;voxcpm=*"
  VOX_OPERATOR_KEY     key for the consent review queue (/consent/reviews*); unset = 503 there
  VOX_MODELS_DIR       consent models (faster-whisper base.en, TitaNet-large); default /data/models
  VOX_CONSENT_ACCEPT / _REJECT  speaker-match thresholds (0.70 / 0.10, TitaNet-large, from the G5 spike)
  VOX_CONSENT_PHRASE_OK / _PHRASE_REVIEW  sentence score bars (0.80 / 0.50); VOX_CONSENT_MIN_SPEECH /
                       _MIN_REFERENCE_SPEECH  seconds of speech required (6 / 8)
  VOX_CONSENT_PRELOAD  load the consent models at startup (default 1)
  VOX_TERMS_VERSION    terms version stored on each consent record (default draft-1)
  ELEVENLABS_API_KEY   enables the ElevenLabs fallback engine (optional)
  ELEVENLABS_DEFAULT_VOICE  default ElevenLabs voice id (default: Adam)
"""

from __future__ import annotations

import asyncio
import io
import logging
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import soundfile as sf
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator, model_validator

from app import audio as audio_codec
from app import consent as consent_mod
from app import consent_store as consent_store_mod
from app import edition as edition_mod
from app import voices as voices_mod
from app.auth import (
    ApiKeyAuthMiddleware,
    headers_have_valid_api_key,
    operator_key_conflict,
    principal_for,
    resolve_api_key_from_env,
    resolve_operator_key_from_env,
)
from app import cache as audio_cache
from app import sink as sink_relay
from app.engines import (
    ElevenLabsEngine,
    EngineOrchestrator,
    EnginesExhaustedError,
    NoEligibleEngineError,
    PermanentEngineError,
    RemoteEngineClient,
    SynthResult,
    normalize_language,
)
from app.reference import ReferenceAudio, ReferenceAudioError, decode_reference
from app.voices import PERSONA_NONE, PERSONA_RE, VOICES_DIR, Voice, VoiceRepo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vox")

REF_AUDIO_MAX_SECONDS = float(os.environ.get("VOX_REF_AUDIO_MAX_SECONDS", "30"))

# ---------- globals populated in lifespan ----------
_repo: VoiceRepo | None = None
_engine: EngineOrchestrator | None = None
_sweep_task: asyncio.Task | None = None
# The consent gate (app/consent.py): the single entry for likeness-making. Built in lifespan over postgres.
_consent_gate: consent_mod.ConsentGate | None = None
_preload_task: asyncio.Task | None = None

# Sink listeners are plain in-process state, not a lifespan resource: a sink is
# a live attention channel, so there is nothing to open at boot and nothing
# worth persisting across a restart (see app/sink.py).
_sinks = sink_relay.SinkRegistry()


# ---------- engine chain builder ----------

def _build_engine_chain() -> list:
    """Parse VOX_ENGINES env (format 'name=url,name=url'), append ElevenLabs.

    ElevenLabsEngine.available() self-disables without ELEVENLABS_API_KEY so
    appending it unconditionally is safe and keeps the registry env-only.
    """
    spec = os.environ.get("VOX_ENGINES", "").strip()
    remotes: list = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, url = entry.split("=", 1)
        remotes.append(RemoteEngineClient(name.strip(), url.strip()))
    return remotes + [ElevenLabsEngine()]


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

    # --- per-request reference (overrides `voice` when present) ---
    reference_audio_b64: Optional[str] = Field(
        None,
        description=(
            "The speaker's own voice: base64 of a mono integer-PCM WAV, at most "
            "2 MiB decoded and 15 s. Overrides `voice` (which is then not looked "
            "up). Held in memory only, never stored or logged."
        ),
    )
    reference_sample_rate: Optional[int] = Field(
        None,
        ge=8_000,
        le=48_000,
        description="Optional; must equal the WAV header's rate when given.",
    )
    prompt_text: Optional[str] = Field(
        None,
        max_length=2000,
        description=(
            "Exact transcript of the reference clip (switches VoxCPM to "
            "continuation cloning). Only with reference_audio_b64."
        ),
    )
    require_reference: bool = Field(
        False,
        description=(
            "Fail (503) instead of falling back to a stock voice when no "
            "reference engine can serve the clip. Needs reference_audio_b64."
        ),
    )
    engines: Optional[list[str]] = Field(
        None,
        min_length=1,
        max_length=8,
        description=(
            "Allowlist of engine names (e.g. [\"voxcpm\"]). Nothing else is "
            "tried; if none can serve, 503 instead of a fallback."
        ),
    )
    language: Optional[str] = Field(
        None,
        description=(
            "Language of `text` (BCP-47, e.g. \"es\", \"en-US\"). Engines that "
            "cannot speak it are skipped (VibeVoice: en/zh only)."
        ),
    )
    seed: Optional[int] = Field(
        None,
        ge=0,
        le=2**31 - 1,
        description="Sampler seed; same seed + inputs = same audio on the same engine build.",
    )

    @field_validator("prompt_text")
    @classmethod
    def _blank_prompt_is_none(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("engines")
    @classmethod
    def _engine_names(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return None
        names = [n.strip() for n in v]
        if any(not n or len(n) > 64 for n in names):
            raise ValueError("engine names must be 1-64 characters")
        return names

    @field_validator("language")
    @classmethod
    def _language_code(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else normalize_language(v)

    @model_validator(mode="after")
    def _reference_fields_need_a_reference(self) -> "SynthesizeRequest":
        if self.reference_audio_b64 is None:
            if self.require_reference:
                raise ValueError("require_reference is true but no reference_audio_b64 was sent")
            if self.prompt_text is not None:
                raise ValueError("prompt_text is only accepted together with reference_audio_b64")
            if self.reference_sample_rate is not None:
                raise ValueError("reference_sample_rate is only accepted together with reference_audio_b64")
        return self


class SynthesizeUrlResponse(BaseModel):
    """Response shape shared by POST /synthesize-url and the speak_url MCP tool.

    Clients that deliver audio elsewhere (Telegram sendVoice, browser <audio>,
    Home Assistant media_player) can pass ``audio_url`` directly to those
    surfaces. The third party fetches the OGG/Opus payload from us.
    """
    audio_url: str
    engine: str
    duration_s: Optional[float] = None
    bytes: int
    format: str = "ogg_opus"
    voice_source: Optional[str] = None  # reference | registered | stock


class SinkPlayRequest(BaseModel):
    """Envelope pushed to a sink's listeners.

    ``audio_url`` is the only required field because the listener fetches the
    bytes itself from the public audio cache; everything else is metadata the
    listener may log or display.
    """

    audio_url: str = Field(..., description="Fetchable audio URL for the listener to play")
    text: Optional[str] = Field(None, description="What was said, for the listener's log")
    voice: Optional[str] = None
    engine: Optional[str] = None
    duration_s: Optional[float] = None
    format: str = "ogg_opus"


class SinkPlayResponse(BaseModel):
    """Result of a publish. ``delivered == 0`` means nobody was listening.

    Callers use this to decide whether to fall back to playing locally, which is
    why an unlistened sink is a 200 with a zero count rather than an error: not
    reaching a sink is an expected, recoverable outcome.
    """

    key: str
    delivered: int
    audio_url: Optional[str] = None


class SinkStatus(BaseModel):
    key: str
    listeners: int


class VoiceOut(BaseModel):
    name: str
    display_name: str
    duration_s: float
    prompt_text: Optional[str] = None
    tags: list[str] = []
    elevenlabs_voice_id: Optional[str] = None
    vibevoice_ref_path: Optional[str] = None
    vibevoice_speaker_tag: Optional[str] = None
    persona: Optional[str] = None
    # verified | pending | unchecked, from the tags; None for voices the consent gate never touched.
    consent: Optional[str] = None

    @classmethod
    def from_model(cls, v: Voice) -> "VoiceOut":
        return cls(
            name=v.name,
            display_name=v.display_name,
            duration_s=v.duration_s,
            prompt_text=v.prompt_text,
            tags=consent_mod.public_tags(v.tags),
            elevenlabs_voice_id=v.elevenlabs_voice_id,
            vibevoice_ref_path=v.vibevoice_ref_path,
            vibevoice_speaker_tag=v.vibevoice_speaker_tag,
            persona=v.persona,
            consent=consent_mod.consent_state(v.tags),
        )


class VoicePersonaIn(BaseModel):
    # A persona slug, "none" (explicitly no persona) or null (clear the association).
    persona: Optional[str] = None


# ---------- synthesis helpers ----------

async def _resolve_voice(voice_name: Optional[str]) -> tuple[
    Optional[Callable[[str], Optional[str]]], Optional[str], Optional[str],
    Optional[list[str]],
]:
    """Resolve ``voice_name`` into (reference_resolver, prompt_text, elevenlabs_voice_id, preferred_engines).

    ``reference_resolver`` is a callable ``(engine_name) -> wav_path`` so each
    engine can be pointed at a different reference clip (e.g. a de-noised
    version for VibeVoice) without plumbing engine identity through the whole
    call stack. Falls back to ``v.abs_path`` when the engine-specific file
    doesn't exist.

    ``preferred_engines`` names the engine a cloned voice should be served by
    first (currently: VibeVoice when the profile carries a vibevoice
    reference). None means configured chain order.

    Returns ``(None, None, None, None)`` for voice-design mode (no voice_name).
    """
    if not voice_name:
        return None, None, None, None
    assert _repo is not None
    v = await _repo.get(voice_name)
    if v is None:
        raise HTTPException(404, f"voice '{voice_name}' not found")
    blocked = consent_mod.synthesis_block_reason(v.tags)
    if blocked:
        raise HTTPException(403, blocked, headers={"X-Vox-Error": "consent_pending"})
    if not v.abs_path.exists():
        raise HTTPException(500, f"voice file missing on disk: {v.abs_path}")

    def resolver(engine: str) -> Optional[str]:
        p = v.ref_path_for(engine)
        return str(p) if p.exists() else str(v.abs_path)

    preferred = ["vibevoice"] if v.vibevoice_ref_path else None
    return resolver, v.prompt_text, v.elevenlabs_voice_id, preferred


async def _synthesize_wav(
    *,
    text: str,
    voice_name: Optional[str],
    cfg: float,
    steps: int,
    reference: Optional[ReferenceAudio] = None,
    require_reference: bool = False,
    engines: Optional[list[str]] = None,
    language: Optional[str] = None,
    seed: Optional[int] = None,
) -> SynthResult:
    assert _engine is not None
    if reference is not None:
        # The caller's own clip overrides the named voice entirely: the voice
        # is not even looked up, so an unknown name cannot 404 a clone request.
        resolver = prompt_text = eleven_voice_id = preferred = None
    else:
        resolver, prompt_text, eleven_voice_id, preferred = await _resolve_voice(voice_name)
    return await _engine.generate(
        text=text,
        reference_resolver=resolver,
        prompt_text=prompt_text,
        voice_id=eleven_voice_id,
        cfg=cfg,
        steps=steps,
        preferred=preferred,
        reference=reference,
        require_reference=require_reference,
        allowed_engines=engines,
        language=language,
        seed=seed,
    )


def _require_ungated_clone(request: Request) -> None:
    """403 unless this caller may turn a clip into a voice without the consent gate (per-request
    ``reference_audio_b64``, plain ``POST /voices``). The rule lives in ``consent.ungated_clone_allowed``."""
    if not consent_mod.ungated_clone_allowed(principal_for(request.headers)):
        raise HTTPException(
            403, "This key can only use voices made through the consent check; a clip sent along with a request "
                 "or uploaded as a voice is not allowed.",
            headers={"X-Vox-Error": "clone_requires_consent"})


def _request_synth_kwargs(req: SynthesizeRequest, request: Request) -> dict[str, Any]:
    """Map a request body onto ``_synthesize_wav`` kwargs, validating its clip.

    An unusable clip is a 422 raised here, before any engine is called, so
    ``require_reference`` with a bad clip is an error and never a fallback. A per-request clip is
    likeness-making outside the consent gate, so it is refused for callers who may not do that.
    """
    reference: Optional[ReferenceAudio] = None
    if req.reference_audio_b64 is not None:
        _require_ungated_clone(request)
        try:
            reference = decode_reference(
                req.reference_audio_b64,
                declared_sample_rate=req.reference_sample_rate,
                prompt_text=req.prompt_text,
            )
        except ReferenceAudioError as exc:
            raise HTTPException(
                422,
                detail=[{
                    "type": "invalid_reference",
                    "loc": ["body", "reference_audio_b64"],
                    "msg": str(exc),
                }],
                headers={"X-Vox-Error": "invalid_reference"},
            ) from None
    return {
        "text": req.text,
        "voice_name": req.voice,
        "cfg": req.cfg,
        "steps": req.steps,
        "reference": reference,
        "require_reference": req.require_reference,
        "engines": req.engines,
        "language": req.language,
        "seed": req.seed,
    }


def _http_error(exc: Exception, *, what: str) -> HTTPException:
    """Translate a synthesis failure into the HTTP answer.

    - engine 4xx: 400 (the request is wrong; no fallback was tried)
    - no engine fits the request's constraints: 503 ``no_eligible_engine``
    - a restricted request (``require_reference`` / ``engines``) whose
      engines were all down or failing: 503 ``engines_unavailable``
    - anything else: 500, as before
    """
    if isinstance(exc, PermanentEngineError):
        return HTTPException(400, str(exc))
    if isinstance(exc, NoEligibleEngineError):
        return HTTPException(503, str(exc), headers={"X-Vox-Error": "no_eligible_engine"})
    if isinstance(exc, EnginesExhaustedError) and exc.restricted:
        return HTTPException(503, str(exc), headers={"X-Vox-Error": "engines_unavailable"})
    logger.exception("%s failed", what)
    return HTTPException(500, f"synthesis failed: {exc}")


def _synth_headers(result: SynthResult) -> dict[str, str]:
    return {"X-Vox-Engine": result.engine, "X-Vox-Voice-Source": result.voice_source}


def _duration_from_wav(wav_bytes: bytes) -> Optional[float]:
    try:
        info = sf.info(io.BytesIO(wav_bytes))
        return float(info.duration)
    except Exception:
        return None


async def _synthesize_and_cache(
    *, text: str, voice_name: Optional[str], cfg: float, steps: int,
    request: Optional[Request] = None,
    **routing: Any,
) -> SynthesizeUrlResponse:
    """Run the fallback chain, transcode to OGG/Opus, cache, return a URL.

    ``routing`` carries the optional per-request kwargs of ``_synthesize_wav``
    (reference, require_reference, engines, language, seed).
    """
    result = await _synthesize_wav(
        text=text, voice_name=voice_name, cfg=cfg, steps=steps, **routing,
    )
    # Transcode off the event loop; ffmpeg blocks.
    ogg_bytes = await asyncio.to_thread(
        audio_codec.to_ogg_opus, result.wav_bytes, input_format="wav",
    )
    cache_id = audio_cache.put(ogg_bytes)
    duration = _duration_from_wav(result.wav_bytes)

    public_base = os.environ.get("VOX_PUBLIC_BASE_URL")
    if public_base:
        audio_url = f"{public_base.rstrip('/')}/audio/{cache_id}.ogg"
    elif request is not None:
        audio_url = str(request.url_for("get_audio", cache_id=cache_id))
    else:
        # MCP callers go through here. Fall back to the well-known public URL.
        audio_url = f"https://vox.delo.sh/audio/{cache_id}.ogg"

    return SynthesizeUrlResponse(
        audio_url=audio_url,
        engine=result.engine,
        duration_s=duration,
        bytes=len(ogg_bytes),
        voice_source=result.voice_source,
    )


# ---------- FastMCP (defined first so we can nest its lifespan) ----------

mcp = FastMCP(name="vox", instructions=(
    "Text-to-speech tools backed by VoxCPM2 with ElevenLabs fallback. "
    "Use `speak_url` when the audio will be consumed by another service "
    "(Telegram sendVoice, browser, Home Assistant) — returns a fetchable "
    "OGG/Opus URL. Use `speak` only when you need the raw bytes inline. "
    "Use `list_voices_tool` to discover saved voice profiles."
))
mcp_app = mcp.http_app(path="/")  # mount at root of the sub-app so /mcp hits it


# ---------- lifespan (wraps MCP lifespan so its task group boots) ----------

async def _retag_legacy_consent() -> None:
    """Web app v1 tagged every own voice ``unverified-consent``; no consent record exists for any of them.
    Hosted: they become ``consent-pending`` (not usable for speech until reviewed or re-recorded).
    Self: ``consent-unchecked-self`` (usable; the operator's responsibility). Idempotent."""
    assert _repo is not None
    new = (consent_mod.TAG_PENDING if edition_mod.is_hosted() else consent_mod.TAG_UNCHECKED_SELF)
    try:
        changed = await _repo.retag(consent_mod.TAG_LEGACY_UNVERIFIED, new)
        if changed:
            logger.info("retagged %d v1 voice(s) %s -> %s (no consent records exist for them)",
                        changed, consent_mod.TAG_LEGACY_UNVERIFIED, new)
    except Exception:  # noqa: BLE001 - the guard treats the legacy tag as pending anyway
        logger.exception("could not retag v1 consent voices")


async def _preload_consent_models() -> None:
    assert _consent_gate is not None
    try:
        await _consent_gate.ensure_ready()
        logger.info("consent models ready")
    except consent_mod.ConsentUnavailable:
        logger.warning("consent models did not load; the first consent check will retry")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _repo, _engine, _sweep_task, _consent_gate, _preload_task
    if operator_key_conflict():
        raise RuntimeError("VOX_OPERATOR_KEY must differ from VOX_API_KEY: the ordinary key would open the review queue")
    dsn = os.environ["VOX_DATABASE_URL"]
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    audio_cache.ensure_dir()

    _repo = await VoiceRepo.connect(dsn)
    _engine = EngineOrchestrator(_build_engine_chain())
    _consent_gate = consent_mod.ConsentGate(
        consent_store_mod.PgConsentStore(_repo.pool), consent_mod.default_providers())
    await _retag_legacy_consent()
    if os.environ.get("VOX_CONSENT_PRELOAD", "1").strip() not in ("0", "false", "no"):
        # Load the models in the background so the first person to record does not wait for a download.
        _preload_task = asyncio.create_task(_preload_consent_models())

    _sweep_task = asyncio.create_task(audio_cache.sweep_loop())

    logger.info(
        "vox-tts core ready: voices_dir=%s audio_cache=%s ref_cap=%.0fs engines=%s",
        VOICES_DIR, audio_cache.AUDIO_CACHE_DIR, REF_AUDIO_MAX_SECONDS,
        [e.name for e in _engine._engines],
    )
    try:
        async with mcp_app.lifespan(app):
            yield
    finally:
        for task in (_sweep_task, _preload_task):
            if task:
                task.cancel()
        await _repo.close()


# ---------- FastAPI ----------

app = FastAPI(title="vox-tts", version="0.2.0", lifespan=lifespan)
app.add_middleware(ApiKeyAuthMiddleware)


@app.exception_handler(RequestValidationError)
async def _validation_error_without_input(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 with each error's type, location and message, but not its input.

    FastAPI's default 422 echoes the offending input. On /synthesize that input
    can be caption text or a 2 MiB voice clip, which must not be bounced back
    into proxies, client logs or error trackers.
    """
    errors = [
        {"type": err.get("type"), "loc": list(err.get("loc", ())), "msg": err.get("msg")}
        for err in exc.errors()
    ]
    if request.url.path.startswith("/v1/"):
        # OpenAI-compatible routes answer in OpenAI's error envelope, 400 for a bad request.
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return _openai_error(
            400, f"{where}: {first.get('msg')}" if where else str(first.get("msg")),
            "invalid_request_error", code="invalid_request", param=where or None,
        )
    return JSONResponse(status_code=422, content={"detail": errors})


@app.get("/healthz")
async def healthz():
    assert _engine is not None
    engines = []
    for e in _engine._engines:
        ready = e.available()
        # For RemoteEngineClient, trigger an actual probe so stale cache
        # doesn't lie. ElevenLabsEngine.available() is instant.
        if hasattr(e, "refresh_health"):
            try:
                ready = await e.refresh_health()
            except Exception:
                ready = False
        engines.append({"name": e.name, "ready": ready})
    overall = any(e["ready"] for e in engines)
    return {"status": "ok" if overall else "degraded", "engines": engines}


# ---------- web app ----------
#
# A static single-page app served by the core itself: no build step, no CDN, no npm. The shell is
# GET / and its assets live under /ui/ (both public in app/auth.py; the page asks for the API key).
# GET /app-config tells the page which edition it is talking to.

WEB_DIR = Path(__file__).parent / "web"


class _NoCacheStatic(StaticFiles):
    """Static files that always revalidate (ETag still saves the bytes), so a deploy shows at once."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _public_base_url(request: Request) -> str:
    configured = os.environ.get("VOX_PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{proto}://{host}"


@app.get("/", include_in_schema=False)
async def web_shell() -> FileResponse:
    """The Voxxy web app shell."""
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "web app not bundled in this image")
    return FileResponse(
        index, media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/app-config")
async def app_config(request: Request) -> dict:
    """What the web app needs to know before it asks for anything: edition and whether a key is needed.

    Public. Never carries a secret: ``auth_required`` only says whether ``VOX_API_KEY`` is set.
    """
    edition = edition_mod.current_edition()
    hosted = edition == edition_mod.EDITION_HOSTED
    return {
        "edition": edition,
        "auth_required": resolve_api_key_from_env() is not None,
        "public_base_url": _public_base_url(request),
        "features": {
            "own_voice": True,
            "describe_voice": True,
            "upload_clip": not hosted,   # the web app offers upload only in the self edition
            "real_name_check": hosted,
            "consent_verification": True,   # ASR + speaker match (story G5); self edition only warns
            "consent_reference_upload": not hosted,  # a longer sample may be uploaded, not only recorded
            "sink": True,
        },
        # the biometric release the hosted edition shows before recording; the self edition has none
        "consent_release": {
            "version": edition_mod.CONSENT_RELEASE_VERSION,
            "text": edition_mod.CONSENT_RELEASE_TEXT,
        } if hosted else None,
    }


if (WEB_DIR / "ui").is_dir():
    app.mount("/ui", _NoCacheStatic(directory=WEB_DIR / "ui"), name="ui")


# ---------- client installer ----------
#
# The service hosts its own CLI distribution so new workstations can bootstrap
# with one line: `curl -fsSL https://vox.delo.sh/install.sh | sh`. Single source
# of truth (scripts/vox-speak in the repo), TLS-fronted via Traefik, no separate
# hosting. `__BASE__` is substituted at request time so the installer pulls from
# whatever origin served it (handy for local dev + prod on the same template).

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "vox-speak"

_INSTALLER_TEMPLATE = """#!/bin/sh
# vox-speak bootstrap installer. Served by __BASE__.
set -eu

DEST="${VOX_INSTALL_DIR:-$HOME/.local/bin}"
SRC="${VOX_BOOTSTRAP_URL:-__BASE__/bin/vox-speak}"

mkdir -p "$DEST"
# Download to .tmp then rename so a partial write never becomes executable.
curl -fsSL "$SRC" -o "$DEST/vox-speak.tmp"
chmod +x "$DEST/vox-speak.tmp"
mv "$DEST/vox-speak.tmp" "$DEST/vox-speak"

echo "installed: $DEST/vox-speak"

case ":$PATH:" in
  *":$DEST:"*)
    echo "on PATH; try: vox-speak 'hello world'"
    ;;
  *)
    echo "warning: $DEST is not on PATH"
    echo "add this to ~/.zshenv (not ~/.zshrc; non-interactive SSH skips .zshrc):"
    echo "  path=(~/.local/bin \\$path)"
    ;;
esac
"""


@app.get("/bin/vox-speak")
async def bin_vox_speak() -> FileResponse:
    """Serve the raw vox-speak shell script."""
    if not _SCRIPT_PATH.is_file():
        raise HTTPException(404, "vox-speak script not bundled in this image")
    return FileResponse(_SCRIPT_PATH, media_type="text/x-shellscript")


@app.get("/install.sh")
async def install_sh(request: Request) -> Response:
    """Bootstrap installer: `curl -fsSL .../install.sh | sh`."""
    # Traefik terminates TLS upstream, so request.url.scheme is always "http"
    # inside the container. Honour X-Forwarded-Proto/Host so the installer
    # advertises the client-facing URL (https://vox.delo.sh) rather than the
    # internal one.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    base = f"{proto}://{host}"
    body = _INSTALLER_TEMPLATE.replace("__BASE__", base)
    return Response(content=body, media_type="text/x-shellscript")


@app.post("/synthesize", responses={200: {"content": {"audio/wav": {}}}})
async def synthesize(req: SynthesizeRequest, request: Request) -> Response:
    """Raw WAV synthesis. Runs the engine chain, returns the winning engine's bytes.

    Headers on success: ``X-Vox-Engine`` and ``X-Vox-Voice-Source``
    (reference | registered | stock).
    """
    assert _engine is not None and _repo is not None
    kwargs = _request_synth_kwargs(req, request)
    try:
        result = await _synthesize_wav(**kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc, what="synthesize") from exc
    return Response(
        content=result.wav_bytes,
        media_type="audio/wav",
        headers=_synth_headers(result),
    )


@app.post("/synthesize-url", response_model=SynthesizeUrlResponse)
async def synthesize_url(req: SynthesizeRequest, request: Request, response: Response) -> SynthesizeUrlResponse:
    """Synthesize with engine fallback, transcode to OGG/Opus, return a URL.

    Consumers like Telegram's sendVoice fetch the URL directly from our
    /audio/<id>.ogg route. The cache entry expires per ``VOX_AUDIO_TTL_SECONDS``.
    """
    kwargs = _request_synth_kwargs(req, request)
    try:
        resp = await _synthesize_and_cache(**kwargs, request=request)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, what="synthesize_url") from exc
    response.headers["X-Vox-Engine"] = resp.engine
    if resp.voice_source:
        response.headers["X-Vox-Voice-Source"] = resp.voice_source
    return resp


# ---------- OpenAI-compatible speech API ----------
#
# POST /v1/audio/speech and GET /v1/models, so any OpenAI-TTS client (earshot-relay's openai adapter,
# Open WebUI, Home Assistant's OpenAI TTS, ...) can use vox as its voice. Same auth as everything else
# (VOX_API_KEY as `Authorization: Bearer` or `X-API-Key`); the OpenAI SDK sends the Bearer form.

# OpenAI's own voice names. They are not vox voices, so they get the default voice (and a header says so).
OPENAI_STOCK_VOICES = frozenset({
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable", "marin",
    "nova", "onyx", "sage", "shimmer", "verse",
})
# `model` names. "vox-local" (or "vox-local-<anything>") refuses the ElevenLabs fallback; the rest allow it.
MODEL_LOCAL = "vox-local"
MODEL_ANY = "vox"
OPENAI_MODEL_IDS = (MODEL_LOCAL, MODEL_ANY)
REQUIRE_LOCAL_HEADER = "x-vox-require-local"
OPENAI_INPUT_MAX_CHARS = 4096  # OpenAI's own limit


def _openai_error(
    status: int, message: str, err_type: str, *, code: Optional[str] = None,
    param: Optional[str] = None, headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": param, "code": code}},
        headers=headers,
    )


def _header_safe(value: str) -> str:
    """Response header values must be latin-1 and single-line."""
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in value)[:200]


class SpeechRequest(BaseModel):
    """OpenAI's `POST /v1/audio/speech` body. Unknown fields (`instructions`, `stream_format`) are ignored."""
    model: str = Field(MODEL_ANY, max_length=100)
    input: str = Field(..., min_length=1, max_length=OPENAI_INPUT_MAX_CHARS)
    voice: Optional[str | dict[str, Any]] = None  # a name, or OpenAI's custom-voice object {"id": ...}
    response_format: str = "mp3"
    speed: float = Field(1.0, ge=0.25, le=4.0)

    @field_validator("response_format")
    @classmethod
    def _known_format(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in audio_codec.OPENAI_FORMATS:
            raise ValueError(f"response_format must be one of: {', '.join(audio_codec.OPENAI_FORMATS)}")
        return v

    @field_validator("input")
    @classmethod
    def _input_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("input must not be blank")
        return v

    def voice_name(self) -> Optional[str]:
        if isinstance(self.voice, dict):
            raw = self.voice.get("id") or self.voice.get("name")
            return str(raw).strip() if raw else None
        return self.voice.strip() if self.voice and self.voice.strip() else None


async def _speech_voice(requested: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The vox voice for an OpenAI `voice` value, and a note when the default stood in.

    A registered vox voice name is used as is. Anything else (an OpenAI stock name like ``alloy``, or a
    name vox does not have) gets the default voice, and the note goes out in ``X-Vox-Voice-Fallback``.
    """
    assert _repo is not None
    default = _default_voice() or None
    if not requested:
        return default, None
    if await _repo.get(requested) is not None:
        return requested, None
    kind = "an OpenAI stock voice" if requested.lower() in OPENAI_STOCK_VOICES else "not a vox voice"
    return default, f"{requested!r} is {kind}; used the default voice {default!r}"


def _local_engine_names() -> list[str]:
    assert _engine is not None
    return [e.name for e in _engine._engines if e.name != ElevenLabsEngine.name]


def _speech_failure(exc: Exception) -> JSONResponse:
    http_exc = exc if isinstance(exc, HTTPException) else _http_error(exc, what="openai_speech")
    headers = http_exc.headers or {}
    return _openai_error(
        http_exc.status_code, str(http_exc.detail),
        "invalid_request_error" if http_exc.status_code < 500 and http_exc.status_code != 503 else "server_error",
        code=headers.get("X-Vox-Error"), headers=headers or None,
    )


@app.post("/v1/audio/speech", responses={200: {"content": {"audio/*": {}}}})
async def openai_speech(req: SpeechRequest, request: Request) -> Response:
    """OpenAI-compatible text-to-speech: raw audio in ``response_format`` (default mp3, as OpenAI).

    - ``voice``: a vox voice name. An OpenAI stock name (alloy, nova, ...) or an unknown name uses the
      default voice (``VOX_DEFAULT_VOICE``) and says so in ``X-Vox-Voice-Fallback``. The voice used is
      in ``X-Vox-Voice``.
    - ``X-Vox-Engine`` names the engine that served. vox falls back to ElevenLabs (a stranger's voice)
      when the GPU engines are down; to refuse that, send ``model: "vox-local"`` or the header
      ``X-Vox-Require-Local: 1``: you then get 503 ``engines_unavailable`` instead of a fallback.
    - ``speed`` 0.25-4.0 is applied with ffmpeg ``atempo``.
    """
    assert _engine is not None and _repo is not None
    model = req.model.strip().lower()
    header = request.headers.get(REQUIRE_LOCAL_HEADER, "").strip().lower()
    require_local = (
        model == MODEL_LOCAL or model.startswith(MODEL_LOCAL + "-") or header in {"1", "true", "yes"}
    )
    try:
        voice, fallback_note = await _speech_voice(req.voice_name())
        engines = None
        if require_local:
            engines = _local_engine_names()
            if not engines:
                return _openai_error(
                    503, "no local engine is configured", "server_error", code="engines_unavailable",
                    headers={"X-Vox-Error": "engines_unavailable"},
                )
        result = await _synthesize_wav(
            text=req.input, voice_name=voice, cfg=2.0, steps=10, engines=engines,
        )
    except Exception as exc:  # noqa: BLE001
        return _speech_failure(exc)
    audio_bytes = result.wav_bytes
    if req.response_format != "wav" or req.speed != 1.0:
        try:
            audio_bytes = await asyncio.to_thread(
                audio_codec.transcode_openai, result.wav_bytes, req.response_format,
                speed=req.speed, input_format="wav",
            )
        except Exception:  # noqa: BLE001
            logger.exception("openai_speech transcode failed")
            return _openai_error(500, "audio transcode failed", "server_error", code="transcode_failed")
    headers = {
        "X-Vox-Engine": result.engine,
        "X-Vox-Voice-Source": result.voice_source,
        "X-Vox-Voice": _header_safe(voice or ""),
    }
    if fallback_note:
        headers["X-Vox-Voice-Fallback"] = _header_safe(fallback_note)
    return Response(
        content=audio_bytes, media_type=audio_codec.OPENAI_FORMATS[req.response_format][1], headers=headers,
    )


@app.get("/v1/models")
async def openai_models() -> dict:
    """OpenAI's model list, for clients that probe it before speaking."""
    return {
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": 0, "owned_by": "vox"} for mid in OPENAI_MODEL_IDS
        ],
    }


@app.get("/audio/{cache_id}.ogg", name="get_audio")
async def get_audio(cache_id: str) -> FileResponse:
    """Serve a cached OGG/Opus blob. Called by Telegram when sendVoice fires."""
    path = audio_cache.path_for(cache_id)
    if path is None:
        raise HTTPException(404, "audio expired or not found")
    # inline so Telegram's probe works; Cache-Control to keep CDN honest
    return FileResponse(
        path,
        media_type="audio/ogg",
        headers={"Cache-Control": "public, max-age=600"},
    )


# ---------- sink relay ----------

def _validated_key(key: str) -> str:
    """Validate a sink key, translating the domain error into a 422."""
    try:
        return sink_relay.validate_key(key)
    except sink_relay.SinkKeyError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/sink/{key}", response_model=SinkStatus)
async def sink_status(key: str) -> SinkStatus:
    """Report how many listeners a sink currently has.

    Cheap enough to probe before every synthesis, which is the point: the caller
    checks this first so it can synthesize down the *right* path -- WAV inline
    for local playback, or OGG-to-cache when a sink will fetch it -- instead of
    synthesizing twice when it guesses wrong.
    """
    return SinkStatus(key=_validated_key(key), listeners=_sinks.listener_count(key))


@app.post("/sink/{key}/play", response_model=SinkPlayResponse)
async def sink_play(key: str, req: SinkPlayRequest) -> SinkPlayResponse:
    """Push an already-synthesized audio URL to a sink's listeners.

    Deliberately thin so anything that can POST JSON -- n8n, a shell
    script, an agent -- can reach the human's speakers without going through the
    CLI.
    """
    _validated_key(key)
    delivered = _sinks.publish(key, req.model_dump())
    return SinkPlayResponse(key=key, delivered=delivered, audio_url=req.audio_url)


@app.post("/sink/{key}/say", response_model=SinkPlayResponse)
async def sink_say(key: str, req: SynthesizeRequest, request: Request) -> SinkPlayResponse:
    """Synthesize and push to a sink in one call.

    The one-liner form for fleet callers:
    ``curl -X POST $VOX/sink/delo-macbook/say -d '{"text":"build done"}'``.
    Synthesis runs even when nobody is listening so the response still carries a
    usable ``audio_url`` -- the caller can fall back to playing it itself.
    """
    _validated_key(key)
    kwargs = _request_synth_kwargs(req, request)
    try:
        resp = await _synthesize_and_cache(**kwargs, request=request)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, what="sink_say") from exc

    delivered = _sinks.publish(key, {
        "audio_url": resp.audio_url,
        "text": req.text,
        "voice": req.voice,
        "engine": resp.engine,
        "duration_s": resp.duration_s,
        "format": resp.format,
    })
    return SinkPlayResponse(key=key, delivered=delivered, audio_url=resp.audio_url)


@app.get("/sink/{key}/events")
async def sink_events(key: str) -> StreamingResponse:
    """Subscribe to a sink as an SSE stream. Held open until the client leaves.

    The listener is registered for the life of the stream and torn down when the
    generator is closed, which Starlette does on client disconnect.
    """
    _validated_key(key)

    async def stream():
        try:
            async with _sinks.subscribe(key) as queue:
                async for frame in sink_relay.event_stream(_sinks, key, queue):
                    yield frame
        except sink_relay.SinkCapacityError as exc:
            yield sink_relay.sse_frame("error", {"message": str(exc)})
        except asyncio.CancelledError:  # normal client disconnect
            raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=sink_relay.SSE_HEADERS,
    )


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


# ---------- voice registration (shared by upload, design and own-voice) ----------

VOICE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
OWN_MIN_SECONDS = 8.0       # the browser enforces 8 s; the server allows 0.5 s of timer jitter
OWN_MIN_SECONDS_SLACK = 0.5
REFERENCE_MIN_SECONDS = 10.0  # the optional longer sample
# The UI stops at 30 s. The server allows a second of timer jitter past REF_AUDIO_MAX_SECONDS and then trims
# the clip to exactly that, BEFORE the consent gate, so the gate judges the very audio the voice is made from.
OWN_MAX_SECONDS_SLACK = 1.0
DESIGN_DEFAULT_SAMPLE = (
    "Hello, this is my voice. I can read you a short story, answer a question, "
    "or just say good morning."
)
DESIGN_CFG = 2.0
DESIGN_STEPS = 10
TAG_OWN = "own"
TAG_DESIGNED = "designed"


def _store_voice_wav(src_path: str, name: str) -> tuple[Path, float]:
    """Trim a reference clip to REF_AUDIO_MAX_SECONDS, downmix to mono, write ``<name>.wav``.

    ``src_path`` is any file soundfile can read. Returns the destination and the kept duration.
    """
    dest = voices_mod.VOICES_DIR / f"{name}.wav"
    if dest.resolve().parent != voices_mod.VOICES_DIR.resolve():
        raise HTTPException(422, "invalid voice name")
    info = sf.info(src_path)
    frames = int(min(info.duration, REF_AUDIO_MAX_SECONDS) * info.samplerate)
    data, sr = sf.read(src_path, frames=frames)
    if data.ndim == 2:
        data = data.mean(axis=1)
    sf.write(dest, data, sr, subtype="PCM_16")
    return dest, min(info.duration, REF_AUDIO_MAX_SECONDS)


async def _register_voice_file(
    *, name: str, display_name: str, src_path: str, source_label: Optional[str],
    prompt_text: Optional[str], tags: list[str],
) -> Voice:
    """Store a clip as the voice ``name`` and upsert its profile. One path for every way in."""
    assert _repo is not None
    dest, duration = _store_voice_wav(src_path, name)
    return await _repo.upsert(
        name=name, display_name=display_name, wav_path=dest.name,
        duration_s=duration, source_path=source_label,
        prompt_text=prompt_text, tags=tags,
        # Auto-populate per-engine ref for zero-friction onboarding. Operators
        # can later POST a different clip per engine if they want engine-specific
        # tuning; the default here means "same clip for everyone".
        vibevoice_ref_path=dest.name,
        vibevoice_speaker_tag=None,  # reserved for future multi-speaker
    )


async def _register_voice_bytes(
    *, name: str, display_name: str, audio: bytes, suffix: str, source_label: Optional[str],
    prompt_text: Optional[str], tags: list[str],
) -> Voice:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(audio)
        src_tmp = tmp.name
    try:
        return await _register_voice_file(
            name=name, display_name=display_name, src_path=src_tmp,
            source_label=source_label, prompt_text=prompt_text, tags=tags,
        )
    finally:
        Path(src_tmp).unlink(missing_ok=True)


def _voice_slug(raw: str) -> str:
    """A voice name that is safe as a filename and a URL part (the field is lowercase, 1-48 chars)."""
    slug = raw.strip()
    if not VOICE_SLUG_RE.match(slug):
        raise HTTPException(
            422,
            "name must be 1-48 characters: lowercase letters, digits, '-' or '_', starting with a letter or digit",
        )
    return slug


async def _require_name_free(name: str, own_tag: str) -> None:
    """A new voice may replace only an earlier voice of the same kind, never someone else's name.

    POST /voices (the fleet's upload) still replaces freely; the web flows must not be a way to
    overwrite a house voice or a fleet voice by typing its name.
    """
    assert _repo is not None
    existing = await _repo.get(name)
    if existing is not None and own_tag not in existing.tags:
        raise HTTPException(409, f"a voice named '{name}' already exists; choose another name")


async def _read_upload(audio: UploadFile) -> bytes:
    data = await audio.read(UPLOAD_MAX_BYTES + 1)
    if len(data) > UPLOAD_MAX_BYTES:
        raise HTTPException(413, "recording is too large; keep it under 30 seconds")
    return data


@app.post("/voices", response_model=VoiceOut)
async def create_voice(
    request: Request,
    name: str = Form(..., description="Slug used as the API key, e.g. 'rick'"),
    display_name: str = Form(...),
    prompt_text: Optional[str] = Form(None),
    tags: Optional[str] = Form(None, description="Comma-separated tags"),
    audio: UploadFile = File(..., description="Reference audio (any format sf can read)"),
) -> VoiceOut:
    """Create or replace a voice profile.

    Uploaded audio is trimmed to REF_AUDIO_MAX_SECONDS and downmixed to mono
    before being stored under VOICES_DIR as <name>.wav.

    This is the fleet's upload: likeness-making outside the consent gate, so callers who may not do that
    (``consent.ungated_clone_allowed``) get 403 and must use ``POST /voices/own``.
    """
    assert _repo is not None
    _require_ungated_clone(request)

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(audio.filename or "up").suffix) as tmp:
        shutil.copyfileobj(audio.file, tmp)
        raw_tmp = tmp.name
    # One decode path for every way in: m4a/webm/mp4 go through ffmpeg like the web flows do.
    src_tmp, _ = await _readable_wav_path(raw_tmp)

    try:
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        v = await _register_voice_file(
            name=name, display_name=display_name, src_path=src_tmp,
            source_label=audio.filename, prompt_text=prompt_text, tags=tag_list,
        )
    finally:
        Path(src_tmp).unlink(missing_ok=True)
    return VoiceOut.from_model(v)


# ---------- describe a voice (POST /voices/design) ----------

def _tidy_text(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    # Parentheses delimit the description in the engine prompt; keep them out of the text.
    v = re.sub(r"[()\r\n]+", " ", v)
    return re.sub(r"\s+", " ", v).strip() or None


def _strip_str(v: Any) -> Any:
    return v.strip() if isinstance(v, str) else v


def _tidy_description(v: Optional[str]) -> str:
    out = _tidy_text(v)
    if out is None or len(out) < 8:
        raise ValueError("describe the voice: age, pace, tone, accent")
    return out


class VoiceDesignIn(BaseModel):
    name: str = Field(..., description="Slug for the new voice, e.g. 'calm-narrator'")
    display_name: str = Field(..., min_length=1, max_length=80)
    description: str = Field(
        ..., min_length=8, max_length=400,
        description="The voice itself: age, pace, tone, accent. No names of real people.",
    )
    sample_text: Optional[str] = Field(
        None, max_length=400,
        description="What the reference clip says; kept as the voice's prompt_text.",
    )
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1)

    _strip_name = field_validator("display_name", mode="before")(_strip_str)
    _tidy_desc = field_validator("description")(_tidy_description)
    _tidy_sample = field_validator("sample_text")(_tidy_text)


class VoiceDesignPreviewIn(BaseModel):
    description: str = Field(..., min_length=8, max_length=400)
    sample_text: Optional[str] = Field(None, max_length=400)
    seed: Optional[int] = Field(None, ge=0, le=2**31 - 1)

    _tidy_desc = field_validator("description")(_tidy_description)
    _tidy_sample = field_validator("sample_text")(_tidy_text)


def _check_description_allowed(description: str, *, action: str) -> None:
    """Hosted edition: refuse a description that names a real person (422, logged)."""
    if not edition_mod.is_hosted():
        return
    reason = edition_mod.refuse_real_person(description)
    if reason is not None:
        logger.warning("voice design refused (%s, hosted edition): %r", action, description[:120])
        raise HTTPException(
            422, reason, headers={"X-Vox-Error": "real_person_description"},
        )


def _design_engines() -> list[str]:
    return [os.environ.get("VOX_DESIGN_ENGINE", "voxcpm").strip() or "voxcpm"]


def _design_text(description: str, sample_text: Optional[str]) -> str:
    """The VoxCPM2 "Voice Design" prompt: the description in parentheses, then what is said.

    Same shape as house-voices/build.py: no reference audio, so the engine invents the voice.
    """
    return f"({description}){sample_text or DESIGN_DEFAULT_SAMPLE}"


@app.post("/voices/design/preview", response_model=SynthesizeUrlResponse)
async def preview_designed_voice(
    req: VoiceDesignPreviewIn, request: Request, response: Response,
) -> SynthesizeUrlResponse:
    """Hear a description before keeping it. Same refusal rule as ``POST /voices/design``;
    nothing is stored except the short-lived audio cache entry."""
    _check_description_allowed(req.description, action="preview")
    try:
        resp = await _synthesize_and_cache(
            text=_design_text(req.description, req.sample_text), voice_name=None,
            cfg=DESIGN_CFG, steps=DESIGN_STEPS, request=request,
            engines=_design_engines(), seed=req.seed,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, what="voice_design_preview") from exc
    response.headers["X-Vox-Engine"] = resp.engine
    return resp


@app.post("/voices/design", response_model=VoiceOut)
async def design_voice(req: VoiceDesignIn) -> VoiceOut:
    """Make a voice from a description, with no recording of anyone.

    Does what ``house-voices/build.py`` does: synthesize ``"(" + description + ")" + sample`` with
    no reference audio, then register the result like ``POST /voices`` (shared helper). Tagged
    ``designed``. In the hosted edition a description that seems to name a real person is
    refused with 422 (see app/edition.py) and the refusal is logged.
    """
    assert _repo is not None
    name = _voice_slug(req.name)
    _check_description_allowed(req.description, action="design")
    await _require_name_free(name, TAG_DESIGNED)
    sample = req.sample_text or DESIGN_DEFAULT_SAMPLE
    try:
        result = await _synthesize_wav(
            text=_design_text(req.description, sample), voice_name=None,
            cfg=DESIGN_CFG, steps=DESIGN_STEPS, engines=_design_engines(), seed=req.seed,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, what="voice_design") from exc
    v = await _register_voice_bytes(
        name=name, display_name=req.display_name.strip(), audio=result.wav_bytes, suffix=".wav",
        source_label="voice-design", prompt_text=sample, tags=[TAG_DESIGNED],
    )
    return VoiceOut.from_model(v)


# ---------- record your own voice (consent challenge + live upload) ----------

class ConsentChallengeOut(BaseModel):
    challenge_id: str
    sentence: str
    code: str
    expires_at: str  # ISO 8601, UTC
    record_min_seconds: float = OWN_MIN_SECONDS
    record_max_seconds: float = REF_AUDIO_MAX_SECONDS


def _gate() -> consent_mod.ConsentGate:
    if _consent_gate is None:
        raise HTTPException(503, "consent check unavailable", headers={"X-Vox-Error": "consent_unavailable"})
    return _consent_gate


@app.post("/voices/consent/challenge", response_model=ConsentChallengeOut)
async def consent_challenge(request: Request) -> ConsentChallengeOut:
    """Issue a single-use sentence and code to read aloud. Valid for 10 minutes, bound to the caller.

    Stored in postgres (consent_challenges), so a restart does not drop open challenges.
    """
    challenge = await _gate().store.issue_challenge(principal_for(request.headers))
    return ConsentChallengeOut(
        challenge_id=challenge.challenge_id,
        sentence=challenge.sentence,
        code=challenge.code,
        expires_at=datetime.fromtimestamp(challenge.expires_at, tz=timezone.utc).isoformat(),
    )


def _challenge_http_error(exc: consent_mod.ChallengeError) -> HTTPException:
    status = 404 if exc.kind == "unknown" else 410
    return HTTPException(status, str(exc), headers={"X-Vox-Error": f"challenge_{exc.kind}"})


async def _readable_wav_path(path: str) -> tuple[str, bool]:
    """``path`` itself when soundfile reads it; otherwise a converted temp WAV (ffmpeg, reading the
    file, not a pipe, so an MP4 with its moov atom at the end still decodes). ``path`` is deleted
    when converted. Returns (path, is_converted); the caller deletes the result."""
    try:
        sf.info(path)
        return path, False
    except Exception:  # noqa: BLE001 - not a format soundfile reads; try ffmpeg
        pass
    converted: Optional[str] = None
    try:
        wav = await asyncio.to_thread(audio_codec.to_wav_mono_file, path)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp.write(wav)
            converted = tmp.name
        if float(sf.info(converted).duration) <= 0:
            raise ValueError("decoded to zero length")
    except Exception:  # noqa: BLE001
        Path(path).unlink(missing_ok=True)
        if converted:
            Path(converted).unlink(missing_ok=True)
        raise HTTPException(422, "could not read that recording; record again or try another browser") from None
    Path(path).unlink(missing_ok=True)
    return converted, True


async def _as_readable_wav(data: bytes, suffix: str) -> tuple[str, bool]:
    """A temp path soundfile can read. Browsers record webm/mp4, which libsndfile cannot decode,
    so those go through ffmpeg. Returns (path, is_converted); the caller deletes the file."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".bin") as tmp:
        tmp.write(data)
        path = tmp.name
    return await _readable_wav_path(path)


class ConsentOut(BaseModel):
    decision: str            # accepted | review | unchecked (rejected is a 422)
    state: str               # verified | pending | unchecked
    message: str             # plain words for the person
    record_id: str
    usable: bool             # can the voice speak now
    warning: Optional[str] = None  # self edition: the check failed but the voice was made anyway


class OwnVoiceOut(VoiceOut):
    consent_result: ConsentOut


async def _readable_upload(audio: UploadFile) -> str:
    data = await _read_upload(audio)
    if not data:
        raise HTTPException(422, "the recording is empty; record again")
    path, _ = await _as_readable_wav(data, Path(audio.filename or "rec").suffix)
    return path


def _require_seconds(path: str, *, minimum: float, what: str) -> float:
    duration = float(sf.info(path).duration)
    if duration < minimum - OWN_MIN_SECONDS_SLACK:
        raise HTTPException(
            422, f"the {what} is {duration:.1f} s; it needs at least {minimum:.0f} seconds")
    if duration > REF_AUDIO_MAX_SECONDS + OWN_MAX_SECONDS_SLACK:
        raise HTTPException(422, f"the {what} is {duration:.0f} s; keep it under {REF_AUDIO_MAX_SECONDS:.0f} seconds")
    return duration


def _cap_clip(path: str) -> str:
    """``path`` itself when it is within REF_AUDIO_MAX_SECONDS; otherwise a trimmed mono WAV of its first
    REF_AUDIO_MAX_SECONDS (``path`` is deleted). What the consent gate scores and what the voice is made
    from must be the same audio, so the cap is applied once, here, before either sees the clip."""
    info = sf.info(path)
    if float(info.duration) <= REF_AUDIO_MAX_SECONDS:
        return path
    data, sr = sf.read(path, frames=int(REF_AUDIO_MAX_SECONDS * info.samplerate))
    if data.ndim == 2:
        data = data.mean(axis=1)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
        capped = tmp.name
    sf.write(capped, data, sr, subtype="PCM_16")
    Path(path).unlink(missing_ok=True)
    return capped


@app.post("/voices/own", response_model=OwnVoiceOut)
async def create_own_voice(
    request: Request,
    challenge_id: str = Form(...),
    name: str = Form(..., description="Slug for the new voice, e.g. 'my-voice'"),
    display_name: str = Form(..., min_length=1, max_length=80),
    audio: UploadFile = File(..., description="A live recording of the challenge sentence and code"),
    reference: Optional[UploadFile] = File(None, description="Optional longer sample (10-30 s) to make the voice from"),
    release_accepted: Optional[str] = Form(
        None, description="Hosted edition: must be true/1/on/yes, the person accepted the biometric release"),
) -> OwnVoiceOut:
    """Add the caller's own voice from a live recording of a challenge sentence and code, through the
    consent gate (app/consent.py): ASR checks the sentence and code, speaker embeddings check the
    recording against the optional longer ``reference`` sample and against the account's voiceprint.

    - accepted: 200, voice tagged ``own`` + ``consent-verified``, usable now.
    - review: 200, voice stored tagged ``own`` + ``consent-pending``; synthesis with it is 403 until the
      operator approves (``consent.state`` is ``pending``).
    - rejected: 422 with a plain reason, nothing stored but the consent record and its audio.
    - consent models unavailable: 503, the challenge is not used up.
    - self edition: the same checks run and are recorded, but a failed check only adds ``warning``; the
      voice is made, tagged ``consent-unchecked-self``.
    - hosted edition without ``release_accepted``: 422 ``release_required``, before the challenge is
      looked at (so it is not used up) and before any audio is read. The self edition ignores the field.

    The voice is made from ``reference`` when given, otherwise from the consent recording.
    """
    if edition_mod.is_hosted() and not edition_mod.release_accepted(release_accepted):
        raise HTTPException(
            422, "Please read and accept the notice about how your recording and voiceprint are kept, then try again.",
            headers={"X-Vox-Error": "release_required"})
    slug = _voice_slug(name)
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(422, "give the voice a display name")
    gate = _gate()
    principal = principal_for(request.headers)
    try:
        await gate.store.check_challenge(challenge_id, principal)
    except consent_mod.ChallengeError as exc:
        raise _challenge_http_error(exc) from exc
    await _require_name_free(slug, TAG_OWN)

    consent_src = await _readable_upload(audio)
    reference_src: Optional[str] = None
    try:
        duration = _require_seconds(consent_src, minimum=OWN_MIN_SECONDS, what="recording")
        consent_src = _cap_clip(consent_src)
        if reference is not None and (reference.filename or reference.size):
            reference_src = await _readable_upload(reference)
            _require_seconds(reference_src, minimum=REFERENCE_MIN_SECONDS, what="longer sample")
            reference_src = _cap_clip(reference_src)
        outcome = await _likeness_gate(
            gate, principal=principal, challenge_id=challenge_id, voice=slug,
            consent_audio=consent_src, reference_audio=reference_src)
        if not outcome.create_voice:
            logger.info("consent rejected voice=%s record=%s", slug, outcome.record.id)
            raise HTTPException(422, outcome.reason, headers={"X-Vox-Error": "consent_rejected",
                                                            "X-Vox-Consent-Record": outcome.record.id})
        v = await _register_voice_file(
            name=slug, display_name=display_name, src_path=outcome.voice_audio,
            source_label="own-voice-recording", prompt_text=None, tags=[TAG_OWN, *outcome.voice_tags],
        )
        await gate.voice_replaced(outcome)
    finally:
        Path(consent_src).unlink(missing_ok=True)
        if reference_src:
            Path(reference_src).unlink(missing_ok=True)
    logger.info("consent %s voice=%s record=%s seconds=%.1f", outcome.decision, slug, outcome.record.id, duration)
    return OwnVoiceOut(
        **VoiceOut.from_model(v).model_dump(),
        consent_result=ConsentOut(
            decision=outcome.decision, state=outcome.state, record_id=outcome.record.id,
            message=_consent_message(outcome), usable=outcome.state in ("verified", "unchecked"),
            warning=outcome.warning),
    )


async def _likeness_gate(
    gate: consent_mod.ConsentGate, *, principal: str, challenge_id: str, voice: str,
    consent_audio: str, reference_audio: Optional[str],
) -> consent_mod.Outcome:
    """THE entry for likeness-making: every path that turns a recording into a voice calls this and obeys
    the outcome (``create_voice``, ``voice_tags``, ``voice_audio``). It maps the gate's failures to HTTP."""
    unavailable = HTTPException(503, "consent check unavailable", headers={"X-Vox-Error": "consent_unavailable"})
    try:
        try:
            await gate.ensure_ready()
        except consent_mod.ConsentUnavailable as exc:
            if edition_mod.is_hosted():
                raise unavailable from exc
            # self edition: the gate records an "unchecked" attempt and the voice is still made
        return await gate.check(
            principal=principal, challenge_id=challenge_id, voice=voice,
            consent_audio=consent_audio, reference_audio=reference_audio)
    except consent_mod.ChallengeError as exc:
        raise _challenge_http_error(exc) from exc
    except consent_mod.ConsentUnavailable as exc:
        raise unavailable from exc


def _consent_message(outcome: consent_mod.Outcome) -> str:
    if outcome.state == "verified":
        return "Your recording matched the sentence, the code and your voice. The voice is ready to use."
    if outcome.state == "pending":
        return ("Your recording is saved and a person will take a quick look at it. "
                "The voice can speak once that is done. " + outcome.reason)
    return outcome.warning or "The consent check did not run on this voice."


# ---------- the review queue (operator only) ----------

class ReviewIn(BaseModel):
    decision: str = Field(..., description="approve or reject")
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("decision")
    @classmethod
    def _decision(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        return v


class ReviewOut(BaseModel):
    id: str
    voice: str
    principal: str
    sentence: str
    code: str
    transcript: Optional[str] = None
    phrase_score: Optional[float] = None
    code_ok: Optional[bool] = None
    speaker_score_reference: Optional[float] = None
    speaker_score_account: Optional[float] = None
    decision: str
    reason: Optional[str] = None
    has_reference_audio: bool = False
    created_at: str
    reviewed_at: Optional[str] = None
    reviewer: Optional[str] = None
    review_note: Optional[str] = None

    @classmethod
    def from_record(cls, r: consent_mod.ConsentRecord) -> "ReviewOut":
        return cls(
            id=r.id, voice=r.voice, principal=r.principal, sentence=r.sentence, code=r.code,
            transcript=r.transcript, phrase_score=r.phrase_score, code_ok=r.code_ok,
            speaker_score_reference=r.speaker_score_reference, speaker_score_account=r.speaker_score_account,
            decision=r.decision, reason=r.reason, has_reference_audio=bool(r.reference_audio_path),
            created_at=r.created_at.isoformat(),
            reviewed_at=r.reviewed_at.isoformat() if r.reviewed_at else None,
            reviewer=r.reviewer, review_note=r.review_note,
        )


def _require_operator(request: Request) -> None:
    """Review routes need the operator key (VOX_OPERATOR_KEY), not the ordinary API key."""
    expected = resolve_operator_key_from_env()
    if expected is None:
        raise HTTPException(503, "no operator key configured", headers={"X-Vox-Error": "no_operator_key"})
    if not headers_have_valid_api_key(request.headers, expected):
        raise HTTPException(403, "this needs the operator key", headers={"X-Vox-Error": "operator_only"})


@app.get("/consent/reviews", response_model=list[ReviewOut])
async def list_consent_reviews(request: Request, status: str = "review") -> list[ReviewOut]:
    """Consent records waiting for a person (``status=review``, the default; oldest first, it is a queue).
    ``status=all`` lists the latest 200 records of any decision and any other value the latest 200 with
    that decision (accepted, rejected, unchecked); both come back newest last."""
    _require_operator(request)
    records = await _gate().store.list_records(
        None if status == "all" else status, newest=status != "review")
    return [ReviewOut.from_record(r) for r in records]


# Kept consent audio keeps the format it arrived in when soundfile reads it (``.ogg``, ``.flac``), else WAV.
_AUDIO_TYPES = {".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac"}


@app.get("/consent/reviews/{record_id}/audio")
async def consent_review_audio(record_id: str, request: Request, which: str = "consent") -> FileResponse:
    """The audio behind a record, so the reviewer can listen. ``which`` is ``consent`` or ``reference``."""
    _require_operator(request)
    record = await _gate().store.get_record(record_id)
    if record is None:
        raise HTTPException(404, "no such consent record")
    name = record.reference_audio_path if which == "reference" else record.consent_audio_path
    path = (consent_mod.consent_dir() / name) if name else None
    if path is None or path.resolve().parent != consent_mod.consent_dir().resolve() or not path.is_file():
        raise HTTPException(404, "no audio kept for that record")
    return FileResponse(path, media_type=_AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))


@app.post("/consent/reviews/{record_id}", response_model=ReviewOut)
async def resolve_consent_review(record_id: str, body: ReviewIn, request: Request) -> ReviewOut:
    """Approve or reject a queued own-voice recording. Approve: the voice becomes ``consent-verified``
    and usable (and fixes the account voiceprint if there is none). Reject: the voice is deleted; the
    record and its audio stay."""
    _require_operator(request)
    assert _repo is not None
    gate = _gate()
    try:
        record = await gate.resolve_review(record_id, decision=body.decision, reviewer="operator", note=body.note)
    except consent_mod.ReviewError as exc:
        raise HTTPException(404 if exc.kind == "unknown" else 409, str(exc)) from exc
    voice = await _repo.get(record.voice)
    # Only touch the voice THIS record made: an own voice, still waiting, carrying this record's tag. A newer
    # recording under the same name has its own record; approving or rejecting this one must not reach it.
    if (voice is not None and TAG_OWN in voice.tags and consent_mod.TAG_PENDING in voice.tags
            and consent_mod.record_id_of(voice.tags) == record.id):
        if body.decision == "approve":
            tags = [t for t in voice.tags if t != consent_mod.TAG_PENDING] + [consent_mod.TAG_VERIFIED]
            await _repo.set_tags(voice.name, tags)
        else:
            await _repo.delete(voice.name)
            voice.abs_path.unlink(missing_ok=True)
    return ReviewOut.from_record(record)


@app.put("/voices/{name}/persona", response_model=VoiceOut)
async def set_voice_persona(name: str, body: VoicePersonaIn) -> VoiceOut:
    """Associate a spoken-text persona with a voice.

    ``persona`` is a persona id (basename of plugins/voice-persona/personas/<id>.md), ``"none"``
    to suppress persona enrichment for this voice, or ``null`` to clear the association. The
    server stores the slug only; it does not read persona files.
    """
    assert _repo is not None
    persona = body.persona.strip() if body.persona is not None else None
    if persona is not None and persona.lower() == PERSONA_NONE:
        persona = PERSONA_NONE  # consumers compare against the literal 'none'
    if persona is not None and persona != PERSONA_NONE and not PERSONA_RE.match(persona):
        raise HTTPException(422, f"invalid persona {body.persona!r}: expected [A-Za-z0-9][A-Za-z0-9_.-]* or 'none'")
    v = await _repo.set_persona(name, persona)
    if v is None:
        raise HTTPException(404, f"voice '{name}' not found")
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
        v.abs_path.with_name(f"{v.abs_path.stem}.consent.json").unlink(missing_ok=True)
    except Exception:
        pass
    return {"deleted": name}


# ---------- FastMCP tools ----------

def _default_voice() -> Optional[str]:
    """Resolve the default voice from env, falling back to 'rick'.

    Set VOX_DEFAULT_VOICE in the container env to change the fleet default.
    Pass voice="" (empty string) to explicitly request voice-design mode.
    """
    return os.environ.get("VOX_DEFAULT_VOICE", "rick")


@mcp.tool
async def speak(
    text: str,
    voice: Optional[str] = None,
    cfg: float = 2.0,
    steps: int = 10,
) -> dict:
    """Synthesize speech and return the audio as base64-encoded WAV bytes.

    Prefer ``speak_url`` for most delivery surfaces; this tool only exists for
    callers that must have the raw bytes inline.

    Args:
        text: What to say.
        voice: Name of a saved voice profile. Defaults to the service default
            voice (rick). Pass empty string "" for voice-design mode.
        cfg: Classifier-free guidance scale (1.0-5.0).
        steps: Diffusion inference steps (higher = better quality, slower).
    """
    import base64
    if voice is None:
        voice = _default_voice()
    result = await _synthesize_wav(
        text=text, voice_name=voice, cfg=cfg, steps=steps,
    )
    return {
        "audio_wav_b64": base64.b64encode(result.wav_bytes).decode("ascii"),
        "voice": voice,
        "engine": result.engine,
        "voice_source": result.voice_source,
        "bytes": len(result.wav_bytes),
    }


@mcp.tool
async def speak_url(
    text: str,
    voice: Optional[str] = None,
    cfg: float = 2.0,
    steps: int = 10,
) -> dict:
    """Synthesize speech and return a short-lived OGG/Opus URL.

    The returned URL is Telegram-ready: pass it to the ``telegram`` channel's
    ``send`` action with ``asVoice: true`` and Telegram's servers fetch
    directly. Also works for browser <audio>, Home Assistant media_player,
    Discord, etc. Entries expire after VOX_AUDIO_TTL_SECONDS (default 1h).

    Args:
        text: What to say.
        voice: Name of a saved voice profile. Defaults to the service default
            voice (rick). Pass empty string "" for voice-design mode.
        cfg: Classifier-free guidance scale (1.0-5.0).
        steps: Diffusion inference steps (higher = better quality, slower).
    """
    if voice is None:
        voice = _default_voice()
    resp = await _synthesize_and_cache(
        text=text, voice_name=voice, cfg=cfg, steps=steps,
    )
    return resp.model_dump()


@mcp.tool
async def speak_sink(
    text: str,
    sink: Optional[str] = None,
    voice: Optional[str] = None,
    cfg: float = 2.0,
    steps: int = 10,
) -> dict:
    """Speak to the human wherever they are, instead of into the host's speakers.

    An agent running on a server has no speakers anyone is sitting in front of.
    This routes the audio to a named *sink* -- the machine the human is actually
    at, which holds a listening connection (``voxxy listen``).

    ``delivered: 0`` means no machine was listening on that sink; the returned
    ``audio_url`` is still valid, so fall back to delivering it another way
    (Telegram, a message) rather than assuming the human heard it.

    Args:
        text: What to say.
        sink: Sink name. Defaults to the service default (VOX_DEFAULT_SINK).
        voice: Saved voice profile. Defaults to the service default voice.
        cfg: Classifier-free guidance scale (1.0-5.0).
        steps: Diffusion inference steps (higher = better quality, slower).
    """
    key = sink or os.environ.get("VOX_DEFAULT_SINK", "")
    if not key:
        return {"error": "no sink given and VOX_DEFAULT_SINK is unset"}
    try:
        sink_relay.validate_key(key)
    except sink_relay.SinkKeyError as exc:
        return {"error": str(exc)}

    if voice is None:
        voice = _default_voice()
    resp = await _synthesize_and_cache(
        text=text, voice_name=voice, cfg=cfg, steps=steps,
    )
    delivered = _sinks.publish(key, {
        "audio_url": resp.audio_url,
        "text": text,
        "voice": voice,
        "engine": resp.engine,
        "duration_s": resp.duration_s,
        "format": resp.format,
    })
    return {"sink": key, "delivered": delivered, **resp.model_dump()}


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
            "elevenlabs_voice_id": v.elevenlabs_voice_id,
            "persona": v.persona,
        }
        for v in await _repo.list()
    ]


# Mount FastMCP's streamable HTTP app at /mcp so clients can:
#   hermes mcp add vox --url https://vox.delo.sh/mcp/
# path="/" above means the MCP endpoint sits at the mount root, not /mcp/mcp.
app.mount("/mcp", mcp_app)
