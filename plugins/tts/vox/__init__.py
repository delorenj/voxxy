"""Hermes TTS provider for the Vox service at ``vox.delo.sh``.

The stable provider id is ``vox`` so existing fleet configuration using
``tts.provider: vox`` is resolved by Hermes' plugin registry rather than
falling through to the Edge fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from agent.tts_provider import TTSProvider

DEFAULT_BASE_URL = "https://vox.delo.sh"
DEFAULT_VOICE = "rick"
DEFAULT_TIMEOUT_SECONDS = 60.0
_FORMAT_SUFFIX = {
    "wav": ".wav",
    "mp3": ".mp3",
    "ogg": ".ogg",
    "opus": ".ogg",
    "flac": ".flac",
}


class VoxTTSProviderError(RuntimeError):
    """Raised when Vox cannot synthesize a usable local audio artifact."""


class VoxTTSProvider(TTSProvider):
    """HTTP-backed provider for Vox's WAV and Telegram-ready OGG/Opus APIs."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_voice: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        ffmpeg_binary: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._default_voice = default_voice
        self._timeout = float(timeout)
        self._ffmpeg_binary = ffmpeg_binary
        self._transport = transport

    @property
    def name(self) -> str:
        return "vox"

    @property
    def display_name(self) -> str:
        return "Vox"

    @property
    def voice_compatible(self) -> bool:
        # Hermes will retain an OGG/Opus file as-is, and safely repair/convert
        # a non-OGG fallback for every voice-message platform.
        return True

    def is_available(self) -> bool:
        try:
            payload = self._get_json("/healthz")
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        engines = payload.get("engines")
        if isinstance(engines, list):
            return any(isinstance(engine, dict) and engine.get("ready") is True for engine in engines)
        return payload.get("status") in {"ok", "healthy", "ready"}

    def list_voices(self) -> List[Dict[str, Any]]:
        try:
            payload = self._get_json("/voices")
        except Exception:
            return []
        if isinstance(payload, dict):
            payload = payload.get("voices", [])
        if not isinstance(payload, list):
            return []

        voices: List[Dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            voice_id = str(item.get("name") or item.get("id") or "").strip()
            if not voice_id:
                continue
            row: Dict[str, Any] = {
                "id": voice_id,
                "display": str(item.get("display_name") or item.get("display") or voice_id),
            }
            for key in ("language", "gender", "preview_url", "tags"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    row[key] = value.strip()
                elif key == "tags" and isinstance(value, list):
                    row[key] = value
            voices.append(row)
        return voices

    def default_voice(self) -> Optional[str]:
        return self._env_voice() or self._resolve_default_voice()

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "self-hosted",
            "tag": "Vox HTTP TTS with Telegram-ready OGG/Opus",
            "env_vars": [
                {
                    "key": "VOX_API_KEY",
                    "prompt": "Vox API key (optional; only for protected deployments)",
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
        del model, speed
        if not isinstance(text, str) or not text.strip():
            raise VoxTTSProviderError("text is required for Vox synthesis")

        requested_format = self._normalize_format(format)
        selected_voice = self._resolve_voice(voice)
        payload: Dict[str, Any] = {"text": text}
        if selected_voice:
            payload["voice"] = selected_voice
        for key, coercer in (("cfg", float), ("steps", int)):
            if extra.get(key) is not None:
                try:
                    payload[key] = coercer(extra[key])
                except (TypeError, ValueError):
                    pass

        # Vox owns the authoritative Telegram codec settings. Request the
        # cache endpoint then download the short-lived OGG/Opus blob locally so
        # Hermes can attach a real local artifact, not a remote URL.
        if requested_format in {"ogg", "opus"}:
            return self._synthesize_ogg(payload, output_path)

        wav_bytes = self._synthesize_wav(payload)
        if requested_format == "wav":
            target = self._rewrite_output_path(output_path, "wav")
            self._write_bytes(target, wav_bytes)
            return target

        ffmpeg_binary = self._resolve_ffmpeg_binary()
        if not ffmpeg_binary:
            # The dispatcher will still convert this WAV for a voice-compatible
            # gateway when ffmpeg is available at the Hermes layer.
            target = self._rewrite_output_path(output_path, "wav")
            self._write_bytes(target, wav_bytes)
            return target
        return self._transcode_wav(wav_bytes, output_path, requested_format, ffmpeg_binary)

    def _synthesize_ogg(self, payload: Dict[str, Any], output_path: str) -> str:
        response = self._request("POST", "/synthesize-url", json=payload)
        if response.is_error:
            raise VoxTTSProviderError(self._format_http_error(response, "/synthesize-url"))
        try:
            data = response.json()
        except Exception as exc:
            raise VoxTTSProviderError("Vox /synthesize-url returned invalid JSON") from exc
        audio_url = data.get("audio_url") if isinstance(data, dict) else None
        if not isinstance(audio_url, str) or not audio_url.strip():
            raise VoxTTSProviderError("Vox /synthesize-url returned no audio_url")
        download = self._request("GET", urljoin(f"{self._resolve_base_url()}/", audio_url.strip()))
        if download.is_error:
            raise VoxTTSProviderError(self._format_http_error(download, "audio_url"))
        if not self._looks_like_ogg_opus(download.content):
            content_type = download.headers.get("content-type", "unknown")
            raise VoxTTSProviderError(
                f"Vox audio_url returned non-OGG/Opus audio (content-type: {content_type})"
            )
        target = self._rewrite_output_path(output_path, "ogg")
        self._write_bytes(target, download.content)
        return target

    def _synthesize_wav(self, payload: Dict[str, Any]) -> bytes:
        response = self._request("POST", "/synthesize", json=payload)
        if response.is_error:
            raise VoxTTSProviderError(self._format_http_error(response, "/synthesize"))
        if not self._looks_like_wav(response.content):
            content_type = response.headers.get("content-type", "unknown")
            raise VoxTTSProviderError(
                f"Vox /synthesize returned non-WAV audio (content-type: {content_type})"
            )
        return response.content

    def _resolve_base_url(self) -> str:
        configured = self._base_url or self._service_config().get("base_url") or os.environ.get("VOX_URL") or DEFAULT_BASE_URL
        return str(configured).strip().rstrip("/") or DEFAULT_BASE_URL

    def _resolve_default_voice(self) -> str:
        configured = self._default_voice or self._service_config().get("voice") or DEFAULT_VOICE
        return str(configured).strip() or DEFAULT_VOICE

    @staticmethod
    def _env_voice() -> Optional[str]:
        """Per-process voice pin from ``VOX_VOICE``."""
        return os.environ.get("VOX_VOICE", "").strip() or None

    def _resolve_voice(self, requested: Optional[str]) -> str:
        """Resolve the voice for one synthesis call.

        ``VOX_VOICE`` wins on purpose. Most fleet agents share a single
        ``config.yaml`` by symlink, so ``tts.voice`` is fleet-wide; pinning
        the voice in an agent's systemd unit is the only way to give one
        agent its own voice without forking that config. Nothing explicit
        is being overridden — Hermes' ``text_to_speech_tool`` has no
        per-call voice argument, so ``requested`` is itself just the
        shared ``tts.voice`` config value.
        """
        if env_voice := self._env_voice():
            return env_voice
        if isinstance(requested, str) and requested.strip():
            return requested.strip()
        return self._resolve_default_voice()

    @staticmethod
    def _service_config() -> Dict[str, Any]:
        """Read the documented ``tts.vox`` block without mutating config."""
        try:
            from hermes_cli.config import load_config
            config = load_config() or {}
        except Exception:
            return {}
        tts = config.get("tts") if isinstance(config, dict) else None
        if not isinstance(tts, dict):
            return {}
        # ``voxxy`` is read only as a migration bridge for early, unshipped
        # skeleton users. New docs/config are strictly canonical ``vox``.
        section = tts.get("vox") or tts.get("voxxy")
        return section if isinstance(section, dict) else {}

    def _resolve_api_key(self) -> Optional[str]:
        value = self._api_key or self._service_config().get("api_key") or os.environ.get("VOX_API_KEY")
        return str(value).strip() if value else None

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "audio/wav, audio/ogg, application/json"}
        if api_key := self._resolve_api_key():
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        return headers

    def _request(self, method: str, path_or_url: str, **kwargs: Any) -> httpx.Response:
        url = path_or_url if path_or_url.startswith(("http://", "https://")) else f"{self._resolve_base_url()}{path_or_url}"
        try:
            with httpx.Client(
                timeout=self._timeout,
                headers=self._headers(),
                follow_redirects=True,
                transport=self._transport,
            ) as client:
                return client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise VoxTTSProviderError(f"Vox request to {path_or_url} failed: {exc}") from exc

    def _get_json(self, path: str) -> Any:
        response = self._request("GET", path)
        if response.is_error:
            raise VoxTTSProviderError(self._format_http_error(response, path))
        try:
            return response.json()
        except Exception as exc:
            raise VoxTTSProviderError(f"Vox {path} returned invalid JSON") from exc

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

    @staticmethod
    def _looks_like_wav(data: bytes) -> bool:
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"

    @staticmethod
    def _looks_like_ogg_opus(data: bytes) -> bool:
        return len(data) >= 36 and data[:4] == b"OggS" and b"OpusHead" in data[:128]

    def _transcode_wav(self, wav_bytes: bytes, output_path: str, format_name: str, ffmpeg_binary: str) -> str:
        target = self._rewrite_output_path(output_path, format_name)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as source:
            source.write(wav_bytes)
            source_path = Path(source.name)
        try:
            codec = ["-codec:a", "libmp3lame", "-q:a", "2"] if format_name == "mp3" else ["-codec:a", "flac"]
            try:
                completed = subprocess.run(
                    [ffmpeg_binary, "-y", "-loglevel", "error", "-i", str(source_path), *codec, target],
                    check=False, capture_output=True, text=True, timeout=60,
                )
            except FileNotFoundError as exc:
                raise VoxTTSProviderError(f"ffmpeg binary not found: {ffmpeg_binary}") from exc
            if completed.returncode != 0:
                raise VoxTTSProviderError(
                    f"ffmpeg failed converting Vox WAV to {format_name}: {(completed.stderr or 'unknown error').strip()}"
                )
            if not Path(target).is_file() or Path(target).stat().st_size == 0:
                raise VoxTTSProviderError(f"ffmpeg produced an empty {format_name} file")
            return target
        finally:
            source_path.unlink(missing_ok=True)

    @staticmethod
    def _format_http_error(response: httpx.Response, path: str) -> str:
        try:
            data = response.json()
        except Exception:
            data = None
        detail = data.get("detail") or data.get("error") or data.get("message") if isinstance(data, dict) else None
        if detail is None:
            detail = response.text.strip() or response.reason_phrase or "request failed"
        return f"Vox request to {path} failed ({response.status_code}): {str(detail).replace(chr(10), ' ').strip()}"


def register(ctx: Any) -> None:
    ctx.register_tts_provider(VoxTTSProvider())


# Intentional compatibility alias for code that imported the unshipped WIP name.
VoxxyTTSProvider = VoxTTSProvider
VoxxyTTSProviderError = VoxTTSProviderError

__all__ = ["VoxTTSProvider", "VoxTTSProviderError", "VoxxyTTSProvider", "VoxxyTTSProviderError", "register"]
