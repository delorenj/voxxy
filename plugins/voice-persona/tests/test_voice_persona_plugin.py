from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

SOURCE = "The build finished in 42 seconds and all 118 tests passed. I pushed the changes to main."


def _load_plugin_module():
    name = "voice_persona_plugin_under_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def vp(monkeypatch, tmp_path):
    module = _load_plugin_module()
    state = {"tts": {"persona": {"enabled": True, "name": "peter-griffin"}}}
    calls: list = []

    def fake_llm(messages, *, timeout, temperature):
        calls.append({"messages": messages, "timeout": timeout, "temperature": temperature})
        return state.get("reply", "Freakin' sweet, the build finished in 42 seconds and all 118 tests passed. "
                                  "I pushed the changes to main. Hehehehe.")

    monkeypatch.setattr(module, "_load_config", lambda: state)
    monkeypatch.setattr(module, "_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "_call_llm_text", fake_llm)
    module.state = state
    module.calls = calls
    return module


def _cfg(vp, **persona):
    vp.state["tts"]["persona"] = {"enabled": True, **persona}


def test_manifest() -> None:
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text())
    assert manifest["name"] == "voice-persona"
    assert manifest["kind"] == "standalone"
    assert manifest["provides_hooks"] == ["transform_tts_text"]
    assert manifest["version"] == "0.1.0"


def test_register_hooks_transform_tts_text(vp) -> None:
    registered = {}

    class Ctx:
        def register_hook(self, name, callback):
            registered[name] = callback

    vp.register(Ctx())
    assert registered["transform_tts_text"] is vp.transform_tts_text


def test_bundled_personas_parse() -> None:
    vp = _load_plugin_module()
    expected = {"peter-griffin": "peter", "george-carlin": "carlin", "steve-brule": "brule"}
    for stem, voice in expected.items():
        persona = vp.load_persona(vp.PERSONAS_DIR / f"{stem}.md")
        assert persona is not None, stem
        assert persona.body.strip() and len(persona.body) > 1500
        assert voice in persona.voices
        assert "Rules for rewriting" in persona.body
    brule = vp.load_persona(vp.PERSONAS_DIR / "steve-brule.md").body
    assert "preetza" in brule and "this is known as harp" in brule and "instrument of harp" in brule


def test_rewrite_accepted_and_prompt_contents(vp) -> None:
    result = vp.transform_tts_text(text=SOURCE, provider="vox", voice="peter", platform="telegram")
    assert result and "Freakin' sweet" in result
    call = vp.calls[0]
    assert call["timeout"] == vp.DEFAULT_TIMEOUT_SECONDS
    system, user = call["messages"]
    assert "Peter Griffin" in system["content"] and "Preserve EVERY fact" in system["content"]
    assert SOURCE in user["content"]
    assert vp.PROMPT_VERSION


def test_disabled_is_noop(vp) -> None:
    _cfg(vp, enabled=False, name="peter-griffin")
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    assert vp.calls == []
    vp.state["tts"] = {}
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None


def test_blank_or_non_string_text_noop(vp) -> None:
    assert vp.transform_tts_text(text="   ") is None
    assert vp.transform_tts_text(text=None) is None
    assert vp.transform_tts_text(text=123) is None
    assert vp.calls == []


def test_missing_persona_is_noop(vp) -> None:
    _cfg(vp, name="does-not-exist")
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    _cfg(vp, name="../etc/passwd")
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    _cfg(vp, file="nope/PERSONA.md")
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    assert vp.calls == []


@pytest.mark.parametrize("voice,persona", [("peter", "Peter Griffin"), ("carlin", "George Carlin"),
                                           ("brule", "Dr. Steve Brule"), ("Peter", "Peter Griffin")])
def test_auto_from_voice_mapping(vp, voice, persona) -> None:
    _cfg(vp)
    assert vp.resolve_persona(vp._persona_config(), voice).name == persona


def test_auto_from_voice_unknown_voice_and_switch(vp) -> None:
    _cfg(vp)
    assert vp.resolve_persona(vp._persona_config(), "rick") is None
    assert vp.resolve_persona(vp._persona_config(), None) is None
    _cfg(vp, auto_from_voice=False)
    assert vp.resolve_persona(vp._persona_config(), "peter") is None


def test_resolution_order_file_over_name_over_auto(vp, tmp_path) -> None:
    custom = tmp_path / "PERSONA.md"
    custom.write_text("---\nname: Custom Guy\nvoices: [custom]\n---\nTalks like a pirate.\n")
    _cfg(vp, file=str(custom), name="george-carlin")
    assert vp.resolve_persona(vp._persona_config(), "brule").name == "Custom Guy"
    _cfg(vp, name="george-carlin")
    assert vp.resolve_persona(vp._persona_config(), "brule").name == "George Carlin"
    _cfg(vp)
    assert vp.resolve_persona(vp._persona_config(), "brule").name == "Dr. Steve Brule"


def test_profile_relative_file_resolution(vp, tmp_path) -> None:
    profile_a = tmp_path / "a"
    profile_b = tmp_path / "b"
    for home, who in ((profile_a, "Alpha"), (profile_b, "Beta")):
        home.mkdir()
        (home / "PERSONA.md").write_text(f"---\nname: {who}\n---\nI am {who}.\n")
    _cfg(vp, file="PERSONA.md")
    vp._hermes_home = lambda: profile_a
    assert vp.resolve_persona(vp._persona_config(), None).name == "Alpha"
    vp._hermes_home = lambda: profile_b
    assert vp.resolve_persona(vp._persona_config(), None).name == "Beta"


