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
    # Never touch the network: by default the voxxy voice record has no persona (frontmatter fallback).
    voxxy: dict = {"record": {}, "calls": []}

    def fake_fetch(base, voice, api_key, timeout):
        voxxy["calls"].append({"base": base, "voice": voice, "api_key": api_key, "timeout": timeout})
        record = voxxy["record"]
        if isinstance(record, Exception):
            raise record
        return record

    monkeypatch.setattr(module, "_fetch_voice_record", fake_fetch)
    monkeypatch.delenv("VOX_URL", raising=False)
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    monkeypatch.delenv("VOX_VOICE", raising=False)
    module._VOXXY_CACHE.clear()
    module.voxxy = voxxy
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
    assert (cfg.timeout_seconds, cfg.max_ratio, cfg.min_ratio) == (40.0, 1.8, 0.5)
    cfg = vp._persona_config({"tts": {"persona": {"enabled": "yes", "timeout_seconds": "bogus", "max_ratio": "2.5",
                                                  "auto_from_voice": "false"}}})
    assert cfg.enabled and cfg.timeout_seconds == 40.0 and cfg.max_ratio == 2.5 and cfg.auto_from_voice is False


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


def test_digit_guard_uses_whole_digit_runs(vp) -> None:
    assert vp.missing_numbers("Retry 4 times", "Retry 14 times") == ["4"]
    assert vp.missing_numbers("Ran 1,2,3", "Ran 1, 2, 3") == []


def test_inline_preamble_is_stripped_but_content_colons_are_kept(vp) -> None:
    assert vp.sanitize_output("Sure, here's the rewritten text: Check it out! Backup done.") == "Check it out! Backup done."
    assert vp.sanitize_output("Here is the version: 4.2.1 is live.") == "Here is the version: 4.2.1 is live."


def test_unparseable_frontmatter_is_logged_once(vp, caplog) -> None:
    import logging
    raw = "---\nvoices: [unclosed\ntemperature: : :\n---\nBody text."
    with caplog.at_level(logging.WARNING):
        meta, body = vp._split_frontmatter(raw)
        vp._split_frontmatter(raw)
    assert meta == {} and body.strip() == "Body text."
    assert sum("frontmatter did not parse" in r.message for r in caplog.records) == 1


# --------------------------------------------------------------------------- voxxy-level association

def _voxxy(vp, persona, **extra):
    vp.voxxy["record"] = {"name": "x", "persona": persona, **extra}


def test_voxxy_record_beats_frontmatter(vp) -> None:
    _cfg(vp)
    _voxxy(vp, "george-carlin")
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "George Carlin"
    assert vp.voxxy["calls"][0]["voice"] == "peter"


def test_voxxy_lookup_gating(vp) -> None:
    _voxxy(vp, "george-carlin")
    # provider None/empty/vox -> lookup; another provider, no voice or auto_from_voice off -> no lookup
    _cfg(vp)
    for provider in (None, "", "VOX"):
        assert vp.resolve_persona(vp._persona_config(), "peter", provider).name == "George Carlin"
    vp._VOXXY_CACHE.clear()
    vp.voxxy["calls"].clear()
    assert vp.resolve_persona(vp._persona_config(), "peter", "elevenlabs").name == "Peter Griffin"
    assert vp.resolve_persona(vp._persona_config(), None, "vox") is None
    _cfg(vp, auto_from_voice=False)
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox") is None
    assert vp.voxxy["calls"] == []


def test_file_and_name_beat_voxxy(vp, tmp_path) -> None:
    _voxxy(vp, "george-carlin")
    custom = tmp_path / "PERSONA.md"
    custom.write_text("---\nname: Custom Guy\n---\nTalks like a pirate.\n")
    _cfg(vp, file=str(custom), name="steve-brule")
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "Custom Guy"
    _cfg(vp, name="steve-brule")
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "Dr. Steve Brule"
    assert vp.voxxy["calls"] == []


def test_voxxy_none_suppresses_frontmatter_fallback(vp) -> None:
    _cfg(vp)
    _voxxy(vp, "none")
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox") is None
    assert vp.transform_tts_text(text=SOURCE, provider="vox", voice="peter") is None
    assert vp.calls == []


def test_voxxy_null_persona_falls_back_to_frontmatter(vp) -> None:
    _cfg(vp)
    _voxxy(vp, None)
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "Peter Griffin"


