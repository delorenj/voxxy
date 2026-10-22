"""Tests for the persona bridge, config keys, client PUT, and the persona/voice/speak CLI.

The plugin under test is the real ``plugins/voice-persona`` from this repo
(loaded by file path, exactly as the CLI does it); only HTTP and the LLM are
stubbed. Skipped as a group when the plugin is not present in the checkout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import httpx
import pytest
from typer.testing import CliRunner

import voxxy.commands.persona as persona_cmd
import voxxy.commands.speak as speak_mod
import voxxy.commands.voice as voice_cmd
import voxxy.persona as bridge
from voxxy.app import app
from voxxy.client import VoiceOut, VoxClient, VoxNotFound, VoxValidationError
from voxxy.config import DEFAULT_PERSONA_BASE_URL, DEFAULT_PERSONA_MODEL, Config, load_config, save_config

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_FILE = REPO_ROOT / bridge.PLUGIN_RELPATH

pytestmark = pytest.mark.skipif(not PLUGIN_FILE.is_file(), reason="voice-persona plugin not in this checkout")

runner = CliRunner()
RealLLM = bridge.PersonaLLM  # the autouse fixture swaps bridge.PersonaLLM for a stub


def _voice(name: str, persona: Optional[str] = None, **kw) -> VoiceOut:
    return VoiceOut(name=name, display_name=name.title(), duration_s=3.0, persona=persona, **kw)


class FakeVox:
    """Stand-in for VoxClient in command modules: an in-memory voices table."""

    voices: dict[str, VoiceOut] = {}
    puts: list[tuple[str, Optional[str]]] = []

    def __init__(self, *_a, **_k) -> None:
        pass

    def list_voices(self):
        return list(self.voices.values())

    def get_voice(self, name):
        if name not in self.voices:
            raise VoxNotFound(name)
        return self.voices[name]

    def set_voice_persona(self, name, persona):
        if name not in self.voices:
            raise VoxNotFound(name)
        self.puts.append((name, persona))
        self.voices[name] = self.voices[name].model_copy(update={"persona": persona})
        return self.voices[name]


class StubLLM:
    """PersonaLLM replacement: canned reply, records the calls."""

    reply: object = "Holy crap, the deploy took 42 seconds, freakin' sweet!"
    calls: list = []
    last_error = None

    def __init__(self, *_a, **_k) -> None:
        self.model = "stub-model"
        self.timeout = 5.0
        self.base_url = "http://stub"

    def __call__(self, messages, *, timeout=None, temperature=0.7):
        StubLLM.calls.append({"messages": messages, "timeout": timeout, "temperature": temperature})
        if isinstance(StubLLM.reply, Exception):
            self.last_error = str(StubLLM.reply)
            raise StubLLM.reply
        return StubLLM.reply


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the CLI at the real repo as project root, isolate config + env."""
    monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setenv("VOXXY_HOME", str(REPO_ROOT))
    for var in ("VOX_PERSONA", "VOXXY_PERSONA_BASE_URL", "VOXXY_PERSONA_MODEL",
                "VOXXY_PERSONA_API_KEY", "VOXXY_PERSONA_TIMEOUT", "VOX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    FakeVox.voices = {
        "peter": _voice("peter"),                       # no record -> frontmatter (peter-griffin)
        "brule": _voice("brule", persona="none"),       # explicit none
        "rick": _voice("rick", persona="george-carlin"),  # voxxy record wins
        "plain": _voice("plain"),                       # nothing anywhere
    }
    FakeVox.puts = []
    StubLLM.reply = "Holy crap, the deploy took 42 seconds, freakin' sweet!"
    StubLLM.calls = []
    monkeypatch.setattr(voice_cmd, "VoxClient", FakeVox)
    monkeypatch.setattr(persona_cmd, "VoxClient", FakeVox)
    monkeypatch.setattr(speak_mod, "VoxClient", FakeVox)
    monkeypatch.setattr(persona_cmd, "PersonaLLM", StubLLM)
    monkeypatch.setattr(bridge, "PersonaLLM", StubLLM)


@pytest.fixture()
def plugin():
    return bridge.load_plugin(REPO_ROOT)


