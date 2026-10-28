"""clone pipeline pieces: JSON extraction, YouTube filtering/ranking, config precedence, persona authoring."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import voxxy.clone as cl
import voxxy.persona as bridge
from voxxy.config import Config, load_config, save_config
from voxxy.llm import ChatLLM, CloneLLM, LLMError, extract_json

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_FILE = REPO_ROOT / bridge.PLUGIN_RELPATH
needs_plugin = pytest.mark.skipif(not PLUGIN_FILE.is_file(), reason="voice-persona plugin not in this checkout")

CLONE_ENV = ("VOXXY_CLONE_BASE_URL", "VOXXY_CLONE_MODEL", "VOXXY_CLONE_API_KEY", "VOXXY_CLONE_TIMEOUT",
             "VOXXY_PERSONA_BASE_URL", "VOXXY_PERSONA_MODEL", "VOXXY_PERSONA_API_KEY", "VOXXY_PERSONA_TIMEOUT")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in CLONE_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setenv("VOXXY_CLONE_DIR", str(tmp_path / "clone"))


class StubLLM:
    """Records calls; replies come from a queue (str, or Exception to raise)."""

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.last_error = None

    def __call__(self, messages, *, timeout=None, temperature=0.7):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            self.last_error = str(reply)
            raise reply
        return reply

    def json(self, messages, expect=None, **kw):
        return extract_json(self(messages, **kw), expect=expect)


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_schema_comments_echoed_by_the_model(self) -> None:
        reply = ('{"display_name": "Brian Griffin", // the show\n "youtube_queries": ["a", "b"], // show\n'
                 '"url": "http://x.test/a//b", "confidence": 0.9}')
        got = extract_json(reply)
        assert got["display_name"] == "Brian Griffin" and got["youtube_queries"] == ["a", "b"]
        assert got["url"] == "http://x.test/a//b"  # // inside a string survives

    def test_expect_dict_skips_a_nested_list(self) -> None:
        broken = '{"a": [1, 2], oops}'
        assert extract_json(broken) == [1, 2]
        with pytest.raises(LLMError):
            extract_json(broken, expect=dict)

    def test_plain(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_code_fence(self) -> None:
        assert extract_json('```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}

    def test_untagged_fence_and_trailing_prose(self) -> None:
        assert extract_json('Here you go:\n```\n{"slug": "brian-griffin"}\n```\nHope that helps!') == {"slug": "brian-griffin"}

    def test_leading_prose_no_fence(self) -> None:
        assert extract_json('Sure! The answer is {"ok": true, "n": {"x": 2}} as requested.') == {"ok": True, "n": {"x": 2}}

    def test_think_block_with_braces_ignored(self) -> None:
        assert extract_json('<think>maybe {"bad": 1}</think>\n{"good": 2}') == {"good": 2}

    def test_array(self) -> None:
        assert extract_json("ranked: [\"a\", \"b\"]") == ["a", "b"]

    def test_skips_unparseable_brace_then_finds_object(self) -> None:
        assert extract_json('note {not json} then {"k": "v"}') == {"k": "v"}

    @pytest.mark.parametrize("text", ["", "   ", "no json here", "{broken: json"])
    def test_raises_llm_error(self, text) -> None:
        with pytest.raises(LLMError):
            extract_json(text)


# ---------------------------------------------------------------------------
# config precedence
# ---------------------------------------------------------------------------


class TestCloneConfig:
    def test_defaults_inherit_persona_settings(self) -> None:
        llm = CloneLLM(Config())
        assert llm.base_url == "https://api.automaticai.io/v1"
        assert llm.model == "automaticai/personal/glm-5.3-flash"
        assert llm.key_ref is None

    def test_clone_config_overrides_persona_config(self) -> None:
        cfg = Config(persona_model="p-model", persona_base_url="http://p/v1", persona_api_key_ref="op://V/p/credential",
                     clone_model="c-model", clone_base_url="http://c/v1/", clone_api_key_ref="op://V/c/credential")
        llm = CloneLLM(cfg)
        assert (llm.base_url, llm.model, llm.key_ref) == ("http://c/v1", "c-model", "op://V/c/credential")

    def test_partial_clone_config_inherits_the_rest(self) -> None:
        llm = CloneLLM(Config(persona_base_url="http://p/v1", persona_model="p-model", clone_model="c-model"))
        assert (llm.base_url, llm.model) == ("http://p/v1", "c-model")

    def test_persona_env_beats_persona_config_for_clone(self, monkeypatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_MODEL", "env-persona-model")
        assert CloneLLM(Config(persona_model="cfg-persona")).model == "env-persona-model"

    def test_clone_config_beats_persona_env(self, monkeypatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_MODEL", "env-persona-model")
        assert CloneLLM(Config(clone_model="cfg-clone")).model == "cfg-clone"

    def test_clone_env_wins_over_everything(self, monkeypatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_MODEL", "env-persona-model")
        monkeypatch.setenv("VOXXY_CLONE_MODEL", "env-clone-model")
        monkeypatch.setenv("VOXXY_CLONE_BASE_URL", "http://env-clone/v1/")
        llm = CloneLLM(Config(clone_model="cfg-clone", clone_base_url="http://cfg/v1"))
        assert (llm.model, llm.base_url) == ("env-clone-model", "http://env-clone/v1")

    def test_key_precedence_and_never_stored(self, monkeypatch) -> None:
        monkeypatch.setenv("VOXXY_PERSONA_API_KEY", "persona-key")
        assert CloneLLM(Config()).api_key() == "persona-key"
        monkeypatch.setenv("VOXXY_CLONE_API_KEY", "clone-key")
        assert CloneLLM(Config()).api_key() == "clone-key"

    def test_clone_ref_beats_exported_persona_key(self, monkeypatch) -> None:
        """A dedicated clone endpoint must not receive the persona key that happens to be exported."""

        class Proc:
            returncode, stdout, stderr = 0, "clone-from-op\n", ""

        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: Proc())
        monkeypatch.setenv("VOXXY_PERSONA_API_KEY", "persona-key")
        assert CloneLLM(Config(clone_api_key_ref="op://V/c/credential")).api_key() == "clone-from-op"
        monkeypatch.setenv("VOXXY_CLONE_API_KEY", "clone-env")
        assert CloneLLM(Config(clone_api_key_ref="op://V/c/credential")).api_key() == "clone-env"

    def test_persona_env_beats_persona_ref(self, monkeypatch) -> None:
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("persona env must win over the persona ref"))
        monkeypatch.setenv("VOXXY_PERSONA_API_KEY", "persona-key")
        assert CloneLLM(Config(persona_api_key_ref="op://V/p/credential")).api_key() == "persona-key"

    def test_missing_key_error_names_the_clone_settings(self) -> None:
        with pytest.raises(LLMError, match="VOXXY_CLONE_API_KEY.*clone_api_key_ref"):
            CloneLLM(Config()).api_key()

    def test_op_ref_resolved_lazily_and_raw_refused(self, monkeypatch) -> None:
        calls = []

        class Proc:
            returncode, stdout, stderr = 0, "from-op\n", ""

        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or Proc())
        llm = CloneLLM(Config(clone_api_key_ref="op://V/c/credential"))
        assert llm.api_key() == "from-op" and llm.api_key() == "from-op"
        assert calls == [["op", "read", "op://V/c/credential"]]
        with pytest.raises(LLMError, match="op://"):
            CloneLLM(Config(clone_api_key_ref="sk-raw")).api_key()

    def test_toml_round_trip_and_defaults_not_written(self, tmp_path: Path) -> None:
        save_config(Config())
        assert "clone_" not in (tmp_path / "config.toml").read_text()
        save_config(Config(clone_model="m", clone_base_url="http://x/v1", clone_api_key_ref="op://V/i/credential"))
        cfg = load_config()
        assert (cfg.clone_model, cfg.clone_base_url, cfg.clone_api_key_ref) == ("m", "http://x/v1", "op://V/i/credential")

    def test_old_config_has_no_clone_keys(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text('persona_model = "pm"\n')
        cfg = load_config()
        assert cfg.clone_model is None and CloneLLM(cfg).model == "pm"

    def test_timeout_env(self, monkeypatch) -> None:
        assert CloneLLM(Config()).timeout == 120.0
        monkeypatch.setenv("VOXXY_CLONE_TIMEOUT", "7")
        assert CloneLLM(Config()).timeout == 7.0

    def test_http_mechanics_shared_with_persona(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)["model"]))
            return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"x": 1}\n```'}}]})

        llm = CloneLLM(Config(clone_base_url="http://c/v1", clone_model="cm"), transport=httpx.MockTransport(handler))
        llm._api_key = "k"
        assert llm.json([{"role": "user", "content": "hi"}]) == {"x": 1}
        assert seen == [("http://c/v1/chat/completions", "Bearer k", "cm")]
        assert issubclass(bridge.PersonaLLM, ChatLLM) and issubclass(CloneLLM, ChatLLM)

    def test_http_errors_are_llm_errors(self) -> None:
        llm = CloneLLM(Config(), transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
        llm._api_key = "k"
        with pytest.raises(LLMError, match="clone LLM returned 500"):
            llm([{"role": "user", "content": "x"}])
        assert "500" in (llm.last_error or "")


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

BRIAN = {
    "display_name": "Brian Griffin", "franchise": "Family Guy", "voice_actor": "Seth MacFarlane",
    "slug": "Brian Griffin!!", "youtube_queries": ["brian griffin best moments", "brian griffin best moments", "family guy brian clips"],
    "persona_brief": "A sardonic talking dog.", "confidence": 0.97,
}


class TestIdentity:
    @pytest.mark.parametrize("slug,voice", [
        ("the-dog-from-family-guy", "dog"), ("the-rock", "rock"), ("dr-phil", "phil"), ("mr-t", "t"),
        ("lord-voldemort", "voldemort"), ("brian-griffin", "brian"), ("the", "the"),
    ])
    def test_voice_name_skips_articles_and_titles(self, slug: str, voice: str) -> None:
        assert cl.Identity(display_name="x", slug=slug).voice_name == voice

    def test_prompt_schema_has_no_comments_to_echo(self) -> None:
        assert "//" not in cl.IDENTITY_SYSTEM

    def test_parse_normalises_slug_and_queries(self) -> None:
        ident = cl.parse_identity(BRIAN, "the dog from Family Guy")
        assert ident.slug == "brian-griffin" and ident.voice_name == "brian"
        assert ident.label == "Brian Griffin, from Family Guy"
        assert ident.youtube_queries == ["brian griffin best moments", "family guy brian clips"]
        assert ident.confidence == pytest.approx(0.97)

    def test_hostile_slug_cannot_escape_the_workdir(self) -> None:
        ident = cl.parse_identity({**BRIAN, "slug": "../../etc/passwd"}, "q")
        assert cl.SLUG_RE.match(ident.slug) and "/" not in ident.slug and ".." not in ident.slug
        assert cl.workdir_for(ident.slug).parent == cl.clone_root()

    def test_real_person_has_no_franchise(self) -> None:
        ident = cl.parse_identity({"display_name": "Arnold Schwarzenegger", "franchise": None, "slug": "arnold-schwarzenegger",
                                   "youtube_queries": ["arnold speech"], "confidence": "0.9"}, "q")
        assert ident.label == "Arnold Schwarzenegger" and ident.franchise is None

    def test_bad_confidence_defaults_to_zero(self) -> None:
        assert cl.parse_identity({**BRIAN, "confidence": "high"}, "q").confidence == 0.0

    @pytest.mark.parametrize("data", [None, [], {"slug": "x"}, {"display_name": "  "}])
    def test_unusable_reply_raises(self, data) -> None:
        with pytest.raises(cl.CloneError):
            cl.parse_identity(data, "q")

    def test_resolve_with_llm_includes_web_results_as_untrusted(self) -> None:
        llm = StubLLM([json.dumps(BRIAN)])
        ident = cl.resolve_identity("the dog from Family Guy", llm, [{"title": "Brian Griffin", "snippet": "dog", "url": "http://w"}])
        assert ident.resolved_by == "llm" and ident.slug == "brian-griffin"
        user = llm.calls[0][1]["content"]
        assert 'untrusted="true"' in user and "Brian Griffin" in user and "the dog from Family Guy" in user

    def test_resolve_falls_back_when_llm_fails(self) -> None:
        for reply in (LLMError("no key"), "I cannot help with that", "{}"):
            ident = cl.resolve_identity("Morgan Freeman", StubLLM([reply]), [])
            assert ident.resolved_by == "fallback" and ident.slug == "morgan-freeman" and ident.confidence == 0.0
            assert len(ident.youtube_queries) >= 3

    def test_web_search_absent_is_graceful(self, monkeypatch) -> None:
        monkeypatch.setattr(cl.shutil, "which", lambda name: None)
        assert cl.web_search("anything") == []

    def test_web_search_parses_results(self) -> None:
        out = json.dumps({"results": [{"title": "T", "url": "http://u", "snippet": "S", "position": 1}]})
        runner = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=out)  # noqa: E731
        res = cl.web_search("q", 5, runner=runner)
        assert res == [{"title": "T", "url": "http://u", "snippet": "S"}]

    def test_web_search_argv_and_failure(self) -> None:
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            return SimpleNamespace(returncode=1, stdout="")

        assert cl.web_search("q", 5, runner=runner) == []
        assert seen[0] == ["web-search", "search", "-n", "5", "--json", "q"]

    def test_slugify(self) -> None:
        assert cl.slugify("Brian Griffin, from Family Guy") == "brian-griffin-from-family-guy"
        assert cl.slugify("Zoë Kravitz") == "zoe-kravitz"
        assert cl.slugify("***", "voice") == "voice"


# ---------------------------------------------------------------------------
# YouTube search / filter / rank
# ---------------------------------------------------------------------------

FLAT = {"entries": [
    {"id": "aaa", "title": "Brian Griffin best moments", "url": "https://www.youtube.com/watch?v=aaa", "duration": 600, "channel": "FG", "view_count": 10},
    {"id": "bbb", "title": "Brian Griffin REACTION", "url": "https://www.youtube.com/watch?v=bbb", "duration": 300, "uploader": "RX"},
    {"id": "ccc", "title": "Short", "url": "https://www.youtube.com/watch?v=ccc", "duration": 20},
    {"id": "ddd", "title": "Ten hour loop", "url": "https://www.youtube.com/watch?v=ddd", "duration": 36000},
    {"id": "aaa", "title": "Brian Griffin best moments (dup)", "url": "https://www.youtube.com/watch?v=aaa", "duration": 600},
    {"id": "eee", "title": "No duration (live?)", "url": "https://www.youtube.com/watch?v=eee"},
    {"id": "fff", "title": "Family Guy scenes: Brian talks", "duration": 900},
    {"title": "no id"},
]}


class TestYoutube:
    def test_parse_search_json(self) -> None:
        vids = cl.parse_search_json(FLAT, "q")
        assert [v.id for v in vids] == ["aaa", "bbb", "ccc", "ddd", "aaa", "eee", "fff"]
        assert vids[1].channel == "RX" and vids[0].views == 10
        assert vids[-1].url == "https://www.youtube.com/watch?v=fff"  # synthesized when absent
        assert all(v.query == "q" for v in vids)

    def test_filter_bounds_and_dedupe(self) -> None:
        vids = cl.filter_videos(cl.parse_search_json(FLAT))
        assert [v.id for v in vids] == ["aaa", "bbb", "fff"]  # 20 s, 10 h, dup and 0 s dropped
        assert cl.MIN_VIDEO_SECONDS == 45 and cl.MAX_VIDEO_SECONDS == 40 * 60

    def test_filter_custom_bounds(self) -> None:
        vids = cl.filter_videos(cl.parse_search_json(FLAT), min_seconds=500, max_seconds=700)
        assert [v.id for v in vids] == ["aaa"]

    def test_heuristic_prefers_clean_and_demotes_reactions(self) -> None:
        vids = cl.filter_videos(cl.parse_search_json(FLAT))
        ranked = cl.heuristic_rank(vids)
        assert ranked[-1].id == "bbb" and ranked[0].id in ("aaa", "fff")

    @pytest.mark.parametrize("title", ["Peter reacts to Brian", "Brian Griffin AI cover of a song", "Lois dubbed in German", "Stewie parody #shorts"])
    def test_bad_titles_score_negative(self, title) -> None:
        assert cl.heuristic_score(cl.Video("x", title, "u", 300)) < 0

    def test_llm_ranking_respected_and_unknown_ids_ignored(self) -> None:
        vids = cl.filter_videos(cl.parse_search_json(FLAT))
        llm = StubLLM([json.dumps({"ranked": ["fff", "ghost", "aaa"], "rejected": ["bbb"]})])
        ident = cl.parse_identity(BRIAN, "q")
        assert [v.id for v in cl.rank_videos(ident, vids, llm)] == ["fff", "aaa"]
        assert "untrusted" in llm.calls[0][1]["content"]

    def test_llm_failure_falls_back_to_titles(self) -> None:
        vids = cl.filter_videos(cl.parse_search_json(FLAT))
        ident = cl.parse_identity(BRIAN, "q")
        ranked = cl.rank_videos(ident, vids, StubLLM([LLMError("down")]))
        assert {v.id for v in ranked} == {"aaa", "bbb", "fff"} and ranked[-1].id == "bbb"

    def test_llm_rejecting_everything_still_returns_videos(self) -> None:
        vids = cl.filter_videos(cl.parse_search_json(FLAT))
        ident = cl.parse_identity(BRIAN, "q")
        ranked = cl.rank_videos(ident, vids, StubLLM([json.dumps({"ranked": [], "rejected": ["bbb"]})]))
        assert ranked and "bbb" not in [v.id for v in ranked]

    def test_find_videos_merges_queries_in_order(self) -> None:
        ident = cl.parse_identity(BRIAN, "q")
        pages = {"brian griffin best moments": FLAT, "family guy brian clips": {"entries": [
            {"id": "zzz", "title": "Brian full scene", "duration": 400}, {"id": "aaa", "title": "dup", "duration": 600}]}}
        seen = []

        def searcher(q):
            seen.append(q)
            return cl.parse_search_json(pages[q], q)

        out = cl.find_videos(ident, StubLLM([LLMError("x")]), searcher=searcher)
        assert seen == ident.youtube_queries
        ids = [v.id for v in out]
        assert ids.count("aaa") == 1 and "zzz" in ids and "ccc" not in ids

    def test_search_argv(self) -> None:
        argv = cl.build_search_argv("brian clips", 8)
        assert argv[-1] == "ytsearch8:brian clips" and "--flat-playlist" in argv and "-J" in argv

    def test_search_runner_failure_returns_empty(self) -> None:
        assert cl.search_youtube("q", runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="")) == []
        assert cl.search_youtube("q", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="not json")) == []

    def test_download_argv_sections_and_wav(self, tmp_path: Path) -> None:
        argv = cl.build_download_argv("https://youtu.be/x", tmp_path, 12)
        assert argv[argv.index("--download-sections") + 1] == "*0-720"
        assert "-x" in argv and argv[argv.index("--audio-format") + 1] == "wav"
        assert "--no-playlist" in argv and argv[-1] == "https://youtu.be/x"
        assert argv[argv.index("-o") + 1] == str(tmp_path / "source.%(ext)s")

    def test_ytdlp_falls_back_to_python_module(self, monkeypatch) -> None:
        monkeypatch.setattr(cl.shutil, "which", lambda name: None)
        cmd = cl.ytdlp_cmd()
        assert cmd[1:] == ["-m", "yt_dlp"]

    def test_download_writes_source_json(self, tmp_path: Path) -> None:
        video = cl.Video("abc", "A title", "https://youtu.be/abc", 600, "Chan", "q")
        ident = cl.parse_identity(BRIAN, "q")

        def runner(argv, **kw):
            (tmp_path / "source.wav").write_bytes(b"RIFF....")
            return SimpleNamespace(returncode=0, stderr="")

        wav = cl.download_audio(video, tmp_path, max_minutes=5, runner=runner, identity=ident)
        meta = json.loads((tmp_path / "source.json").read_text())
        assert wav.name == "source.wav"
        assert meta["url"] == "https://youtu.be/abc" and meta["title"] == "A title" and meta["identity"]["slug"] == "brian-griffin"

    def test_download_reused_for_same_video_and_not_for_others(self, tmp_path: Path) -> None:
        video = cl.Video("abc", "A title", "https://youtu.be/abc", 600)
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            (tmp_path / "source.wav").write_bytes(b"RIFF....")
            return SimpleNamespace(returncode=0, stderr="")

        cl.download_audio(video, tmp_path, max_minutes=5, runner=runner)
        cl.download_audio(video, tmp_path, max_minutes=5, runner=runner)   # same video, same head: reused
        cl.download_audio(video, tmp_path, max_minutes=3, runner=runner)   # shorter head asked: reused
        assert len(calls) == 1
        cl.download_audio(video, tmp_path, max_minutes=9, runner=runner)   # longer head: re-download
        cl.download_audio(cl.Video("zzz", "t", "https://youtu.be/zzz", 600), tmp_path, max_minutes=3, runner=runner)
        assert len(calls) == 3
        cl.download_audio(video, tmp_path, max_minutes=9, runner=runner, reuse=False)
        assert len(calls) == 4

    def test_download_failure_raises_clone_error(self, tmp_path: Path) -> None:
        video = cl.Video("abc", "t", "https://youtu.be/abc", 600)
        runner = lambda argv, **kw: SimpleNamespace(returncode=1, stderr="ERROR: Video unavailable\n")  # noqa: E731
        with pytest.raises(cl.CloneError, match="Video unavailable"):
            cl.download_audio(video, tmp_path, runner=runner)


# ---------------------------------------------------------------------------
# persona authoring
# ---------------------------------------------------------------------------


def good_persona(name: str = "Brian Griffin", voice: str = "brian") -> str:
    """A valid persona body adapted from a bundled one (real structure, real rules)."""
    text = (REPO_ROOT / "plugins/voice-persona/personas/peter-griffin.md").read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    return f"---\nname: {name}\nvoices: [{voice}]\ntemperature: 0.8\ndescription: A sardonic talking dog.\n---\n{body}"


@pytest.fixture()
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = bridge.load_plugin(REPO_ROOT)
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (pdir / "peter-griffin.md").write_text((REPO_ROOT / "plugins/voice-persona/personas/peter-griffin.md").read_text())
    monkeypatch.setattr(mod, "PERSONAS_DIR", pdir)
    return mod


@needs_plugin
class TestPersona:
    def test_good_persona_has_no_problems(self) -> None:
        assert cl.persona_problems(good_persona()) == []

    @pytest.mark.parametrize("mutate, expect", [
        (lambda t: t.replace("## Rules for rewriting", "## Notes"), "Rules for rewriting"),
        (lambda t: t.replace("## Examples", "## Samples"), "Examples"),
        (lambda t: t.replace("NEVER drop or alter", "avoid changing"), "NEVER drop or alter"),
        (lambda t: t.replace("Before:", "Prev:"), "Before:/After:"),
    ])
    def test_structure_problems_detected(self, mutate, expect) -> None:
        problems = cl.persona_problems(mutate(good_persona()))
        assert any(expect in p for p in problems), problems

    def test_normalise_forces_voice_and_strips_fences(self) -> None:
        raw = "Sure!\n```markdown\n" + good_persona(voice="wrong") + "\n```"
        out = cl.normalize_persona_markdown(raw, display_name="Brian Griffin", voice="brian")
        assert out.startswith("---\nname: Brian Griffin\nvoices: [brian]\n")
        assert "wrong" not in out.split("---")[1] and "```" not in out

    def test_normalise_clamps_temperature_and_defaults(self) -> None:
        out = cl.normalize_persona_markdown("---\nname: X\ntemperature: 5\n---\nbody text", display_name="X", voice=None)
        assert "temperature: 1" in out and "voices: []" in out and "description: X - in character." in out

    def test_generate_validates_and_parses_through_plugin(self, plugin) -> None:
        llm = StubLLM([good_persona()])
        md = cl.generate_persona(llm, plugin, character="Brian Griffin, from Family Guy", voice="brian",
                                 brief="sardonic dog", facts=[{"title": "Brian", "snippet": "dog", "url": "u"}],
                                 display_name="Brian Griffin")
        assert "voices: [brian]" in md
        sys_prompt, user = llm.calls[0][0]["content"], llm.calls[0][1]["content"]
        assert "Rules for rewriting" in sys_prompt and "NEVER drop or alter" in sys_prompt
        assert "<template>" in user and "Peter Griffin" in user  # the few-shot template is included
        assert 'untrusted="true"' in user

    def test_generate_retries_once_with_problems_then_succeeds(self, plugin) -> None:
        llm = StubLLM(["---\nname: B\n---\nthin", good_persona()])
        md = cl.generate_persona(llm, plugin, character="Brian", voice="brian")
        assert "## Examples" in md and len(llm.calls) == 2
        assert "failed validation" in llm.calls[1][-1]["content"]

    def test_generate_gives_up_after_two_bad_replies(self, plugin) -> None:
        with pytest.raises(cl.CloneError, match="failed validation"):
            cl.generate_persona(StubLLM(["junk", "junk"]), plugin, character="Brian", voice="brian")

    def test_generate_wraps_llm_errors(self, plugin) -> None:
        with pytest.raises(cl.CloneError, match="persona generation failed"):
            cl.generate_persona(StubLLM([LLMError("quota")]), plugin, character="Brian", voice="brian")

    def test_written_file_loads_in_plugin(self, plugin) -> None:
        path = cl.write_persona(plugin, "brian-griffin", good_persona())
        persona = plugin.persona_by_id("brian-griffin")
        assert path.name == "brian-griffin.md" and persona is not None and persona.voices == ("brian",)

    def test_write_refuses_overwrite_without_force(self, plugin) -> None:
        cl.write_persona(plugin, "brian-griffin", good_persona())
        with pytest.raises(cl.CloneError, match="already exists"):
            cl.write_persona(plugin, "brian-griffin", good_persona())
        cl.write_persona(plugin, "brian-griffin", good_persona(name="Brian v2"), force=True)
        assert "Brian v2" in (plugin.PERSONAS_DIR / "brian-griffin.md").read_text()

    @pytest.mark.parametrize("bad", ["../evil", "Has Space", "UPPER", "", "-lead"])
    def test_write_rejects_bad_ids(self, plugin, bad) -> None:
        with pytest.raises(cl.CloneError, match="invalid persona id"):
            cl.write_persona(plugin, bad, good_persona())


class TestYtdlpBotCheck:
    BOT = "ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies"

    @staticmethod
    def _proc(code, err=""):
        return SimpleNamespace(returncode=code, stderr=err, stdout="")

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for k in ("VOXXY_YTDLP_COOKIES", "VOXXY_YTDLP_COOKIES_FROM_BROWSER"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(cl, "_cookie_browser_in_use", None)

    def test_bot_check_retries_with_browser_cookies_and_remembers_the_winner(self) -> None:
        seen: list[list[str]] = []

        def runner(argv, **kw):
            seen.append(argv)
            if "--cookies-from-browser" not in argv:
                return self._proc(1, self.BOT)
            return self._proc(0) if "firefox" in argv else self._proc(1, "ERROR: could not find chromium cookies database")

        msgs: list[str] = []
        proc = cl.run_ytdlp(lambda extra: ["yt-dlp", *extra, "url"], runner, notify=msgs.append)
        assert proc.returncode == 0
        assert [a[2] for a in seen if "--cookies-from-browser" in a] == ["chrome", "chromium", "firefox"]
        assert cl.cookie_args() == ["--cookies-from-browser", "firefox"]  # next call goes straight to the working jar
        assert any("retrying with chrome" in m for m in msgs)

    def test_explicit_choice_is_never_second_guessed(self, monkeypatch) -> None:
        monkeypatch.setenv("VOXXY_YTDLP_COOKIES_FROM_BROWSER", "none")
        calls: list[list[str]] = []

        def runner(argv, **kw):
            calls.append(argv)
            return self._proc(1, self.BOT)

        proc = cl.run_ytdlp(lambda extra: ["yt-dlp", *extra], runner)
        assert proc.returncode == 1 and len(calls) == 1 and cl.cookie_args() == []
        monkeypatch.setenv("VOXXY_YTDLP_COOKIES", "/tmp/cookies.txt")
        assert cl.cookie_args() == ["--cookies", "/tmp/cookies.txt"]

    def test_other_failures_are_not_retried(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **kw):
            calls.append(argv)
            return self._proc(1, "ERROR: Video unavailable")

        assert cl.run_ytdlp(lambda extra: ["yt-dlp", *extra], runner).returncode == 1
        assert len(calls) == 1

    def test_argv_builders_take_extra_flags(self, tmp_path) -> None:
        assert "--cookies-from-browser" in cl.build_download_argv("u", tmp_path, 12, ["--cookies-from-browser", "chrome"])
        assert "--cookies-from-browser" in cl.build_search_argv("q", 8, ["--cookies-from-browser", "chrome"])
