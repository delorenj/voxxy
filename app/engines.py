"""Synthesis engines with a pluggable interface and fallback orchestration.

Engines are pluggable. Today the shipped set is:

- ``RemoteEngineClient``: HTTP/JSON client that speaks the ``/v1/synthesize``
  contract defined in :mod:`app.engine_contract`. One instance per entry in
  ``VOX_ENGINES`` (``name=url,name=url``). The local VoxCPM2 engine now runs
  as a sidecar container and is accessed via ``RemoteEngineClient``; there is
  no longer an in-process model loader in this package.
- ``ElevenLabsEngine``: remote ElevenLabs TTS. Stays in-core because it's
  already a remote call; wrapping it in another container would add a hop for
  nothing. Terminal fallback when all remote engines fail.

:class:`EngineOrchestrator` tries engines in order; first success wins. The
request shape is the same for every engine so a new one is just another
implementation of :class:`SynthEngine`.

Routing rules on top of the chain (all request-scoped, all optional):

- A **per-request reference** (the caller's own clip) only ever goes to an
  engine in ``VOX_REFERENCE_ENGINES`` (default ``voxcpm``: Apache-2.0,
  multilingual). It never reaches VibeVoice (research licence, English and
  Chinese only, audible watermark) and never ElevenLabs (cannot clone it).
- ``require_reference`` turns the stock fallback off: the clip is served by a
  reference engine or the request fails.
- ``allowed_engines`` is an allowlist; nothing outside it is tried.
- ``language`` drops engines that cannot speak it (``VOX_ENGINE_LANGUAGES``).

Every success carries ``voice_source``: ``reference`` (the caller's clip was
used), ``registered`` (a saved voice's clip was used) or ``stock`` (no clip was
used, e.g. ElevenLabs or voice-design mode).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence

import httpx
import soundfile as sf

from app.engine_contract import (
    EngineHealth,
    EngineSynthesizeRequest,
    EngineSynthesizeResponse,
)
from app.reference import ReferenceAudio

logger = logging.getLogger(__name__)


# Values of SynthResult.voice_source and the X-Vox-Voice-Source header.
VOICE_SOURCE_REFERENCE = "reference"
VOICE_SOURCE_REGISTERED = "registered"
VOICE_SOURCE_STOCK = "stock"


@dataclass(slots=True)
class SynthResult:
    wav_bytes: bytes
    sample_rate: int
    engine: str  # identifies which engine served, e.g. "voxcpm" or "elevenlabs"
    # Whose voice this is: reference | registered | stock. Set by the orchestrator,
    # which is the only place that knows which clip (if any) the engine was given.
    voice_source: str = VOICE_SOURCE_STOCK


# ---------- routing policy ----------

# Engines allowed to clone from a clip sent with the request. Matched on the
# engine's configured name or its base name (``voxcpm-b`` -> ``voxcpm``).
DEFAULT_REFERENCE_ENGINES = "voxcpm"

# Languages each engine can speak (ISO 639-1, plus the aliases nb and fil). An
# engine missing from the table is not restricted. VoxCPM2: the 30 languages
# on its model card. VibeVoice-1.5B: English and Chinese only.
_VOXCPM2_LANGUAGES = frozenset(
    "ar da de el en es fi fil fr he hi id it ja km ko lo ms my nb nl no pl pt ru "
    "sv sw th tl tr vi zh".split()
)
DEFAULT_ENGINE_LANGUAGES: dict[str, Optional[frozenset[str]]] = {
    "voxcpm": _VOXCPM2_LANGUAGES,
    "vibevoice": frozenset({"en", "zh"}),
}

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")


def normalize_language(code: str) -> str:
    """``es-MX`` / ``ES`` / ``es_ES`` -> ``es``. Raises ValueError on a malformed tag."""
    code = code.strip()
    if not _LANGUAGE_RE.match(code):
        raise ValueError(f"not a language tag: {code!r}")
    return re.split(r"[-_]", code, maxsplit=1)[0].lower()


def parse_engine_languages(spec: str) -> dict[str, Optional[frozenset[str]]]:
    """Parse ``VOX_ENGINE_LANGUAGES``: ``vibevoice=en,zh;voxcpm=*`` (``*`` = any)."""
    table: dict[str, Optional[frozenset[str]]] = {}
    for entry in spec.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, langs = entry.split("=", 1)
        langs = langs.strip()
        table[name.strip().lower()] = (
            None if langs == "*"
            else frozenset(code.strip().lower() for code in langs.split(",") if code.strip())
        )
    return table


def _base_name(engine_name: str) -> str:
    return re.split(r"[-/]", engine_name, maxsplit=1)[0].lower()


def _accepts_reference(engine: "SynthEngine") -> bool:
    return bool(getattr(engine, "accepts_reference", True))


class SynthEngine(Protocol):
    """Contract every synthesis engine implements.

    ``name`` surfaces in telemetry and in the ``engine`` field of the tool
    response so callers can detect when a fallback engaged. ``available()`` is
    a cheap pre-flight to skip engines that can't possibly serve (e.g. no API
    key). ``generate`` is the actual work and must raise on failure so the
    orchestrator can try the next engine.

    ``accepts_reference`` says whether the engine clones from reference audio
    at all (False for ElevenLabs). The orchestrator only passes
    ``reference_audio`` / ``reference_sample_rate`` / ``seed`` when it has them,
    so an engine written against the older signature keeps working.
    """

    name: str
    accepts_reference: bool

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
        reference_audio: Optional[bytes] = None,
        reference_sample_rate: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> SynthResult: ...


def _error_summary(resp: httpx.Response, limit: int = 200) -> str:
    """A short, log-safe description of an error response.

    Error bodies can quote the request: a FastAPI 422 echoes its input, which
    here is caption text or a voice clip. Only a recognised envelope's code and
    message are kept (truncated); any other body is withheld.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return "no parseable error body"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            # Voxxy engine envelope: {"error": {"code": ..., "message": ...}}
            message = err.get("message")
            return f"{err['code']}: {str(message)[:limit]}" if message else err["code"]
        detail = body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("status"), str):
            # ElevenLabs: {"detail": {"status": "quota_exceeded", "message": ...}}
            return detail["status"][:limit]
    return "error body withheld"


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
    # Stock voices only: it cannot clone from a clip, so it never receives one
    # and anything it serves is reported as voice_source=stock.
    accepts_reference = False

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
        reference_audio: Optional[bytes] = None,
        reference_sample_rate: Optional[int] = None,
        seed: Optional[int] = None,
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
                    f"elevenlabs {resp.status_code}: {_error_summary(resp)}"
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