# ---------------------------------------------------------------------------
# config + client
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults(self) -> None:
        cfg = load_config()
        assert cfg.persona_base_url == "https://api.automaticai.io/v1"
        assert cfg.persona_model == "automaticai/personal/glm-5.3-flash"
        assert cfg.persona_api_key_ref is None

    def test_old_config_file_still_loads(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text('default_url = "http://x"\ndefault_voice = "v"\n')
        cfg = load_config()
        assert cfg.default_url == "http://x"
        assert cfg.persona_model == DEFAULT_PERSONA_MODEL and cfg.persona_base_url == DEFAULT_PERSONA_BASE_URL

    def test_config_with_only_persona_keys_keeps_url_and_voice_defaults(self, tmp_path: Path) -> None:
        # Config is slots=True: Config.default_url is a slot descriptor, not the default string.
        (tmp_path / "config.toml").write_text('persona_api_key_ref = "op://V/i/credential"\n')
        cfg = load_config()
        assert cfg.default_url == "https://vox.delo.sh" and cfg.default_voice == "rick"
        assert cfg.persona_api_key_ref == "op://V/i/credential"

    def test_round_trip(self) -> None:
        save_config(Config(persona_base_url="http://llm/v1", persona_model="m", persona_api_key_ref="op://V/i/credential"))
        cfg = load_config()
        assert (cfg.persona_base_url, cfg.persona_model) == ("http://llm/v1", "m")
        assert cfg.persona_api_key_ref == "op://V/i/credential"

    def test_defaults_not_written(self, tmp_path: Path) -> None:
        save_config(Config())
        assert "persona" not in (tmp_path / "config.toml").read_text()


class TestClient:
    def test_voiceout_tolerates_old_server(self) -> None:
        v = VoiceOut.model_validate({"name": "a", "display_name": "A", "duration_s": 1.0})
        assert v.persona is None

    @pytest.mark.parametrize("value", ["peter-griffin", "none", None])
    def test_set_voice_persona_puts_body(self, value) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"name": "peter", "display_name": "Peter", "duration_s": 3, "persona": value})

        client = VoxClient("http://t", transport=httpx.MockTransport(handler), api_key=None)
        out = client.set_voice_persona("peter", value)
        assert seen == [("PUT", "/voices/peter/persona", {"persona": value})]
        assert out.persona == value

    def test_set_voice_persona_errors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404 if "ghost" in request.url.path else 422, json={"detail": "x"})

        client = VoxClient("http://t", transport=httpx.MockTransport(handler), api_key=None)
        with pytest.raises(VoxNotFound):
            client.set_voice_persona("ghost", "x")
        with pytest.raises(VoxValidationError):
            client.set_voice_persona("peter", "bad slug")


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------


class TestBridge:
    def test_load_plugin_missing(self, tmp_path: Path) -> None:
        with pytest.raises(bridge.PersonaError, match="plugin not found"):
            bridge.load_plugin(tmp_path)

    def test_load_plugin_no_project_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from voxxy.config import ProjectNotFound
        monkeypatch.delenv("VOXXY_HOME")
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ProjectNotFound):
            bridge.load_plugin()
        plugin, why = bridge.try_load_plugin()
        assert plugin is None and why

    def test_persona_by_id(self, plugin) -> None:
        assert bridge.persona_id(bridge.persona_by_id(plugin, "peter-griffin")) == "peter-griffin"
        assert bridge.persona_by_id(plugin, "nope") is None
        assert bridge.persona_by_id(plugin, "../etc/passwd") is None

    def test_effective_matrix(self, plugin) -> None:
        e = bridge.effective_persona("peter", None, plugin)
        assert (e.persona_id, e.source) == ("peter-griffin", "frontmatter")
        e = bridge.effective_persona("peter", "george-carlin", plugin)
        assert (e.persona_id, e.source) == ("george-carlin", "voxxy")
        e = bridge.effective_persona("peter", "none", plugin)
        assert e.persona_id is None and e.explicit_none and e.label() == "none (explicit)"
        e = bridge.effective_persona("plain", None, plugin)
        assert e.persona_id is None and e.source == "none" and not e.explicit_none
        e = bridge.effective_persona("peter", "ghost", plugin)
        assert e.source == "voxxy" and e.persona is None and "ghost" in e.problem
        e = bridge.effective_persona("peter", None, None)
        assert e.source == "unknown"
        assert bridge.effective_persona("rick", "george-carlin", None).source == "voxxy"


