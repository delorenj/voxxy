"""OpenAI-compatible chat caller shared by the persona rewrite and the clone pipeline.

Design notes:
- One HTTP/auth mechanism, two configuration namespaces. ``PersonaLLM`` (speak-time
  rewrites, must stay cheap and fail-open) and ``CloneLLM`` (identity resolution,
  video ranking, persona authoring: rare, quality over latency) differ only in
  which env vars / config keys they read and in how they label errors. Copying the
  request code twice would let the two drift on things like the content-parts
  response shape, so the mechanics live here once.
- Precedence per setting, highest first: ``<PREFIX>_*`` env > config.toml key of
  that namespace > (clone only) the persona namespace's env > persona config >
  built-in default. A user who has only ever configured the persona LLM therefore
  gets a working clone LLM for free, and can still point clone at a stronger model
  without touching the speak-time one.
- The API key is never stored raw: env wins, then an ``op://`` reference resolved
  with ``op read`` at call time.
- ``extract_json`` exists because models wrap JSON in code fences and leading
  prose even when told not to; strict ``json.loads`` on the raw reply would turn a
  cosmetic habit into a pipeline failure.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Optional

import httpx

from voxxy.config import DEFAULT_CLONE_MODEL, Config, load_config

DEFAULT_LLM_TIMEOUT = 40.0


class LLMError(Exception):
    """Anything that stops an LLM call (missing key, HTTP failure, bad body, bad JSON)."""


def env_timeout(var: str = "VOXXY_PERSONA_TIMEOUT", default: float = DEFAULT_LLM_TIMEOUT) -> float:
    raw = os.environ.get(var)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return default


def read_op_ref(ref: str, *, setting: str = "persona_api_key_ref", error_cls: type[Exception] = LLMError) -> str:
    if not ref.startswith("op://"):
        raise error_cls(f"{setting} must be an op:// reference (raw keys are never stored in config)")
    try:
        proc = subprocess.run(["op", "read", ref], capture_output=True, text=True, timeout=20)
    except FileNotFoundError as exc:
        raise error_cls(f"{setting} is set but the 'op' CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise error_cls(f"'op read' timed out resolving {setting}") from exc
    key = proc.stdout.strip()
    if proc.returncode != 0 or not key:
        detail = (proc.stderr or "").strip().splitlines()
        raise error_cls(f"'op read' failed for {setting}: {detail[-1] if detail else 'empty result'}")
    return key


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


class ChatLLM:
    """OpenAI-compatible ``POST {base}/chat/completions`` caller.

    Subclasses set the class attributes below and implement ``_resolve``. ``last_error``
    carries the real failure (HTTP status, missing key) because some callers (the
    plugin's ``rewrite_with``) report failures only as a bare ``(None, reason)``.
    """

    label = "LLM"
    error_cls: type[Exception] = LLMError
    key_env_names: tuple[str, ...] = ()
    key_setting = "api_key_ref"

    def __init__(self, cfg: Optional[Config] = None, *, transport: Optional[httpx.BaseTransport] = None) -> None:
        cfg = cfg or load_config()
        self.base_url, self.model, self.key_ref = self._resolve(cfg)
        self.base_url = self.base_url.rstrip("/")
        self.timeout = self._timeout()
        self.last_error: Optional[str] = None
        self._transport = transport
        self._api_key: Optional[str] = None

    # -- configuration (overridden) --

    def _resolve(self, cfg: Config) -> tuple[str, str, Optional[str]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _timeout(self) -> float:
        return env_timeout()

    # -- calls --

    def api_key(self) -> str:
        if self._api_key:
            return self._api_key
        env_key = _first_env(*self.key_env_names)
        if env_key:
            self._api_key = env_key
            return env_key
        if self.key_ref:
            self._api_key = read_op_ref(self.key_ref, setting=self.key_setting, error_cls=self.error_cls)
            return self._api_key
        names = " or ".join(self.key_env_names)
        raise self.error_cls(
            f"no {self.label} LLM key: set {names}, or put an op:// reference in "
            f"~/.config/voxxy/config.toml as {self.key_setting} "
            f"(e.g. {self.key_setting} = \"op://DeLoSecrets/<item-uuid>/credential\")"
        )

    def __call__(self, messages: list[dict], *, timeout: Optional[float] = None, temperature: float = 0.7) -> str:
        self.last_error = None
        try:
            return self._complete(messages, timeout=timeout, temperature=temperature)
        except LLMError as exc:
            self.last_error = str(exc)
            raise
        except self.error_cls as exc:
            self.last_error = str(exc)
            raise

    def _complete(self, messages: list[dict], *, timeout: Optional[float], temperature: float) -> str:
        key = self.api_key()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        err = self.error_cls
        try:
            with httpx.Client(timeout=timeout or self.timeout, transport=self._transport) as http:
                resp = http.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
        except httpx.TimeoutException as exc:
            raise err(f"{self.label} LLM timed out ({exc})") from exc
        except httpx.HTTPError as exc:
            raise err(f"{self.label} LLM unreachable at {self.base_url}: {exc}") from exc
        if resp.status_code >= 400:
            raise err(f"{self.label} LLM returned {resp.status_code}: {resp.text[:200]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise err(f"{self.label} LLM returned an unexpected body: {resp.text[:200]}") from exc
        if isinstance(content, list):  # content-parts shape
            content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
        if not isinstance(content, str) or not content.strip():
            raise err(f"{self.label} LLM returned empty content")
        return content

    def json(
        self, messages: list[dict], *, timeout: Optional[float] = None, temperature: float = 0.2,
        expect: Optional[type] = None,
    ) -> Any:
        """Call the model and parse its reply as JSON (fences / prose tolerated)."""
        return extract_json(self(messages, timeout=timeout, temperature=temperature), expect=expect)


class CloneLLM(ChatLLM):
    """The LLM behind ``voxxy voice clone`` / ``voxxy persona generate``."""

    label = "clone"
    key_env_names = ("VOXXY_CLONE_API_KEY", "VOXXY_PERSONA_API_KEY")
    key_setting = "clone_api_key_ref"

    def _resolve(self, cfg: Config) -> tuple[str, str, Optional[str]]:
        # Endpoint: env(clone) > config(clone) > env(persona) > config(persona).
        # Model: env(clone) > config(clone) > DEFAULT_CLONE_MODEL. It does not
        # inherit the persona model: that is the router's fast tier, too weak for
        # identity resolution and persona authoring.
        base = (
            _first_env("VOXXY_CLONE_BASE_URL")
            or cfg.clone_base_url
            or _first_env("VOXXY_PERSONA_BASE_URL")
            or cfg.persona_base_url
        )
        model = (
            _first_env("VOXXY_CLONE_MODEL")
            or cfg.clone_model
            or DEFAULT_CLONE_MODEL
        )
        self._clone_ref = cfg.clone_api_key_ref or None
        self._persona_ref = cfg.persona_api_key_ref or None
        return base, model, self._clone_ref or self._persona_ref

    def api_key(self) -> str:
        # Same order as the base URL and model: clone env > clone ref > persona env > persona ref.
        # Otherwise an exported persona key would beat a dedicated clone endpoint's own ref
        # and be sent to the wrong service (401).
        if self._api_key:
            return self._api_key
        for env_names, ref, setting in (
            (("VOXXY_CLONE_API_KEY",), None, ""),
            ((), self._clone_ref, "clone_api_key_ref"),
            (("VOXXY_PERSONA_API_KEY",), None, ""),
            ((), self._persona_ref, "persona_api_key_ref"),
        ):
            value = _first_env(*env_names) if env_names else ""
            if not value and ref:
                value = read_op_ref(ref, setting=setting, error_cls=self.error_cls)
            if value:
                self._api_key = value
                return value
        raise self.error_cls(
            "no clone LLM key: set VOXXY_CLONE_API_KEY (or VOXXY_PERSONA_API_KEY), or put an op:// "
            "reference in ~/.config/voxxy/config.toml as clone_api_key_ref "
            "(e.g. clone_api_key_ref = \"op://DeLoSecrets/<item-uuid>/credential\")"
        )

    def _timeout(self) -> float:
        # Authoring a persona is a long generation; the speak-time 40 s cap is too tight.
        return env_timeout("VOXXY_CLONE_TIMEOUT", env_timeout("VOXXY_PERSONA_TIMEOUT", 120.0))


# ---------- JSON extraction ----------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


def _strip_line_comments(text: str) -> str:
    """Remove ``// ...`` comments that sit outside JSON strings (models echo prompt-schema comments)."""
    out: list[str] = []
    in_str = escaped = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == "/" and text[i + 1:i + 2] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def extract_json(text: str, expect: Optional[type] = None) -> Any:
    """Pull the first JSON object/array out of a model reply.

    Handles ``<think>`` blocks, ```json fences, leading/trailing prose, and ``//``
    comments (a model that copies a commented schema example), by scanning for the
    first ``{`` / ``[`` that ``raw_decode`` accepts. With ``expect`` (``dict`` /
    ``list``), a value of another type is skipped: otherwise an unparseable outer
    object would surface as the first list nested inside it. Raises LLMError when
    nothing parses, so callers can fall back instead of crashing.
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMError("empty model reply, expected JSON")
    cleaned = _THINK_RE.sub("", text)
    decoder = json.JSONDecoder()
    for variant in (_strip_line_comments(cleaned), cleaned):
        candidates = [m.group(1) for m in _FENCE_RE.finditer(variant)] + [variant]
        for chunk in candidates:
            for i, ch in enumerate(chunk):
                if ch not in "{[":
                    continue
                try:
                    value, _end = decoder.raw_decode(chunk[i:])
                except ValueError:
                    continue
                if expect is not None and not isinstance(value, expect):
                    continue
                return value
    raise LLMError(f"model reply did not contain valid JSON: {text.strip()[:160]!r}")