class PermanentEngineError(RuntimeError):
    """Engine returned a 4xx. Do not fall through to the next engine.

    4xx means the request itself is wrong (bad text, missing voice, malformed
    reference). Retrying on a different engine would just produce the same
    failure, and might mask the real issue. Surface it to the caller.
    """


class NoEligibleEngineError(RuntimeError):
    """The request's constraints leave no engine to try.

    Raised before any engine is called: an ``allowed_engines`` list naming
    nothing configured, a ``language`` no eligible engine speaks, or a
    per-request reference with no reference-capable engine. Core answers 503
    rather than quietly serving something the caller ruled out.
    """


class EnginesExhaustedError(RuntimeError):
    """Every eligible engine was unavailable or failed.

    ``restricted`` is True when the caller narrowed the chain
    (``require_reference`` or ``allowed_engines``). Core answers 503 for a
    restricted request (fail closed) and keeps its historical 500 otherwise.
    """

    def __init__(self, message: str, *, restricted: bool) -> None:
        super().__init__(message)
        self.restricted = restricted


class RemoteEngineClient:
    """SynthEngine that speaks to a container over HTTP/JSON.

    Matches the ``/v1/synthesize`` + ``/healthz`` contract in
    :mod:`app.engine_contract`. One instance per configured remote engine;
    core reads ``VOX_ENGINES`` (``name=url,name=url``) at startup and builds
    one ``RemoteEngineClient`` per entry.

    Health is cached for ``_HEALTH_TTL_SECONDS`` so ``available()`` doesn't hit
    the wire on every orchestrator pass; orchestrator loops happen on every
    synth request, and a 100ms health probe per request per engine adds up.

    Only evidence that the engine is *down* marks it unhealthy: a failed probe,
    a refused or reset connection, or a 5xx. A read timeout does not. It means
    the engine accepted the request and is still working (a long utterance, or
    a queue behind its GPU lock); counting that as an outage is what used to
    tip the next requests over to ElevenLabs.
    """

    _HEALTH_TTL_SECONDS = 10.0

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        timeout: float | None = None,
        health_timeout: float = 2.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if timeout is None:
            # Local GPU engines can take minutes on a long utterance when the
            # card is shared; a short cap silently falls through to ElevenLabs.
            timeout = float(os.environ.get("VOX_ENGINE_TIMEOUT_SECONDS", "240"))
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._health_timeout = health_timeout
        self._transport = transport  # tests inject httpx.MockTransport
        # Cached (timestamp, ready) pair. None forces a probe on first call.
        self._health_cache: Optional[tuple[float, bool]] = None
        self._health_lock = asyncio.Lock()
        # From the engine's /healthz capabilities; unknown until the first probe.
        self._accepts_reference: Optional[bool] = None

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    @property
    def accepts_reference(self) -> bool:
        """Whether the engine clones from reference audio (its healthz says so).

        Defaults to True before the first probe: the contract's default, and
        every engine shipped today (voxcpm, vibevoice) clones.
        """
        return True if self._accepts_reference is None else self._accepts_reference

    # --- health ---

    async def _probe_health(self) -> bool:
        try:
            async with self._client(self._health_timeout) as client:
                resp = await client.get(f"{self._base_url}/healthz")
            if resp.status_code != 200:
                logger.warning(
                    "engine %s healthz %d: %s", self.name,
                    resp.status_code, _error_summary(resp),
                )
                return False
            parsed = EngineHealth.model_validate(resp.json())
            if parsed.engine != self.name:
                # Not fatal, but worth flagging: config mismatch means a rename
                # somewhere. Keep serving if ``ready`` is true.
                logger.warning(
                    "engine %s name mismatch: remote reports %r",
                    self.name, parsed.engine,
                )
            self._accepts_reference = bool(parsed.capabilities.accepts_reference_audio)
            return bool(parsed.ready and parsed.model_loaded)
        except Exception as exc:  # noqa: BLE001
            logger.warning("engine %s healthz failed: %r", self.name, exc)
            return False

    def available(self) -> bool:
        """Non-async per Protocol; reads the cached health value.

        First call returns True optimistically so the first synth attempt
        triggers a real probe via ``generate()``. Subsequent calls reflect the
        last observed health within the TTL window.
        """
        if self._health_cache is None:
            return True
        ts, ok = self._health_cache
        if time.monotonic() - ts > self._HEALTH_TTL_SECONDS:
            return True  # let the next generate() refresh the cache
        return ok

    async def refresh_health(self) -> bool:
        """Explicit async probe. Useful for /healthz aggregation in core."""
        async with self._health_lock:
            ok = await self._probe_health()
            self._health_cache = (time.monotonic(), ok)
            return ok

    # --- generate ---

    async def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        prompt_text: Optional[str] = None,
        voice_id: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
        reference_audio: Optional[bytes] = None,
        reference_sample_rate: Optional[int] = None,
        seed: Optional[int] = None,
    ) -> SynthResult:
        ref_b64: Optional[str] = None
        ref_sr: Optional[int] = None
        if reference_audio is not None:
            # Per-request clip: already validated by core, held in memory only.
            ref_b64 = base64.b64encode(reference_audio).decode("ascii")
            ref_sr = reference_sample_rate
        elif reference_wav_path:
            # Read raw bytes, don't decode/re-encode. Engines know how to handle
            # WAV framing; keeping bytes intact preserves whatever bit depth
            # and channel layout the caller chose.
            with open(reference_wav_path, "rb") as fh:
                ref_bytes = fh.read()
            ref_b64 = base64.b64encode(ref_bytes).decode("ascii")
            try:
                ref_sr = int(sf.info(reference_wav_path).samplerate)
            except Exception:
                ref_sr = None

        payload = EngineSynthesizeRequest(
            text=text,
            reference_audio_b64=ref_b64,
            reference_sample_rate=ref_sr,
            prompt_text=prompt_text,
            voice_id=voice_id,
            cfg=cfg,
            steps=steps,
            seed=seed,
        )

        url = f"{self._base_url}/v1/synthesize"
        try:
            async with self._client(self._timeout) as client:
                resp = await client.post(url, json=payload.model_dump())
        except httpx.TimeoutException as exc:
            # ConnectTimeout: the engine is not accepting connections, so it
            # is down. Read/write/pool timeouts: it is busy, not down.
            if isinstance(exc, httpx.ConnectTimeout):
                self._health_cache = (time.monotonic(), False)
            raise RuntimeError(
                f"engine {self.name} timed out after {self._timeout:.0f}s ({type(exc).__name__})"
            ) from exc
        except httpx.TransportError as exc:
            self._health_cache = (time.monotonic(), False)
            raise RuntimeError(
                f"engine {self.name} transport error: {exc!r}"
            ) from exc

        if resp.status_code == 200:
            body = EngineSynthesizeResponse.model_validate(resp.json())
            wav = base64.b64decode(body.wav_b64)
            # Mark healthy on success so subsequent available() checks stay cheap.
            self._health_cache = (time.monotonic(), True)
            return SynthResult(
                wav_bytes=wav, sample_rate=body.sample_rate, engine=self.name,
            )

        # Non-2xx. Distinguish between "try the next engine" and "stop here".
        # The summary keeps the engine's error code and message, never the raw
        # body: a FastAPI 422 would echo the caption text or the clip back.
        summary = _error_summary(resp)
        if 400 <= resp.status_code < 500:
            # Bad input; next engine will hit the same error. Surface it.
            raise PermanentEngineError(
                f"engine {self.name} {resp.status_code}: {summary}"
            )
        # 5xx: transient on this engine. Mark unhealthy, fall through.
        self._health_cache = (time.monotonic(), False)
        raise RuntimeError(
            f"engine {self.name} {resp.status_code}: {summary}"
        )