def test_persona_without_frontmatter_and_cache_invalidation(vp, tmp_path) -> None:
    path = tmp_path / "plain.md"
    path.write_text("Just a body.\n")
    persona = vp.load_persona(path)
    assert persona.name == "plain" and persona.voices == ()
    assert vp.load_persona(path) is persona  # cached
    path.write_text("A different body.\n")
    import os
    os.utime(path, ns=(time.time_ns() + 5_000_000_000, time.time_ns() + 5_000_000_000))
    assert vp.load_persona(path).body == "A different body."
    path.write_text("   \n")
    os.utime(path, ns=(time.time_ns() + 9_000_000_000, time.time_ns() + 9_000_000_000))
    assert vp.load_persona(path) is None


def test_config_defaults_and_coercion(vp) -> None:
    cfg = vp._persona_config({})
    assert cfg.enabled is False and cfg.auto_from_voice is True
    assert (cfg.timeout_seconds, cfg.max_ratio, cfg.min_ratio) == (25.0, 1.8, 0.5)
    cfg = vp._persona_config({"tts": {"persona": {"enabled": "yes", "timeout_seconds": "bogus", "max_ratio": "2.5",
                                                  "auto_from_voice": "false"}}})
    assert cfg.enabled and cfg.timeout_seconds == 25.0 and cfg.max_ratio == 2.5 and cfg.auto_from_voice is False


def test_ratio_rejects(vp) -> None:
    vp.state["reply"] = "Holy crap. " * 60 + "42 118"
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    vp.state["reply"] = "42 118 ok"
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    # Tunable: a looser max_ratio lets the long one through.
    _cfg(vp, name="peter-griffin", max_ratio=10)
    vp.state["reply"] = "Holy crap. " * 30 + "42 118"
    assert vp.transform_tts_text(text=SOURCE, voice="peter")


def test_digit_guard_rejects_dropped_numbers(vp) -> None:
    vp.state["reply"] = "Freakin' sweet, the build finished quickly and the tests passed. I pushed the changes to main."
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    vp.state["reply"] = "Freakin' sweet, the build finished in 42 seconds and the tests passed. I pushed the changes to main."
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None  # 118 missing = 50%


def test_digit_guard_tolerates_thousands_separators_and_small_misses(vp) -> None:
    assert vp.missing_numbers("Wrote 1,000 rows", "wrote 1000 rows") == []
    source = "Items: " + " ".join(str(n) for n in range(10, 30))
    reply = "Items: " + " ".join(str(n) for n in range(10, 29))  # one of 20 missing = 5%
    cfg = vp.PersonaConfig()
    assert vp.validate_rewrite(source, reply, cfg) is None


def test_preamble_and_quote_stripping(vp) -> None:
    body = "Freakin' sweet, the build finished in 42 seconds and all 118 tests passed. I pushed the changes to main."
    vp.state["reply"] = f"Sure, here's the rewritten text:\n\n\"{body}\""
    assert vp.transform_tts_text(text=SOURCE, voice="peter") == body
    vp.state["reply"] = f"“{body}”"
    assert vp.transform_tts_text(text=SOURCE, voice="peter") == body
    vp.state["reply"] = f"<think>hmm</think>{body}"
    assert vp.transform_tts_text(text=SOURCE, voice="peter") == body


def test_sanitize_keeps_legit_content() -> None:
    vp = _load_plugin_module()
    assert vp.sanitize_output("Here's what happened:\nit broke") == "Here's what happened:\nit broke"
    assert vp.sanitize_output('"One" and "two"') == '"One" and "two"'


def test_empty_llm_reply_is_noop(vp) -> None:
    vp.state["reply"] = "   "
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None


def test_llm_exception_returns_none(vp, monkeypatch) -> None:
    def boom(messages, *, timeout, temperature):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(vp, "_call_llm_text", boom)
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None


def test_unexpected_internal_error_returns_none(vp, monkeypatch) -> None:
    monkeypatch.setattr(vp, "resolve_persona", lambda *a, **k: 1 / 0)
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None


def test_timeout_honored(vp, monkeypatch) -> None:
    def slow(messages, *, timeout, temperature):
        time.sleep(3)
        return SOURCE

    monkeypatch.setattr(vp, "_call_llm_text", slow)
    _cfg(vp, name="peter-griffin", timeout_seconds=1)
    started = time.monotonic()
    assert vp.transform_tts_text(text=SOURCE, voice="peter") is None
    assert time.monotonic() - started < 2.5


def test_response_text_extraction() -> None:
    vp = _load_plugin_module()

    class Msg:
        content = "hi"
        reasoning_content = "SECRET CHAIN OF THOUGHT"

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]

    assert vp._response_text(Resp()) == "hi"
    assert vp._response_text({"choices": [{"message": {"content": [{"text": "a"}, {"text": "b"}]}}]}) == "ab"
    assert vp._response_text(None) == ""
    assert vp._response_text({"choices": []}) == ""


def test_temperature_from_frontmatter(vp) -> None:
    vp.transform_tts_text(text=SOURCE, voice="peter")
    assert vp.calls[0]["temperature"] == 0.8