def _llm(handler, **env_cfg) -> bridge.PersonaLLM:
    return RealLLM(Config(**env_cfg), transport=httpx.MockTransport(handler))


class TestLLM:
    def test_posts_openai_chat_completion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_API_KEY", "k-env")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "hi there"}}]})

        llm = _llm(handler)
        assert llm([{"role": "user", "content": "x"}], timeout=3, temperature=0.4) == "hi there"
        assert seen["url"] == "https://api.automaticai.io/v1/chat/completions"
        assert seen["auth"] == "Bearer k-env"
        assert seen["body"]["model"] == "automaticai/personal/glm-5.3-flash"
        assert seen["body"]["temperature"] == 0.4

    def test_env_overrides_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_BASE_URL", "http://env/v1/")
        monkeypatch.setenv("VOXXY_PERSONA_MODEL", "env-model")
        monkeypatch.setenv("VOXXY_PERSONA_TIMEOUT", "7")
        llm = RealLLM(Config(persona_base_url="http://cfg/v1", persona_model="cfg-model"))
        assert (llm.base_url, llm.model, llm.timeout) == ("http://env/v1", "env-model", 7.0)

    def test_config_used_without_env(self) -> None:
        llm = RealLLM(Config(persona_base_url="http://cfg/v1", persona_model="cfg-model"))
        assert (llm.base_url, llm.model, llm.timeout) == ("http://cfg/v1", "cfg-model", 40.0)

    def test_no_key_explains_configuration(self) -> None:
        llm = _llm(lambda r: httpx.Response(200))
        with pytest.raises(bridge.PersonaError, match="VOXXY_PERSONA_API_KEY"):
            llm([{"role": "user", "content": "x"}])

    def test_key_resolved_from_op_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        class Proc:
            returncode, stdout, stderr = 0, "k-from-op\n", ""

        monkeypatch.setattr(bridge.subprocess, "run", lambda argv, **kw: calls.append(argv) or Proc())
        auth = []

        def handler(request: httpx.Request) -> httpx.Response:
            auth.append(request.headers["authorization"])
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        llm = _llm(handler, persona_api_key_ref="op://DeLoSecrets/abc/credential")
        llm([{"role": "user", "content": "x"}])
        llm([{"role": "user", "content": "x"}])
        assert calls == [["op", "read", "op://DeLoSecrets/abc/credential"]]  # resolved once, not stored
        assert auth == ["Bearer k-from-op"] * 2

    def test_raw_key_ref_refused(self) -> None:
        llm = _llm(lambda r: httpx.Response(200), persona_api_key_ref="sk-rawkey")
        with pytest.raises(bridge.PersonaError, match="op://"):
            llm([{"role": "user", "content": "x"}])

    @pytest.mark.parametrize("response", [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"nope": 1}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
    ])
    def test_bad_responses_raise_persona_error(self, monkeypatch: pytest.MonkeyPatch, response) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_API_KEY", "k")
        llm = _llm(lambda r: response)
        with pytest.raises(bridge.PersonaError):
            llm([{"role": "user", "content": "x"}])
        assert llm.last_error

    def test_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_API_KEY", "k")

        def handler(request):
            raise httpx.ConnectError("down")

        with pytest.raises(bridge.PersonaError, match="unreachable"):
            _llm(handler)([{"role": "user", "content": "x"}])


class TestRewrite:
    TEXT = "The deploy finished in 42 seconds."

    def test_accepted(self, plugin) -> None:
        r = bridge.rewrite(plugin, bridge.persona_by_id(plugin, "peter-griffin"), self.TEXT, StubLLM())
        assert r.accepted and "42" in r.rewrite and r.reason is None
        assert r.persona == "peter-griffin" and r.model == "stub-model"
        assert r.prompt_version == plugin.PROMPT_VERSION
        assert set(r.as_dict()) == {"original", "rewrite", "accepted", "reason", "persona",
                                    "model", "prompt_version", "elapsed_s"}
        # temperature comes from persona frontmatter; timeout from the llm config
        assert StubLLM.calls[0]["timeout"] == 5.0
        assert StubLLM.calls[0]["temperature"] == 0.8

    def test_validation_rejects_dropped_numbers(self, plugin) -> None:
        StubLLM.reply = "Holy crap the deploy finished real fast, freakin' sweet!"
        r = bridge.rewrite(plugin, bridge.persona_by_id(plugin, "peter-griffin"), self.TEXT, StubLLM())
        assert not r.accepted and r.rewrite is None and "dropped numbers" in r.reason

    def test_llm_failure_reported(self, plugin) -> None:
        StubLLM.reply = bridge.PersonaError("persona LLM returned 500: boom")
        r = bridge.rewrite(plugin, bridge.persona_by_id(plugin, "peter-griffin"), self.TEXT, StubLLM())
        assert not r.accepted and "500" in r.reason