class EngineOrchestrator:
    """Try engines in order. First success wins; log every fallback.

    ``reference_engines`` and ``engine_languages`` default to the env
    (``VOX_REFERENCE_ENGINES``, ``VOX_ENGINE_LANGUAGES``) over the built-in
    tables above; tests pass them explicitly.
    """

    def __init__(
        self,
        engines: list[SynthEngine],
        *,
        reference_engines: Optional[Sequence[str]] = None,
        engine_languages: Optional[dict[str, Optional[frozenset[str]]]] = None,
    ) -> None:
        self._engines = engines
        if reference_engines is None:
            reference_engines = os.environ.get(
                "VOX_REFERENCE_ENGINES", DEFAULT_REFERENCE_ENGINES
            ).split(",")
        self._reference_engines = frozenset(
            n.strip().lower() for n in reference_engines if n.strip()
        )
        if engine_languages is None:
            engine_languages = {
                **DEFAULT_ENGINE_LANGUAGES,
                **parse_engine_languages(os.environ.get("VOX_ENGINE_LANGUAGES", "")),
            }
        self._engine_languages = engine_languages

    # --- policy ---

    def speaks(self, engine_name: str, language: str) -> bool:
        """True unless the engine's language table rules ``language`` out."""
        name = engine_name.lower()
        for key in (name, _base_name(name)):
            if key in self._engine_languages:
                langs = self._engine_languages[key]
                return langs is None or language in langs
        return True

    def takes_request_reference(self, engine: SynthEngine) -> bool:
        """May this engine clone from a clip sent with the request?"""
        name = engine.name.lower()
        allowed = name in self._reference_engines or _base_name(name) in self._reference_engines
        return allowed and _accepts_reference(engine)

    def _plan(
        self,
        *,
        reference: Optional[ReferenceAudio],
        require_reference: bool,
        allowed_engines: Optional[Sequence[str]],
        language: Optional[str],
        preferred: Optional[list[str]],
    ) -> list[tuple[SynthEngine, bool]]:
        """Engines to try, in order, each with "send it the request's clip?".

        Raises NoEligibleEngineError when the constraints leave nothing.
        """
        engines = list(self._engines)
        if preferred and reference is None:
            # Stable sort: configured order holds within each group.
            engines.sort(key=lambda e: 0 if e.name in preferred else 1)
        if allowed_engines is not None:
            allowed = {n.strip() for n in allowed_engines}
            engines = [e for e in engines if e.name in allowed]
            if not engines:
                raise NoEligibleEngineError(
                    f"none of the requested engines ({', '.join(sorted(allowed))}) is configured; "
                    f"configured: {', '.join(e.name for e in self._engines)}"
                )
        if language:
            engines = [e for e in engines if self.speaks(e.name, language)]
            if not engines:
                raise NoEligibleEngineError(f"no eligible engine speaks language '{language}'")
        if reference is None:
            return [(e, False) for e in engines]

        cloners = [e for e in engines if self.takes_request_reference(e)]
        if require_reference:
            if not cloners:
                raise NoEligibleEngineError(
                    "no eligible engine may clone from a per-request reference "
                    f"(reference engines: {', '.join(sorted(self._reference_engines)) or 'none'})"
                )
            return [(e, True) for e in cloners]
        # The caller allowed a stock fallback. Only engines that never take a
        # clip qualify; a cloning engine outside the reference set (VibeVoice)
        # is left out entirely rather than handed the speaker's text.
        stock = [e for e in engines if not _accepts_reference(e)]
        plan = [(e, True) for e in cloners] + [(e, False) for e in stock]
        if not plan:
            raise NoEligibleEngineError("no eligible engine for a per-request reference")
        return plan

    # --- generate ---

    async def generate(
        self,
        *,
        text: str,
        reference_wav_path: Optional[str] = None,
        reference_resolver: Optional[Callable[[str], Optional[str]]] = None,
        prompt_text: Optional[str] = None,
        voice_id: Optional[str] = None,
        cfg: float = 2.0,
        steps: int = 10,
        preferred: Optional[list[str]] = None,
        reference: Optional[ReferenceAudio] = None,
        require_reference: bool = False,
        allowed_engines: Optional[Sequence[str]] = None,
        language: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> SynthResult:
        """Run the engine chain; first success wins.

        Args:
            reference_wav_path: Legacy / no-voice-profile path. Used when
                ``reference_resolver`` is not supplied.
            reference_resolver: Per-engine callable ``(engine_name) -> path``.
                When provided, it takes precedence over ``reference_wav_path``
                and lets each engine use a different reference clip (e.g. a
                de-noised version for VibeVoice).
            preferred: Optional engine names to try before the rest of the
                chain. A voice whose clone lives on a specific engine (e.g. a
                VibeVoice reference) should be served by that engine first.
                Stable sort preserves configured order within each group, so
                fallback order still follows ``VOX_ENGINES``. Ignored for a
                per-request reference.
            reference: The caller's own clip (and its transcript). Overrides
                the registered-voice arguments; goes only to reference engines.
            require_reference: Fail instead of falling back to a stock voice.
            allowed_engines: Allowlist of engine names; None = whole chain.
            language: Normalized language code; drops engines that can't speak it.
            seed: Forwarded to the engine's sampler.
        """
        if require_reference and reference is None:
            raise ValueError("require_reference needs a per-request reference")
        plan = self._plan(
            reference=reference,
            require_reference=require_reference,
            allowed_engines=allowed_engines,
            language=language,
            preferred=preferred,
        )
        restricted = require_reference or allowed_engines is not None
        text_len = len(text)
        ref_bytes = len(reference.wav_bytes) if reference is not None else 0
        last_exc: Optional[BaseException] = None
        tried: list[str] = []  # engines attempted before the winner (for fallback tracking)
        skipped: list[str] = []
        for engine, use_request_ref in plan:
            if not engine.available():
                logger.info("engine %s skipped (unavailable)", engine.name)
                skipped.append(engine.name)
                continue
            kwargs: dict = {"text": text, "voice_id": voice_id, "cfg": cfg, "steps": steps}
            if use_request_ref:
                assert reference is not None
                kwargs.update(
                    reference_wav_path=None,
                    prompt_text=reference.prompt_text,
                    reference_audio=reference.wav_bytes,
                    reference_sample_rate=reference.sample_rate,
                )
                source = VOICE_SOURCE_REFERENCE
            elif reference is not None:
                # Stock fallback for a reference request: nothing of the
                # speaker (clip or transcript) goes to this engine.
                kwargs.update(reference_wav_path=None, prompt_text=None)
                source = VOICE_SOURCE_STOCK
            else:
                effective_ref = (
                    reference_resolver(engine.name)
                    if reference_resolver is not None
                    else reference_wav_path
                )
                kwargs.update(reference_wav_path=effective_ref, prompt_text=prompt_text)
                source = (
                    VOICE_SOURCE_REGISTERED
                    if effective_ref and _accepts_reference(engine)
                    else VOICE_SOURCE_STOCK
                )
            if seed is not None:
                kwargs["seed"] = seed
            try:
                result = await engine.generate(**kwargs)
            except PermanentEngineError:
                # 4xx from a remote engine: propagate, don't try the next.
                # Retrying would just reproduce the same validation error.
                raise
            except Exception as exc:  # noqa: BLE001 - deliberately broad
                last_exc = exc
                # repr() because many torch/voxcpm exceptions have empty str().
                # Keep a traceback for diagnostic logs; stays on the WARNING
                # level so it's filterable.
                logger.warning(
                    "engine %s failed: %r", engine.name, exc, exc_info=True,
                )
                tried.append(engine.name)
                continue
            result.voice_source = source
            logger.info("engine %s served (%d bytes)", engine.name, len(result.wav_bytes))
            logger.info(
                "synth.completed engine=%s voice_source=%s text_len=%d ref_bytes=%d bytes=%d "
                "sample_rate=%d fallback_from=%s",
                engine.name, source, text_len, ref_bytes, len(result.wav_bytes),
                result.sample_rate, ",".join(tried),
            )
            return result
        # Every eligible engine was skipped or failed.
        logger.warning(
            "synth.failed tried=%s skipped=%s restricted=%s last_error=%r text_len=%d",
            ",".join(tried), ",".join(skipped), restricted, last_exc, text_len,
        )
        if last_exc is None:
            raise EnginesExhaustedError(
                f"no eligible engine is available (skipped: {', '.join(skipped)})",
                restricted=restricted,
            )
        raise EnginesExhaustedError(
            f"all synthesis engines failed; last error: {last_exc!r}",
            restricted=restricted,
        ) from last_exc
