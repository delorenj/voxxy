"""Bridge between the voxxy CLI and the Hermes ``voice-persona`` plugin.

Design notes:
- Persona logic (frontmatter parsing, prompt, sanitizing, fact-preservation
  validation) lives in ``plugins/voice-persona/__init__.py`` and is NOT
  reimplemented here. That module imports cleanly without Hermes, so the CLI
  loads it by file path from the discovered project root and uses its public
  API. One rewrite+validate path means the CLI dry-run and the Hermes TTS hook
  can never disagree about what is acceptable.
- The only thing the CLI supplies is the injected LLM caller: an
  OpenAI-compatible ``POST {base}/chat/completions`` over httpx. Hermes has its
  own auxiliary-client caller; the CLI runs outside Hermes.
- The API key is never stored raw. ``VOXXY_PERSONA_API_KEY`` wins, then an
  ``op://`` reference from config.toml (``persona_api_key_ref``) is resolved with
  ``op read`` at call time.
- Everything that can fail at speak time raises PersonaError so ``speak`` can
  fail open (speak the original text) with a single warning.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess  # noqa: F401 - tests patch bridge.subprocess.run (shared with voxxy.llm)
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from voxxy.config import Config, discover_project_root
from voxxy.llm import ChatLLM, LLMError

PLUGIN_MODULE_NAME = "voxxy_voice_persona_plugin"
PLUGIN_RELPATH = Path("plugins") / "voice-persona" / "__init__.py"

# Same shape the plugin (_NAME_RE) and the server accept for a persona slug.
PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# Stored in voices.persona to mean "explicitly no persona" (suppresses the
# frontmatter fallback).
NONE_SENTINEL = "none"

_plugin_cache: dict[str, Any] = {}


class PersonaError(LLMError):
    """Anything that stops a persona operation (plugin missing, LLM failure, ...)."""


# ---------- plugin loading ----------


def load_plugin(project_root: Optional[Path] = None) -> Any:
    """Import plugins/voice-persona/__init__.py by file path and return the module.

    Raises ProjectNotFound (from config) when no project root is resolvable and
    PersonaError when the root exists but has no plugin. Cached per file path.
    """
    root = project_root if project_root is not None else discover_project_root()
    path = (root / PLUGIN_RELPATH).resolve()
    key = str(path)
    if key in _plugin_cache:
        return _plugin_cache[key]
    if not path.is_file():
        raise PersonaError(
            f"voice-persona plugin not found at {path}. "
            "Is this a current checkout of the voxxy repository?"
        )
    spec = importlib.util.spec_from_file_location(PLUGIN_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise PersonaError(f"cannot import voice-persona plugin from {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the plugin defines dataclasses, which resolve their
    # string annotations through sys.modules[cls.__module__].
    sys.modules[PLUGIN_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(PLUGIN_MODULE_NAME, None)
        raise PersonaError(f"voice-persona plugin failed to import: {exc}") from exc
    _plugin_cache[key] = module
    return module


def try_load_plugin() -> tuple[Optional[Any], Optional[str]]:
    """Tolerant variant for display paths: (module, None) or (None, reason)."""
    try:
        return load_plugin(), None
    except PersonaError as exc:
        return None, str(exc)
    except Exception as exc:  # ProjectNotFound and friends
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return None, first


def persona_id(persona: Any) -> str:
    """The id of a bundled persona: its file stem (what voices.persona stores)."""
    path = getattr(persona, "path", None)
    if path:
        return Path(path).stem
    return str(getattr(persona, "name", "")).strip().lower().replace(" ", "-")


def persona_by_id(plugin: Any, slug: str) -> Optional[Any]:
    """Bundled persona for a slug, or None. Never raises on a bad slug."""
    if not PERSONA_ID_RE.match(slug or ""):
        return None
    finder = getattr(plugin, "persona_by_id", None)
    if callable(finder):
        return finder(slug)
    return plugin.load_persona(plugin.PERSONAS_DIR / f"{slug}.md")


def frontmatter_persona_for_voice(plugin: Any, voice: str) -> Optional[Any]:
    """The persona whose frontmatter ``voices:`` lists this voice, or None."""
    finder = getattr(plugin, "frontmatter_persona_for_voice", None)
    if callable(finder):
        return finder(voice)
    wanted = (voice or "").strip().lower()
    for persona in plugin.list_personas():
        if wanted and wanted in getattr(persona, "voices", ()):
            return persona
    return None


# ---------- effective persona ----------


@dataclass
class Effective:
    """What persona a voice ends up with, and why.

    ``source`` is ``voxxy`` (voices.persona record), ``frontmatter`` (persona
    file ``voices:``), ``none`` (nothing applies, or explicitly suppressed) or
    ``unknown`` (no record and the plugin could not be loaded to check).
    """

    persona_id: Optional[str]
    source: str
    explicit_none: bool = False
    persona: Any = field(default=None, repr=False)
    problem: Optional[str] = None

    def label(self) -> str:
        if self.persona_id:
            return f"{self.persona_id} ({self.source})"
        if self.explicit_none:
            return "none (explicit)"
        return "unknown" if self.source == "unknown" else "none"

    def as_dict(self) -> dict:
        return {
            "persona": self.persona_id,
            "source": self.source,
            "explicit_none": self.explicit_none,
            "problem": self.problem,
        }


def effective_persona(voice: str, record_persona: Optional[str], plugin: Optional[Any]) -> Effective:
    """Resolve a voice's persona: voxxy record > frontmatter default > none.

    Mirrors the Hermes plugin's order for the voxxy/frontmatter stages.
    ``plugin`` may be None (project root not resolvable): a voxxy record still
    resolves, but the frontmatter fallback cannot be checked.
    """
    if record_persona == NONE_SENTINEL:
        return Effective(None, "none", explicit_none=True)
    if record_persona:
        persona = persona_by_id(plugin, record_persona) if plugin is not None else None
        problem = None
        if plugin is not None and persona is None:
            problem = f"no bundled persona '{record_persona}'"
        return Effective(record_persona, "voxxy", persona=persona, problem=problem)
    if plugin is None:
        return Effective(None, "unknown")
    persona = frontmatter_persona_for_voice(plugin, voice)
    if persona is not None:
        return Effective(persona_id(persona), "frontmatter", persona=persona)
    return Effective(None, "none")


# ---------- LLM caller ----------


class PersonaLLM(ChatLLM):
    """OpenAI-compatible chat caller injected into the plugin's ``rewrite_with``.

    The HTTP/auth mechanics are shared with the clone pipeline (:mod:`voxxy.llm`).
    Config precedence per setting: env > config.toml > built-in default.
    ``last_error`` carries the real failure (HTTP status, missing key) because
    ``rewrite_with`` reports failures as a bare ``(None, reason)``.
    """

    label = "persona"
    error_cls = PersonaError
    key_env_names = ("VOXXY_PERSONA_API_KEY",)
    key_setting = "persona_api_key_ref"

    def _resolve(self, cfg: Config) -> tuple[str, str, Optional[str]]:
        return (
            os.environ.get("VOXXY_PERSONA_BASE_URL") or cfg.persona_base_url,
            os.environ.get("VOXXY_PERSONA_MODEL") or cfg.persona_model,
            cfg.persona_api_key_ref,
        )


# ---------- rewrite ----------


@dataclass
class RewriteResult:
    original: str
    rewrite: Optional[str]
    accepted: bool
    reason: Optional[str]
    persona: str
    model: str
    prompt_version: str
    elapsed_s: float

    def as_dict(self) -> dict:
        return {
            "original": self.original,
            "rewrite": self.rewrite,
            "accepted": self.accepted,
            "reason": self.reason,
            "persona": self.persona,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "elapsed_s": round(self.elapsed_s, 3),
        }


def rewrite(plugin: Any, persona: Any, text: str, llm: Optional[PersonaLLM] = None) -> RewriteResult:
    """One in-character rewrite through the plugin's rewrite_with. Never raises."""
    llm = llm or PersonaLLM()
    pcfg = plugin.PersonaConfig(enabled=True, timeout_seconds=llm.timeout)
    started = time.monotonic()
    replacement: Optional[str] = None
    reason: Optional[str] = None
    try:
        replacement, reason = plugin.rewrite_with(text, persona, pcfg, llm)
    except Exception as exc:  # noqa: BLE001 - dry run reports, speak fails open
        reason = str(exc) or type(exc).__name__
    if replacement is None:
        reason = reason or llm.last_error or "no rewrite produced"
    return RewriteResult(
        original=text,
        rewrite=replacement,
        accepted=replacement is not None,
        reason=None if replacement is not None else reason,
        persona=persona_id(persona),
        model=llm.model,
        prompt_version=str(getattr(plugin, "PROMPT_VERSION", "")),
        elapsed_s=time.monotonic() - started,
    )
