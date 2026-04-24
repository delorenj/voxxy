"""HTTP client wrapper over the voxxy-core public API.

Design notes:
- Response models are defined here (not imported from app/) because the CLI's
  contract is the *public HTTP wire format*, not the server's internal pydantic
  models. This decoupling means server-internal refactors that preserve the HTTP
  shape do not require CLI changes, and vice versa.
- All methods are synchronous. The CLI is a single-threaded command runner; async
  would add overhead and complexity for zero benefit. httpx.Client (not AsyncClient)
  is used throughout.
- SynthUrlResponse carries the X-Vox-Engine response header as an attribute.
  Rather than returning a tuple (which loses the pydantic benefits), we extend
  the model with an extra field that the model_validator populates after the JSON
  is parsed. This keeps call sites clean: `resp.engine` vs `resp, engine = ...`.
- VoxUnreachable is raised on ConnectError/TransportError so callers can
  distinguish "server is down" from "server returned an error" without catching
  httpx internals.
- Timeout defaults to 30s. Synthesis can take ~5-15s on the GPU; 30s leaves
  headroom for cold starts while not hanging indefinitely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field


# ---------- typed exceptions ----------

class VoxError(Exception):
    """Base exception for all voxxy client errors."""


class VoxNotFound(VoxError):
    """Raised for HTTP 404 responses."""


class VoxValidationError(VoxError):
    """Raised for HTTP 4xx responses that are not 404."""


class VoxServerError(VoxError):
    """Raised for HTTP 5xx responses."""


class VoxUnreachable(VoxError):
    """Raised when the server cannot be reached (connection refused, DNS failure, etc.)."""


# ---------- response models (mirrors the public HTTP API, not server internals) ----------

class EngineInfo(BaseModel):
    """One entry in the /healthz engines[] array."""

    name: str
    ready: bool


class HealthResponse(BaseModel):
    """Full /healthz response body."""

    status: str
    engines: list[EngineInfo]


class VoiceOut(BaseModel):
    """Mirror of the VoiceOut shape returned by GET /voices and POST /voices.

    Fields match app/main.py's VoiceOut exactly. Keeping them in sync is a
    convention enforced by code review, not a build-time check. If the server
    adds a field, old CLI versions will silently ignore it (pydantic v2 default).
    """

    name: str
    display_name: str
    duration_s: float
    tags: list[str] = Field(default_factory=list)
    prompt_text: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    vibevoice_ref_path: Optional[str] = None
    vibevoice_speaker_tag: Optional[str] = None


class SynthUrlResponse(BaseModel):
    """Response from POST /synthesize-url, extended with the X-Vox-Engine header.

    The ``vox_engine`` attribute is populated by VoxClient.synthesize_url after
    parsing the JSON body. It is NOT part of the JSON body itself; it comes from
    the X-Vox-Engine response header. Using a model field (vs a tuple return)
    keeps the calling code clean.
    """

    audio_url: str
    engine: str
    duration_s: Optional[float] = None
    bytes: int
    format: str = "ogg_opus"
    # Populated from the X-Vox-Engine response header after JSON parse.
    vox_engine_header: Optional[str] = None


# ---------- client ----------

class VoxClient:
    """Thin synchronous wrapper over the voxxy-core HTTP API.

    Instantiate once per command invocation; do not share across threads.
    The underlying httpx.Client is closed implicitly when the object is
    garbage-collected, or explicitly via the context manager protocol.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
        )

    def __enter__(self) -> "VoxClient":
        return self

    def __exit__(self, *_) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Execute a request and raise typed exceptions on failure."""
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise VoxUnreachable(f"Could not connect to voxxy server: {exc}") from exc
        except httpx.TransportError as exc:
            raise VoxUnreachable(f"Transport error talking to voxxy server: {exc}") from exc

        if resp.status_code == 404:
            raise VoxNotFound(f"Not found: {method} {path} → 404")
        if 400 <= resp.status_code < 500:
            body = resp.text[:200]
            raise VoxValidationError(
                f"Client error: {method} {path} → {resp.status_code}: {body}"
            )
        if resp.status_code >= 500:
            body = resp.text[:200]
            raise VoxServerError(
                f"Server error: {method} {path} → {resp.status_code}: {body}"
            )

        return resp

    def healthz(self) -> HealthResponse:
        """GET /healthz — returns overall status + per-engine readiness."""
        resp = self._request("GET", "/healthz")
        return HealthResponse.model_validate(resp.json())

    def list_voices(self) -> list[VoiceOut]:
        """GET /voices — returns all saved voice profiles."""
        resp = self._request("GET", "/voices")
        return [VoiceOut.model_validate(v) for v in resp.json()]

    def get_voice(self, name: str) -> VoiceOut:
        """GET /voices/{name} — returns one voice profile.

        Raises VoxNotFound if the voice does not exist.
        """
        resp = self._request("GET", f"/voices/{name}")
        return VoiceOut.model_validate(resp.json())

    def create_voice(
        self,
        name: str,
        display_name: str,
        audio_path: Path,
        *,
        tags: Optional[list[str]] = None,
        prompt_text: Optional[str] = None,
    ) -> VoiceOut:
        """POST /voices — create or replace a voice profile.

        Sends the audio as a multipart form upload. The server does its own
        trimming and downmixing; the CLI preprocesses first (via audio.py) so
        the upload is already clean and the server's work is trivial.
        """
        form_data: dict = {
            "name": name,
            "display_name": display_name,
        }
        if tags:
            form_data["tags"] = ",".join(tags)
        if prompt_text:
            form_data["prompt_text"] = prompt_text

        with open(audio_path, "rb") as fh:
            files = {"audio": (audio_path.name, fh, "audio/wav")}
            resp = self._request("POST", "/voices", data=form_data, files=files)

        return VoiceOut.model_validate(resp.json())

    def delete_voice(self, name: str) -> None:
        """DELETE /voices/{name}.

        Raises VoxNotFound if the voice does not exist.
        """
        self._request("DELETE", f"/voices/{name}")

    def synthesize_url(
        self,
        text: str,
        voice: Optional[str] = None,
        *,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> SynthUrlResponse:
        """POST /synthesize-url — synthesize, transcode to OGG, return a URL.

        The returned SynthUrlResponse includes the X-Vox-Engine response header
        value in the ``vox_engine_header`` field so callers can log which engine
        served the request.
        """
        payload: dict = {"text": text, "cfg": cfg, "steps": steps}
        if voice:
            payload["voice"] = voice

        resp = self._request("POST", "/synthesize-url", json=payload)
        result = SynthUrlResponse.model_validate(resp.json())
        result.vox_engine_header = resp.headers.get("x-vox-engine")
        return result

    def synthesize_wav(
        self,
        text: str,
        voice: Optional[str] = None,
        *,
        cfg: float = 2.0,
        steps: int = 10,
    ) -> bytes:
        """POST /synthesize — returns raw WAV bytes.

        Used by ``voxxy speak --raw``. Bypasses the audio cache and OGG
        transcoding path; bytes are returned inline and never stored on the server.
        """
        payload: dict = {"text": text, "cfg": cfg, "steps": steps}
        if voice:
            payload["voice"] = voice

        resp = self._request("POST", "/synthesize", json=payload)
        return resp.content

    def fetch_audio(self, url: str) -> bytes:
        """GET an audio URL and return the raw bytes.

        Used after synthesize_url to download the OGG blob. The URL is expected
        to be the audio_url from SynthUrlResponse, served via Traefik. We use
        a plain httpx.get (not self._client) so the full URL is used as-is
        without base_url prepending.
        """
        try:
            resp = httpx.get(url, timeout=self._client.timeout)
        except httpx.ConnectError as exc:
            raise VoxUnreachable(f"Could not fetch audio from {url}: {exc}") from exc
        except httpx.TransportError as exc:
            raise VoxUnreachable(f"Transport error fetching {url}: {exc}") from exc

        if resp.status_code == 404:
            raise VoxNotFound(f"Audio not found at {url} (cache may have expired)")
        if resp.status_code >= 400:
            raise VoxServerError(f"Error fetching audio: {resp.status_code}")

        return resp.content