def test_voxxy_missing_persona_file_is_noop_not_fallback(vp, caplog) -> None:
    import logging
    _cfg(vp)
    _voxxy(vp, "ghost-persona")
    with caplog.at_level(logging.WARNING):
        assert vp.resolve_persona(vp._persona_config(), "peter", "vox") is None
        assert vp.resolve_persona(vp._persona_config(), "peter", "vox") is None
    assert sum("ghost-persona" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("failure", [OSError("connection refused"), ValueError("bad json"), TimeoutError("slow")])
def test_voxxy_failure_falls_back_and_logs_once(vp, caplog, failure) -> None:
    import logging
    _cfg(vp)
    vp.voxxy["record"] = failure
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            vp._VOXXY_CACHE.clear()
            assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "Peter Griffin"
    assert sum("voxxy lookup" in r.message for r in caplog.records) == 1


def test_voxxy_unusable_slug_falls_back(vp) -> None:
    _cfg(vp)
    for bad in ("../etc/passwd", 7, ["x"]):
        vp._VOXXY_CACHE.clear()
        _voxxy(vp, bad)
        assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "Peter Griffin"


def test_voxxy_cache_ttl_includes_negative_results(vp, monkeypatch) -> None:
    _cfg(vp)
    clock = {"now": 1000.0}
    monkeypatch.setattr(vp.time, "monotonic", lambda: clock["now"])
    vp.voxxy["record"] = OSError("down")
    for _ in range(3):
        vp.resolve_persona(vp._persona_config(), "peter", "vox")
    assert len(vp.voxxy["calls"]) == 1  # the failure itself is cached
    clock["now"] += vp.VOXXY_CACHE_TTL_SECONDS + 1
    _voxxy(vp, "george-carlin")
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "George Carlin"
    assert len(vp.voxxy["calls"]) == 2
    vp.resolve_persona(vp._persona_config(), "peter", "vox")
    assert len(vp.voxxy["calls"]) == 2
    vp.resolve_persona(vp._persona_config(), "rick", "vox")  # keyed per voice
    assert len(vp.voxxy["calls"]) == 3


def test_vox_settings_resolution(vp, monkeypatch) -> None:
    assert vp._vox_settings() == ("https://vox.delo.sh", None)
    monkeypatch.setenv("VOX_URL", "http://127.0.0.1:9999/")
    monkeypatch.setenv("VOX_API_KEY", "env-key")
    assert vp._vox_settings() == ("http://127.0.0.1:9999", "env-key")
    vp.state["tts"]["vox"] = {"base_url": "http://cfg.example/", "api_key": "cfg-key"}
    assert vp._vox_settings() == ("http://cfg.example", "cfg-key")


def test_voxxy_timeout_config_clamped(vp) -> None:
    assert vp._persona_config({}).voxxy_timeout_seconds == 3.0
    for raw, want in ((0.01, 0.5), (99, 15.0), ("1.5", 1.5), ("junk", 3.0)):
        cfg = vp._persona_config({"tts": {"persona": {"voxxy_timeout_seconds": raw}}})
        assert cfg.voxxy_timeout_seconds == want
    _cfg(vp, voxxy_timeout_seconds=7)
    vp.resolve_persona(vp._persona_config(), "peter", "vox")
    assert vp.voxxy["calls"][0]["timeout"] == 7


def test_voxxy_lookup_has_a_hard_deadline(vp, monkeypatch) -> None:
    # urllib's timeout does not cover DNS or a slow drip: a hung fetch must not hold the hook.
    _cfg(vp, voxxy_timeout_seconds=0.5)
    monkeypatch.setattr(vp, "_fetch_voice_record", lambda *a, **k: time.sleep(5))
    started = time.monotonic()
    persona = vp.resolve_persona(vp._persona_config(), "peter", "vox")
    assert time.monotonic() - started < 2.5
    assert persona.name == "Peter Griffin"  # fell back to frontmatter
    started = time.monotonic()
    vp.resolve_persona(vp._persona_config(), "peter", "vox")
    assert time.monotonic() - started < 0.3  # the failure is cached


def test_vox_voice_env_pin_decides_the_voice_for_the_vox_provider(vp, monkeypatch) -> None:
    # plugins/tts/vox speaks $VOX_VOICE when set, so the persona must follow the pinned voice.
    _cfg(vp)
    monkeypatch.setenv("VOX_VOICE", "carlin")
    assert vp.resolve_persona(vp._persona_config(), "peter", "vox").name == "George Carlin"
    assert vp.voxxy["calls"][0]["voice"] == "carlin"
    # other providers do not read VOX_VOICE
    assert vp.resolve_persona(vp._persona_config(), "peter", "elevenlabs").name == "Peter Griffin"


def test_hook_uses_voxxy_persona_end_to_end(vp) -> None:
    _cfg(vp)
    _voxxy(vp, "steve-brule")
    assert vp.transform_tts_text(text=SOURCE, provider="vox", voice="peter")
    assert "Steve Brule" in vp.calls[0]["messages"][0]["content"]


def test_real_http_lookup_against_local_server(vp, monkeypatch) -> None:
    """The unstubbed urllib path: URL shape, auth headers, 404 and non-JSON bodies."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization"), self.headers.get("X-API-Key")))
            if self.path == "/voices/peter":
                body, code = json.dumps({"name": "peter", "persona": "george-carlin"}).encode(), 200
            elif self.path == "/voices/junk":
                body, code = b"<html>", 200
            else:
                body, code = b'{"detail": "nope"}', 404
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("VOX_URL", base)
        monkeypatch.setenv("VOX_API_KEY", "k3y")
        monkeypatch.setattr(vp, "_fetch_voice_record", _load_plugin_module()._fetch_voice_record)  # un-stub
        assert vp.voxxy_persona_for_voice("peter") == "george-carlin"
        assert seen[0] == ("/voices/peter", "Bearer k3y", "k3y")
        assert vp.voxxy_persona_for_voice("ghost") is None  # 404
        assert vp.voxxy_persona_for_voice("junk") is None  # not JSON
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- public API for the CLI

def test_public_api_surface() -> None:
    vp = _load_plugin_module()
    for name in vp.__all__:
        assert hasattr(vp, name), name
    for name in ("list_personas", "persona_by_id", "frontmatter_persona_for_voice", "rewrite_with",
                 "load_persona", "build_messages", "sanitize_output", "validate_rewrite", "PersonaConfig",
                 "PERSONAS_DIR", "PROMPT_VERSION"):
        assert name in vp.__all__, name


def test_list_personas_and_persona_by_id() -> None:
    vp = _load_plugin_module()
    assert [p.id for p in vp.list_personas()] == ["george-carlin", "peter-griffin", "steve-brule"]
    assert vp.persona_by_id("peter-griffin").name == "Peter Griffin"
    for bad in ("nope", "../x", "", None, "-x"):
        assert vp.persona_by_id(bad) is None


def test_frontmatter_persona_for_voice() -> None:
    vp = _load_plugin_module()
    assert vp.frontmatter_persona_for_voice("Peter").id == "peter-griffin"
    assert vp.frontmatter_persona_for_voice("peter.wav").id == "peter-griffin"
    assert vp.frontmatter_persona_for_voice("rick") is None
    assert vp.frontmatter_persona_for_voice(None) is None


def test_rewrite_with_injected_caller(vp) -> None:
    persona = vp.persona_by_id("peter-griffin")
    cfg = vp.PersonaConfig(timeout_seconds=9)
    got = {}
    good = "Freakin' sweet, the build finished in 42 seconds and all 118 tests passed. I pushed the changes to main."

    def call(messages, *, timeout, temperature):
        got.update(messages=messages, timeout=timeout, temperature=temperature)
        return f'"{good}"'

    assert vp.rewrite_with(SOURCE, persona, cfg, call) == (good, None)
    assert got["timeout"] == 9 and got["temperature"] == 0.8 and SOURCE in got["messages"][1]["content"]

    replacement, reason = vp.rewrite_with(SOURCE, persona, cfg, lambda m, **k: "42 118 ok")
    assert replacement is None and reason.startswith("too short")
    replacement, reason = vp.rewrite_with(SOURCE, persona, cfg, lambda m, **k: "Holy crap. " * 60)
    assert replacement is None and reason.startswith("too long")
    replacement, reason = vp.rewrite_with(SOURCE, persona, cfg, lambda m, **k: None)
    assert replacement is None and reason == vp.NO_RESPONSE

    def boom(messages, **kw):
        raise RuntimeError("gateway down")

    replacement, reason = vp.rewrite_with(SOURCE, persona, cfg, boom)
    assert replacement is None and "gateway down" in reason


def test_rewrite_text_goes_through_rewrite_with(vp, monkeypatch) -> None:
    seen = {}

    def fake(text, persona, cfg, call):
        seen.update(text=text, persona=persona.name, call=call)
        return "ok", None

    monkeypatch.setattr(vp, "rewrite_with", fake)
    assert vp.rewrite_text(SOURCE, vp.persona_by_id("peter-griffin"), vp.PersonaConfig()) == "ok"
    assert seen["call"] is vp._call_with_deadline