# ---------------------------------------------------------------------------
# voice commands
# ---------------------------------------------------------------------------


class TestVoiceCommands:
    def test_show(self) -> None:
        res = runner.invoke(app, ["voice", "persona", "peter", "--json"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.stdout)
        assert data["persona"] is None
        assert data["effective"]["persona"] == "peter-griffin"
        assert data["effective"]["source"] == "frontmatter"

    def test_set_and_clear(self) -> None:
        res = runner.invoke(app, ["voice", "persona", "plain", "steve-brule", "--json"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.stdout)["effective"]["source"] == "voxxy"
        res = runner.invoke(app, ["voice", "persona", "plain", "--clear", "--json"])
        assert res.exit_code == 0, res.output
        assert FakeVox.puts == [("plain", "steve-brule"), ("plain", None)]
        assert json.loads(res.stdout)["persona"] is None

    def test_none_is_explicit(self) -> None:
        res = runner.invoke(app, ["voice", "persona", "peter", "NONE", "--json"])
        assert res.exit_code == 0, res.output
        assert FakeVox.puts == [("peter", "none")]
        assert json.loads(res.stdout)["effective"]["explicit_none"] is True

    def test_unknown_persona_warns_but_sets(self) -> None:
        res = runner.invoke(app, ["voice", "persona", "plain", "ghost"])
        assert res.exit_code == 0, res.output
        assert "no bundled persona 'ghost'" in res.output
        assert FakeVox.puts == [("plain", "ghost")]

    def test_bad_slug_rejected_before_request(self) -> None:
        res = runner.invoke(app, ["voice", "persona", "plain", "bad slug"])
        assert res.exit_code == 5
        assert FakeVox.puts == []

    def test_clear_and_persona_conflict(self) -> None:
        assert runner.invoke(app, ["voice", "persona", "plain", "x", "--clear"]).exit_code == 2

    def test_unknown_voice(self) -> None:
        assert runner.invoke(app, ["voice", "persona", "ghost"]).exit_code == 4
        assert runner.invoke(app, ["voice", "persona", "ghost", "peter-griffin"]).exit_code == 4

    def test_set_without_project_root_still_works(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("VOXXY_HOME")
        monkeypatch.chdir(tmp_path)
        res = runner.invoke(app, ["voice", "persona", "plain", "peter-griffin"])
        assert res.exit_code == 0, res.output
        assert "cannot check" in res.output
        assert FakeVox.puts == [("plain", "peter-griffin")]

    def test_list_has_persona_column(self) -> None:
        res = runner.invoke(app, ["voice", "list"])
        assert res.exit_code == 0, res.output
        assert "persona" in res.output
        assert "george-carlin" in res.output
        assert "peter-griffin" in res.output  # dim frontmatter default for a voice with no record

    def test_list_json_includes_persona(self) -> None:
        data = json.loads(runner.invoke(app, ["voice", "list", "--json"]).stdout)
        assert {v["name"]: v["persona"] for v in data}["rick"] == "george-carlin"

    def test_info_shows_effective(self) -> None:
        res = runner.invoke(app, ["voice", "info", "peter"])
        assert res.exit_code == 0, res.output
        assert "peter-griffin (frontmatter)" in res.output


# ---------------------------------------------------------------------------
# persona commands
# ---------------------------------------------------------------------------


class TestPersonaCommands:
    def test_list_json_maps_voices(self) -> None:
        res = runner.invoke(app, ["persona", "list", "--json"])
        assert res.exit_code == 0, res.output
        rows = {r["id"]: r for r in json.loads(res.stdout)}
        assert {"peter-griffin", "george-carlin", "steve-brule"} <= set(rows)
        assert {"voice": "rick", "source": "voxxy"} in rows["george-carlin"]["voices"]
        assert {"voice": "peter", "source": "frontmatter"} in rows["peter-griffin"]["voices"]
        brule = [v for v in rows["steve-brule"]["voices"] if v["source"] == "frontmatter"][0]
        assert brule["overridden_by"] == "none"
        assert rows["peter-griffin"]["temperature"] == 0.8

    def test_list_table(self) -> None:
        res = runner.invoke(app, ["persona", "list"])
        assert res.exit_code == 0, res.output
        assert "peter-griffin" in res.output

    def test_show_and_raw(self) -> None:
        res = runner.invoke(app, ["persona", "show", "peter-griffin"])
        assert res.exit_code == 0 and "Peter Griffin" in res.output
        raw = runner.invoke(app, ["persona", "show", "peter-griffin", "--raw"])
        assert raw.stdout.startswith("---")
        assert runner.invoke(app, ["persona", "show", "ghost"]).exit_code == 4

    def test_path(self) -> None:
        res = runner.invoke(app, ["persona", "path"])
        assert res.stdout.strip() == str(REPO_ROOT / "plugins" / "voice-persona" / "personas")

    def test_rewrite_by_id(self) -> None:
        res = runner.invoke(app, ["persona", "rewrite", "peter-griffin", "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert res.stdout.strip() == StubLLM.reply

    def test_rewrite_stdin(self) -> None:
        res = runner.invoke(app, ["persona", "rewrite", "peter-griffin"], input="The deploy finished in 42 seconds.\n")
        assert res.exit_code == 0, res.output

    def test_rewrite_json(self) -> None:
        res = runner.invoke(app, ["persona", "rewrite", "peter-griffin", "The deploy finished in 42 seconds.", "--json"])
        data = json.loads(res.stdout)
        assert data["accepted"] is True and data["persona"] == "peter-griffin"
        assert data["original"] == "The deploy finished in 42 seconds."

    def test_rewrite_by_voice(self) -> None:
        res = runner.invoke(app, ["persona", "rewrite", "--voice", "peter", "The deploy finished in 42 seconds.", "--json"])
        assert res.exit_code == 0, res.output
        assert json.loads(res.stdout)["persona"] == "peter-griffin"
        res = runner.invoke(app, ["persona", "rewrite", "--voice", "rick", "The deploy finished in 42 seconds.", "--json"])
        assert json.loads(res.stdout)["persona"] == "george-carlin"

    def test_rewrite_voice_without_persona(self) -> None:
        assert runner.invoke(app, ["persona", "rewrite", "--voice", "brule", "hi there"]).exit_code == 4
        assert runner.invoke(app, ["persona", "rewrite", "--voice", "plain", "hi there"]).exit_code == 4

    def test_rewrite_rejected_exits_nonzero_with_reason(self) -> None:
        StubLLM.reply = "Holy crap, that was fast."
        res = runner.invoke(app, ["persona", "rewrite", "peter-griffin", "The deploy finished in 42 seconds."])
        assert res.exit_code == 1
        assert "dropped numbers" in res.output
        assert res.stdout.strip() == ""
        res = runner.invoke(app, ["persona", "rewrite", "peter-griffin", "The deploy finished in 42 seconds.", "--json"])
        assert res.exit_code == 1 and json.loads(res.stdout)["accepted"] is False

    def test_rewrite_argument_errors(self) -> None:
        assert runner.invoke(app, ["persona", "rewrite", "ghost", "text here"]).exit_code == 4
        assert runner.invoke(app, ["persona", "rewrite"], input="").exit_code == 2

    def test_no_project_root_is_a_clear_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("VOXXY_HOME")
        monkeypatch.chdir(tmp_path)
        res = runner.invoke(app, ["persona", "list"])
        assert res.exit_code == 4
        assert "voxxy project root" in res.output


# ---------------------------------------------------------------------------
# speak --persona
# ---------------------------------------------------------------------------


@pytest.fixture()
def spoken(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    texts: list[str] = []
    monkeypatch.setattr(speak_mod, "_fetch_wav", lambda client, text, voice, c, s: texts.append(text) or b"RIFFwav")
    return texts


class TestSpeakPersona:
    def test_off_by_default(self, spoken) -> None:
        res = runner.invoke(app, ["speak", "--raw", "-v", "peter", "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert spoken == ["The deploy finished in 42 seconds."]
        assert StubLLM.calls == []

    def test_explicit_persona(self, monkeypatch: pytest.MonkeyPatch, spoken) -> None:
        res = runner.invoke(app, ["speak", "--raw", "-v", "rick", "--persona", "peter-griffin",
                                  "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert spoken == [StubLLM.reply]

    def test_auto_uses_voice_persona(self, spoken) -> None:
        res = runner.invoke(app, ["speak", "--raw", "-v", "peter", "--persona", "auto", "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert spoken == [StubLLM.reply]
        assert "PETER GRIFFIN" in StubLLM.calls[0]["messages"][0]["content"].upper()

    def test_env_var(self, monkeypatch: pytest.MonkeyPatch, spoken) -> None:
        monkeypatch.setenv("VOX_PERSONA", "auto")
        runner.invoke(app, ["speak", "--raw", "-v", "peter", "The deploy finished in 42 seconds."])
        assert spoken == [StubLLM.reply]

    def test_env_off_value(self, monkeypatch: pytest.MonkeyPatch, spoken) -> None:
        monkeypatch.setenv("VOX_PERSONA", "off")
        runner.invoke(app, ["speak", "--raw", "-v", "peter", "hello there"])
        assert spoken == ["hello there"] and StubLLM.calls == []

    @pytest.mark.parametrize("voice", ["brule", "plain"])
    def test_auto_without_persona_speaks_original(self, voice, spoken) -> None:
        res = runner.invoke(app, ["speak", "--raw", "-v", voice, "--persona", "auto", "hello there"])
        assert res.exit_code == 0, res.output
        assert spoken == ["hello there"]
        assert "speaking original text" in res.output

    def test_rejected_rewrite_is_fail_open(self, spoken) -> None:
        StubLLM.reply = "Holy crap, that was fast."
        res = runner.invoke(app, ["speak", "--raw", "-v", "peter", "--persona", "auto", "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert spoken == ["The deploy finished in 42 seconds."]
        assert "dropped numbers" in res.output

    def test_llm_error_is_fail_open(self, spoken) -> None:
        StubLLM.reply = bridge.PersonaError("persona LLM unreachable")
        res = runner.invoke(app, ["speak", "--raw", "-v", "peter", "--persona", "auto", "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert spoken == ["The deploy finished in 42 seconds."]

    def test_unknown_persona_is_fail_open(self, spoken) -> None:
        res = runner.invoke(app, ["speak", "--raw", "-v", "peter", "--persona", "ghost", "hello there"])
        assert res.exit_code == 0, res.output
        assert spoken == ["hello there"]
        assert "no bundled persona 'ghost'" in res.output

    def test_missing_project_root_is_fail_open(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spoken) -> None:
        monkeypatch.delenv("VOXXY_HOME")
        monkeypatch.chdir(tmp_path)
        res = runner.invoke(app, ["speak", "--raw", "-v", "peter", "--persona", "auto", "hello there"])
        assert res.exit_code == 0, res.output
        assert spoken == ["hello there"]

    def test_via_receives_rewritten_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = {}
        monkeypatch.setattr(speak_mod, "_via_is_local", lambda host: False)
        monkeypatch.setattr(speak_mod, "_speak_via_ssh",
                            lambda host, text, voice, url, explicit, mode, player: seen.update(text=text, voice=voice))
        res = runner.invoke(app, ["speak", "--raw", "--via", "remote", "-v", "peter", "--persona", "auto",
                                  "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert seen == {"text": StubLLM.reply, "voice": "peter"}

    def test_out_mode_receives_rewritten_text(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen = {}
        monkeypatch.setattr(speak_mod, "_speak_to_file", lambda client, text, voice, c, s, out: seen.update(text=text))
        res = runner.invoke(app, ["speak", "--out", str(tmp_path / "o.ogg"), "-v", "peter", "--persona", "auto",
                                  "The deploy finished in 42 seconds."])
        assert res.exit_code == 0, res.output
        assert seen["text"] == StubLLM.reply
