"""Hermes TTS provider for Cartesia's Sonic models.

The stable provider id is ``cartesia`` so fleet configuration using
``tts.provider: cartesia`` is resolved by Hermes' plugin registry rather than
falling through to the Edge fallback.

Unlike Vox, which serves Telegram-ready OGG/Opus from its own cache endpoint,
Cartesia's ``/tts/bytes`` can only return ``wav``, ``mp3`` or ``raw`` -- every
other container is rejected with ``400 unsupported format``. Opus is therefore
this plugin's job: we ask Cartesia for 48 kHz PCM and hand that straight to
libopus, so the one lossy step is the one Telegram needs anyway.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent.tts_provider import TTSProvider

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.cartesia.ai"
DEFAULT_MODEL = "sonic-3.6"
# Required header. The server rejects a request without it and names the
# current value in the error, but it does not validate against a known list.
DEFAULT_API_VERSION = "2026-08-14"
DEFAULT_TIMEOUT_SECONDS = 60.0

# Cartesia's own voice catalog reports BOTH of these as name "Tanner" -- the
# "Upbeat Assistant" / "Laidback Spirit" labels are playground UI text and are
# not API fields, so a lookup by name is ambiguous and must not be attempted.
# These aliases exist so config can say `voice: tanner` and still be exact.
VOICE_ALIASES: Dict[str, str] = {
    "tanner": "710feaa3-b550-42f3-b3eb-6f37f2a7cc0a",
    "tanner-upbeat": "710feaa3-b550-42f3-b3eb-6f37f2a7cc0a",
    "tanner-laidback": "373e661a-f0ef-4e34-a09e-183184a443e6",
}
DEFAULT_VOICE = "tanner"

# Containers Cartesia will actually serve. Anything else is a 400.
_NATIVE_CONTAINERS = frozenset({"wav", "mp3"})
_FORMAT_SUFFIX = {
    "wav": ".wav",
    "mp3": ".mp3",
    "ogg": ".ogg",
    "opus": ".ogg",
    "flac": ".flac",
}
# Only these sample rates are accepted; 32000 is not one of them.
_PCM_SAMPLE_RATE = 48000
_MP3_SAMPLE_RATE = 44100
_MP3_BIT_RATE = 128000


class CartesiaTTSProviderError(RuntimeError):
    """Raised when Cartesia cannot synthesize a usable local audio artifact."""


class CartesiaTTSProvider(TTSProvider):
    """HTTP-backed provider for Cartesia Sonic with local Telegram-ready OGG/Opus."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_voice: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        ffmpeg_binary: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_voice = default_voice
        self._default_model = default_model
        self._timeout = float(timeout)
        self._ffmpeg_binary = ffmpeg_binary
        self._transport = transport

    @property
    def name(self) -> str:
        return "cartesia"

    @property
    def display_name(self) -> str:
        return "Cartesia"

    @property
    def voice_compatible(self) -> bool:
        # synthesize() returns a real OGG/Opus file for ogg/opus requests, and
        # Hermes safely repairs a non-OGG fallback for voice-message platforms.
        return True

    def is_available(self) -> bool:
        if not self._resolve_api_key():
            return False
        try:
            response = self._request("GET", "/voices/?limit=1")
        except Exception:
            return False
        return not response.is_error

    def list_voices(self) -> List[Dict[str, Any]]:
        voices: List[Dict[str, Any]] = []
        page: Optional[str] = None
        # The catalog is ~1000 entries; cap the walk so a runaway cursor can
        # never turn a voice listing into an unbounded crawl.
        for _ in range(20):
            path = "/voices/?limit=100" + (f"&starting_after={page}" if page else "")
            try:
                response = self._request("GET", path)
            except Exception:
                break
            if response.is_error:
                break
            try:
                payload = response.json()
            except Exception:
                break
            if not isinstance(payload, dict):
                break
            for item in payload.get("data", []):
                if not isinstance(item, dict):
                    continue
                voice_id = str(item.get("id") or "").strip()
                if not voice_id:
                    continue
                row: Dict[str, Any] = {
                    "id": voice_id,
                    "display": str(item.get("name") or voice_id),
                }
                for key in ("language", "description"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        row[key] = value.strip()
                voices.append(row)
            if not payload.get("has_more"):
                break
            page = payload.get("next_page")
        return voices

    def default_voice(self) -> Optional[str]:
        return self._env_voice() or self._resolve_default_voice()

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "hosted",
            "tag": "Cartesia Sonic TTS with Telegram-ready OGG/Opus",
            "env_vars": [
                {
                    "key": "CARTESIA_API_KEY",
                    "prompt": "Cartesia API key",
                },
            ],
        }

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        if not isinstance(text, str) or not text.strip():
            raise CartesiaTTSProviderError("text is required for Cartesia synthesis")

        requested_format = self._normalize_format(format)
        payload: Dict[str, Any] = {
            "model_id": self._resolve_model(model),
            "transcript": text,
            "voice": {"id": self._resolve_voice(voice)},
            "language": str(extra.get("language") or "en"),
        }

        generation_config: Dict[str, Any] = {}
        if speed is not None:
            try:
                # Measurably effective across roughly 0.6-1.5 on sonic-3.6.
                generation_config["speed"] = float(speed)
            except (TypeError, ValueError):
                pass
        if extra.get("volume") is not None:
            try:
                generation_config["volume"] = float(extra["volume"])
            except (TypeError, ValueError):
                pass
        if generation_config:
            payload["generation_config"] = generation_config

        # mp3 is served natively; everything else starts life as 48 kHz PCM so
        # the opus/flac encode is the only generation loss.
        if requested_format == "mp3":
            payload["output_format"] = {
                "container": "mp3",
                "sample_rate": _MP3_SAMPLE_RATE,
                "bit_rate": _MP3_BIT_RATE,
            }
        else:
            payload["output_format"] = {
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": _PCM_SAMPLE_RATE,
            }

        audio = self._synthesize_bytes(payload)

        if requested_format in _NATIVE_CONTAINERS:
            target = self._rewrite_output_path(output_path, requested_format)
            self._write_bytes(target, audio)
            return target

        ffmpeg_binary = self._resolve_ffmpeg_binary()
        if not ffmpeg_binary:
            # Hand back the WAV and let the dispatcher convert it for a
            # voice-compatible gateway where ffmpeg lives at the Hermes layer.
            logger.warning(
                "[Cartesia] ffmpeg not found; returning WAV instead of %s",
                requested_format,
            )
            target = self._rewrite_output_path(output_path, "wav")
            self._write_bytes(target, audio)
            return target
        return self._transcode_wav(audio, output_path, requested_format, ffmpeg_binary)

    def _synthesize_bytes(self, payload: Dict[str, Any]) -> bytes:
        response = self._request("POST", "/tts/bytes", json=payload)
        if response.is_error:
            raise CartesiaTTSProviderError(self._format_http_error(response, "/tts/bytes"))
        audio = response.content
        if not audio:
            raise CartesiaTTSProviderError("Cartesia /tts/bytes returned no audio")
        return audio

    @staticmethod
    def _service_config() -> Dict[str, Any]:
        """Read the documented ``tts.cartesia`` block without mutating config."""
        try:
            from hermes_cli.config import load_config

            config = load_config() or {}
        except Exception:
            return {}
        tts = config.get("tts") if isinstance(config, dict) else None
        if not isinstance(tts, dict):
            return {}
        section = tts.get("cartesia")
        return section if isinstance(section, dict) else {}

    def _resolve_api_key(self) -> Optional[str]:
        value = (
            self._api_key
            or self._service_config().get("api_key")
            or os.environ.get("CARTESIA_API_KEY")
        )
        return str(value).strip() if value else None

    def _resolve_base_url(self) -> str:
        configured = (
            self._base_url
            or self._service_config().get("base_url")
            or os.environ.get("CARTESIA_URL")
            or DEFAULT_BASE_URL
        )
        return str(configured).strip().rstrip("/")

    def _resolve_api_version(self) -> str:
        configured = (
            self._service_config().get("api_version")
            or os.environ.get("CARTESIA_VERSION")
            or DEFAULT_API_VERSION
        )
        return str(configured).strip()

    def _resolve_model(self, requested: Optional[str]) -> str:
        configured = (
            (requested or "").strip()
            or self._default_model
            or self._service_config().get("model")
            or os.environ.get("CARTESIA_MODEL")
            or DEFAULT_MODEL
        )
        return str(configured).strip()

    def _resolve_default_voice(self) -> str:
        configured = (
            self._default_voice
            or self._service_config().get("voice")
            or DEFAULT_VOICE
        )
        return str(configured).strip()

    @staticmethod
    def _env_voice() -> Optional[str]:
        value = os.environ.get("CARTESIA_VOICE")
        return value.strip() if value and value.strip() else None

    def _voice_aliases(self) -> Dict[str, str]:
        aliases = dict(VOICE_ALIASES)
        configured = self._service_config().get("voices")
        if isinstance(configured, dict):
            for key, value in configured.items():
                if isinstance(value, str) and value.strip():
                    aliases[str(key).strip().lower()] = value.strip()
        return aliases

    def _resolve_voice(self, requested: Optional[str]) -> str:
        candidate = (requested or "").strip() or self._env_voice() or self._resolve_default_voice()
        return self._voice_aliases().get(candidate.lower(), candidate)

    def _headers(self) -> Dict[str, str]:
        headers = {"Cartesia-Version": self._resolve_api_version()}
        api_key = self._resolve_api_key()
        if not api_key:
            raise CartesiaTTSProviderError(
                "CARTESIA_API_KEY is not set (or tts.cartesia.api_key in config.yaml)"
            )
        headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _request(self, method: str, path_or_url: str, **kwargs: Any) -> httpx.Response:
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else f"{self._resolve_base_url()}{path_or_url}"
        )
        try:
            with httpx.Client(
                timeout=self._timeout,
                headers=self._headers(),
                follow_redirects=True,
                transport=self._transport,
            ) as client:
                return client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CartesiaTTSProviderError(
                f"Cartesia request to {path_or_url} failed: {exc}"
            ) from exc

    def _resolve_ffmpeg_binary(self) -> Optional[str]:
        return self._ffmpeg_binary if self._ffmpeg_binary is not None else shutil.which("ffmpeg")

    @staticmethod
    def _normalize_format(value: Optional[str]) -> str:
        value = str(value or "mp3").strip().lower().lstrip(".")
        return "wav" if value == "wave" else value if value in _FORMAT_SUFFIX else "mp3"

    @staticmethod
    def _rewrite_output_path(output_path: str, format_name: str) -> str:
        return str(Path(output_path).with_suffix(_FORMAT_SUFFIX[format_name]))

    @staticmethod
    def _write_bytes(output_path: str, data: bytes) -> None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _transcode_wav(
        self, wav_bytes: bytes, output_path: str, format_name: str, ffmpeg_binary: str
    ) -> str:
        target = self._rewrite_output_path(output_path, format_name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as source:
            source.write(wav_bytes)
            source_path = Path(source.name)
        try:
            if format_name in {"ogg", "opus"}:
                # Telegram voice-note shape: mono 48 kHz Opus in an Ogg container.
                codec = [
                    "-codec:a", "libopus",
                    "-b:a", "48k",
                    "-ar", "48000",
                    "-ac", "1",
                    "-application", "voip",
                ]
            else:
                codec = ["-codec:a", "flac"]
            try:
                completed = subprocess.run(
                    [ffmpeg_binary, "-y", "-loglevel", "error", "-i", str(source_path), *codec, target],
                    check=False, capture_output=True, text=True, timeout=60,
                )
            except FileNotFoundError as exc:
                raise CartesiaTTSProviderError(f"ffmpeg binary not found: {ffmpeg_binary}") from exc
            if completed.returncode != 0:
                raise CartesiaTTSProviderError(
                    f"ffmpeg failed converting Cartesia WAV to {format_name}: "
                    f"{(completed.stderr or 'unknown error').strip()}"
                )
            if not Path(target).is_file() or Path(target).stat().st_size == 0:
                raise CartesiaTTSProviderError(f"ffmpeg produced an empty {format_name} file")
            return target
        finally:
            source_path.unlink(missing_ok=True)

    @staticmethod
    def _format_http_error(response: httpx.Response, path: str) -> str:
        try:
            data = response.json()
        except Exception:
            data = None
        detail = None
        if isinstance(data, dict):
            # Cartesia errors are {error_code, message, title, request_id}.
            detail = data.get("message") or data.get("error") or data.get("detail")
            if code := data.get("error_code"):
                detail = f"{detail} [{code}]" if detail else str(code)
        if detail is None:
            detail = response.text.strip() or response.reason_phrase or "request failed"
        return (
            f"Cartesia request to {path} failed ({response.status_code}): "
            f"{str(detail).replace(chr(10), ' ').strip()}"
        )


def register(ctx: Any) -> None:
    ctx.register_tts_provider(CartesiaTTSProvider())
