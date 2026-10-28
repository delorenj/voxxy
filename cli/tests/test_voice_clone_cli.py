"""Typer wiring for voice add (new flags), voice candidates, voice clone and persona generate.

Everything heavy is mocked: whisper/ffmpeg analysis, yt-dlp, the LLM, the vox server.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import voxxy.clone as cl
import voxxy.commands.persona as persona_cmd
import voxxy.commands.voice as voice_cmd
import voxxy.commands.voice_clone as vc
import voxxy.persona as bridge
from voxxy import refclip as rc
from voxxy.app import app
from voxxy.audio import AudioInfo
from voxxy.client import SynthUrlResponse, VoiceOut, VoxNotFound, VoxServerError
from voxxy.llm import LLMError

from tests.test_clone import BRIAN, StubLLM, good_persona

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_FILE = REPO_ROOT / bridge.PLUGIN_RELPATH

runner = CliRunner()


def marker(start: float, end: float, score: float = 0.9, text: str = "So I wanted to escape from that home.", ok: bool = True):
    return rc.Marker(
        start=start, end=end, text=text, score=score, ok=ok, reasons=[] if ok else ["noisy"],
        breakdown={"clarity": 1.0, "sentence": 0.9, "snr": 0.8, "background": 1.0, "density": 1.0, "filler": 1.0,
                   "duration": 0.5, "speaker": 1.0},
        metrics={"snr_db": 35.0, "other_speaker_frac": 0.0},
    )


ONE_SPEAKER = rc.SpeakerMap(segments=[], dominant=1, n_clusters=1)


def fake_analysis(path: str, markers: list, speakers=ONE_SPEAKER, *, gated: bool = True) -> rc.Analysis:
    """``gated`` pretends the ECAPA one-voice gate ran on every marker (its metrics are what _speaker_risk reads)."""
    if gated:
        for m in markers:
            m.metrics.setdefault("voice_gated", 1.0)
            m.metrics.setdefault("coherence", 0.52)
    return rc.Analysis(path=path, duration=60.0, words=[], pauses=[], speech_level_db=-20.0, pause_below_db=30.0,
                       speakers=speakers, markers=markers)


class FakeVox:
    voices: dict[str, VoiceOut] = {}
    created: list[dict] = []
    persona_puts: list[tuple] = []
    engine = "voxcpm"

    def __init__(self, *_a, **_k) -> None:
        pass

    def get_voice(self, name):
        if name not in self.voices:
            raise VoxNotFound(name)
        return self.voices[name]

    def create_voice(self, name, display_name, audio_path, *, tags=None, prompt_text=None):
        FakeVox.created.append({"name": name, "display_name": display_name, "audio": Path(audio_path), "tags": tags,
                                "prompt_text": prompt_text, "audio_exists": Path(audio_path).is_file()})
        v = VoiceOut(name=name, display_name=display_name, duration_s=12.0, tags=tags or [], prompt_text=prompt_text)
        FakeVox.voices[name] = v
        return v

    def set_voice_persona(self, name, persona):
        if name not in self.voices:
            raise VoxNotFound(name)
        FakeVox.persona_puts.append((name, persona))
        return self.voices[name].model_copy(update={"persona": persona})

    def synthesize_url(self, text, voice=None, **kw):
        return SynthUrlResponse(audio_url="https://vox.test/audio/abc.ogg", engine=FakeVox.engine, bytes=1234,
                                duration_s=3.0, vox_engine_header=FakeVox.engine)

    def sink_status(self, key):
        return SimpleNamespace(listeners=0)


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setenv("VOXXY_HOME", str(REPO_ROOT))
    monkeypatch.setenv("VOXXY_CLONE_DIR", str(tmp_path / "clone"))
    for var in ("VOX_SINK", "VOX_PLAYER", "VOX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    FakeVox.voices, FakeVox.created, FakeVox.persona_puts, FakeVox.engine = {}, [], [], "voxcpm"
    monkeypatch.setattr(voice_cmd, "VoxClient", FakeVox)
    monkeypatch.setattr(vc, "VoxClient", FakeVox)
    monkeypatch.setattr(persona_cmd, "VoxClient", FakeVox)
    monkeypatch.setattr(vc.importlib.util, "find_spec", lambda name: object())


# ---------------------------------------------------------------------------
# voice add
# ---------------------------------------------------------------------------


@pytest.fixture()
def add_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    src = tmp_path / "talk.wav"
    src.write_bytes(b"RIFFfake")
    calls = SimpleNamespace(preprocess=[], analyze=[], prepare=[])
    monkeypatch.setattr(voice_cmd, "probe", lambda p: AudioInfo(duration=60.0, channels=2, sample_rate=44100, codec="pcm"))

    def fake_preprocess(src, dst, **kw):
        calls.preprocess.append(kw)
        Path(dst).write_bytes(b"RIFFplain")

    monkeypatch.setattr(voice_cmd, "preprocess", fake_preprocess)

    def fake_analyze(path, **kw):
        calls.analyze.append((Path(path), kw))
        return fake_analysis(str(path), calls.markers)

    calls.markers = [marker(12.64, 24.94, 0.88), marker(2.25, 12.36, 0.7)]
    monkeypatch.setattr(vc.refclip, "analyze", fake_analyze)

    def fake_prepare(src_path, dst, start, end, *, fallback_text="", prompt_text=None):
        calls.prepare.append((start, end, fallback_text, prompt_text))
        Path(dst).write_bytes(b"RIFFclean")
        return vc.PreparedClip(clip=Path(dst), start=start, end=end, prompt_text=prompt_text or "transcribed words")

    monkeypatch.setattr(vc, "prepare_clip", fake_prepare)
    calls.src = src
    return calls


class TestVoiceAdd:
    def test_plain_add_unchanged(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "--name", "plain", "--no-prompt"])
        assert res.exit_code == 0, res.output
        assert len(add_env.preprocess) == 1 and not add_env.analyze and not add_env.prepare
        assert FakeVox.created[0]["prompt_text"] is None
        assert FakeVox.created[0]["audio"].name != "talk.wav"

    def test_prompt_text_passed_through(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "plain", "-N", "--prompt-text", "  the exact words  "])
        assert res.exit_code == 0, res.output
        assert len(add_env.preprocess) == 1 and not add_env.analyze
        assert FakeVox.created[0]["prompt_text"] == "the exact words"

    def test_auto_picks_best_window_and_sets_prompt_text(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "brian", "-N", "--auto"])
        assert res.exit_code == 0, res.output
        assert add_env.prepare == [(12.64, 24.94, "So I wanted to escape from that home.", None)]
        assert not add_env.preprocess  # the clean export replaces the plain trim
        assert add_env.analyze[0][1]["min_seconds"] == 10.0 and add_env.analyze[0][1]["max_seconds"] == 15.0
        created = FakeVox.created[0]
        assert created["prompt_text"] == "transcribed words" and created["audio_exists"]

    def test_auto_respects_min_max_and_explicit_prompt_text(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "b", "-N", "--auto", "--min-seconds", "5",
                                  "--max-seconds", "8", "--prompt-text", "mine"])
        assert res.exit_code == 0, res.output
        assert add_env.analyze[0][1]["min_seconds"] == 5.0 and add_env.analyze[0][1]["max_seconds"] == 8.0
        assert add_env.prepare[0][3] == "mine" and FakeVox.created[0]["prompt_text"] == "mine"

    def test_auto_without_clean_window_fails(self, add_env) -> None:
        add_env.markers = []
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "b", "-N", "--auto"])
        assert res.exit_code == 1 and not FakeVox.created
        assert "voice candidates" in res.output or "--start" in res.output

    def test_start_end_explicit_window_skips_analysis(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "b", "-N", "--start", "12.6", "--end", "26.1"])
        assert res.exit_code == 0, res.output
        assert not add_env.analyze and add_env.prepare == [(12.6, 26.1, "", None)]

    def test_start_only_defaults_end_to_max_seconds(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "b", "-N", "--start", "5", "--max-seconds", "12"])
        assert res.exit_code == 0, res.output
        assert add_env.prepare[0][:2] == (5.0, 17.0)

    def test_bad_window_rejected(self, add_env) -> None:
        res = runner.invoke(app, ["voice", "add", str(add_env.src), "-n", "b", "-N", "--start", "30", "--end", "20"])
        assert res.exit_code == 2 and not FakeVox.created

    def test_help_lists_new_flags(self) -> None:
        out = runner.invoke(app, ["voice", "add", "--help"]).output
        for flag in ("--prompt-text", "--auto", "--start", "--end", "--min-seconds", "--max-seconds"):
            assert flag in out


# ---------------------------------------------------------------------------
# voice candidates
# ---------------------------------------------------------------------------


@pytest.fixture()
def cand_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    src = tmp_path / "talk.mp3"
    src.write_bytes(b"x")
    state = SimpleNamespace(markers=[marker(12.64, 24.94, 0.88), marker(2.25, 12.36, 0.7), marker(30, 41, 0.6, text="third")], kw=None)

    def fake_analyze(path, **kw):
        state.kw = kw
        return fake_analysis(str(path), state.markers)

    monkeypatch.setattr(vc.refclip, "analyze", fake_analyze)
    state.src = src
    return state


class TestCandidates:
    def test_table_lists_ranked_markers(self, cand_env) -> None:
        res = runner.invoke(app, ["voice", "candidates", str(cand_env.src), "--top", "2"])
        assert res.exit_code == 0, res.output
        assert "12.64-24.94s" in res.output and "2.25-12.36s" in res.output and "30.00" not in res.output
        assert "wanted to escape" in res.output

    def test_json_output(self, cand_env) -> None:
        res = runner.invoke(app, ["voice", "candidates", str(cand_env.src), "--json", "--top", "2"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert [c["start"] for c in data["candidates"]] == [12.64, 2.25]
        assert data["candidates"][0]["breakdown"]["clarity"] == 1.0 and data["words"] == 0

    def test_markers_file_written(self, cand_env, tmp_path: Path) -> None:
        out = tmp_path / "m" / "markers.json"
        res = runner.invoke(app, ["voice", "candidates", str(cand_env.src), "--markers", str(out)])
        assert res.exit_code == 0, res.output
        assert json.loads(out.read_text())["candidates"][0]["end"] == 24.94

    def test_forwards_duration_options(self, cand_env) -> None:
        runner.invoke(app, ["voice", "candidates", str(cand_env.src), "--min-seconds", "6", "--max-seconds", "9"])
        assert cand_env.kw["min_seconds"] == 6.0 and cand_env.kw["max_seconds"] == 9.0

    def test_no_clean_window_exit_1_and_shows_reasons(self, cand_env) -> None:
        cand_env.markers = [marker(1, 12, 0.3, ok=False)]
        res = runner.invoke(app, ["voice", "candidates", str(cand_env.src)])
        assert res.exit_code == 1 and "No window passed" in res.output and "noisy" in res.output

    def test_nothing_at_all_exit_1(self, cand_env) -> None:
        cand_env.markers = []
        assert runner.invoke(app, ["voice", "candidates", str(cand_env.src)]).exit_code == 1

    def test_missing_file_exit_4(self, tmp_path: Path) -> None:
        assert runner.invoke(app, ["voice", "candidates", str(tmp_path / "nope.wav")]).exit_code == 4

    def test_missing_dependency_message(self, monkeypatch, cand_env) -> None:
        def boom(*a, **k):
            raise rc.CloneDepsMissing(str(rc._missing("faster-whisper")))

        monkeypatch.setattr(vc.refclip, "analyze", boom)
        res = runner.invoke(app, ["voice", "candidates", str(cand_env.src)])
        assert res.exit_code == 1
        assert 'uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force' in res.output.replace("\n", " ")

    def test_bad_range_exit_2(self, cand_env) -> None:
        assert runner.invoke(app, ["voice", "candidates", str(cand_env.src), "--min-seconds", "9", "--max-seconds", "5"]).exit_code == 2


# ---------------------------------------------------------------------------
# voice clone
# ---------------------------------------------------------------------------


@pytest.fixture()
def clone_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    st = SimpleNamespace(downloads=[], analyses=[], llm=StubLLM(), search_calls=[], persona_writes=[], prepared=[])
    st.videos = [cl.Video("vid1", "Brian Griffin best moments", "https://youtu.be/vid1", 600, "FG"),
                 cl.Video("vid2", "Family Guy Brian scenes", "https://youtu.be/vid2", 700, "FG2")]
    st.markers_by_video = {"vid1": [marker(12.64, 24.94, 0.88)], "vid2": [marker(3.0, 14.0, 0.7)]}

    monkeypatch.setattr(vc, "CloneLLM", lambda cfg=None: st.llm)
    st.llm.replies = [json.dumps(BRIAN)]
    monkeypatch.setattr(cl, "web_search", lambda q, n=5, **k: st.search_calls.append(q) or [{"title": "Brian", "snippet": "dog", "url": "http://w"}])
    monkeypatch.setattr(vc.cl, "find_videos", lambda ident, llm, **k: list(st.videos))

    def fake_download(video, outdir, *, max_minutes=12, identity=None, **kw):
        st.downloads.append((video.id, max_minutes))
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "source.wav").write_bytes(b"RIFFsrc" + video.id.encode())
        (outdir / "source.json").write_text(json.dumps({"url": video.url, "title": video.title}))
        return outdir / "source.wav"

    monkeypatch.setattr(vc.cl, "download_audio", fake_download)

    def fake_analyze(path, **kw):
        vid = Path(path).parent.name.split("-", 1)[1]
        st.analyses.append((vid, kw))
        return fake_analysis(str(path), st.markers_by_video[vid])

    monkeypatch.setattr(vc.refclip, "analyze", fake_analyze)

    def fake_prepare(src, dst, start, end, *, fallback_text="", prompt_text=None):
        st.prepared.append((Path(src).name, start, end))
        Path(dst).write_bytes(b"RIFFclip")
        return vc.PreparedClip(clip=Path(dst), start=start, end=end, prompt_text="So I wanted to escape from that home.")

    monkeypatch.setattr(vc, "prepare_clip", fake_prepare)
    monkeypatch.setattr(vc, "_play_url", lambda *a, **k: None)
    return st


class TestClone:
    def test_happy_path(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "the dog from Family Guy", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 0, res.output
        out = res.output.replace("\n", " ")
        assert "Brian Griffin, from Family Guy" in out
        created = FakeVox.created[0]
        assert created["name"] == "brian" and created["display_name"] == "Brian Griffin"
        assert created["prompt_text"] == "So I wanted to escape from that home." and created["audio_exists"]
        assert created["tags"] == ["clone", "family-guy"]
        assert clone_env.downloads == [("vid1", 12)]  # strong first video: no fall-through
        assert clone_env.prepared[0][1:] == (12.64, 24.94)
        # workdir: source + markers + source.json + identity, chosen wav promoted
        wd = cl.workdir_for("brian-griffin")
        assert (wd / "source.wav").is_file() and (wd / "markers.json").is_file() and (wd / "source.json").is_file()
        assert json.loads((wd / "identity.json").read_text())["slug"] == "brian-griffin"
        assert (wd / "brian-griffin.wav").is_file()
        assert "voxcpm" in out and "https://vox.test/audio/abc.ogg" in out and "youtu.be/vid1" in out
        assert "12.64-24.94s" in out

    def test_name_override_and_pick(self, clone_env) -> None:
        clone_env.markers_by_video["vid1"] = [marker(12.64, 24.94, 0.88), marker(30.0, 41.0, 0.8, text="second")]
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona", "--name", "dogbrian", "--pick", "2"])
        assert res.exit_code == 0, res.output
        assert FakeVox.created[0]["name"] == "dogbrian" and clone_env.prepared[0][1:] == (30.0, 41.0)

    def test_pick_out_of_range(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona", "--pick", "9"])
        assert res.exit_code == 2 and not FakeVox.created

    def test_weak_video_falls_through_to_next(self, clone_env) -> None:
        clone_env.markers_by_video["vid1"] = []
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 0, res.output
        assert [d[0] for d in clone_env.downloads] == ["vid1", "vid2"]
        assert clone_env.prepared[0][1:] == (3.0, 14.0)

    def test_low_score_falls_through_and_best_attempt_wins(self, clone_env) -> None:
        clone_env.markers_by_video["vid1"] = [marker(5.0, 16.0, 0.55)]   # under WEAK_SCORE
        clone_env.markers_by_video["vid2"] = [marker(3.0, 14.0, 0.50)]   # also weak, but worse
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 0, res.output
        assert len(clone_env.downloads) == 2 and clone_env.prepared[0][1:] == (5.0, 16.0)
        wd = cl.workdir_for("brian-griffin")
        assert (wd / "source.wav").read_bytes().endswith(b"vid1")  # winner linked at the root, loser's wav dropped
        assert [p.parent.name for p in (wd / "candidates").rglob("source.wav")] == ["1-vid1"]

    def test_max_videos_limits_attempts(self, clone_env) -> None:
        clone_env.markers_by_video = {"vid1": [], "vid2": []}
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--max-videos", "1"])
        assert res.exit_code == 1 and len(clone_env.downloads) == 1 and not FakeVox.created
        assert "no clean" in res.output.replace("\n", " ")

    def test_max_minutes_forwarded(self, clone_env) -> None:
        runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--max-minutes", "5"])
        assert clone_env.downloads[0] == ("vid1", 5.0)

    def test_dry_run_uploads_nothing(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--dry-run"])
        assert res.exit_code == 0, res.output
        assert not FakeVox.created and not FakeVox.persona_puts
        assert "dry run" in res.output and (cl.workdir_for("brian-griffin") / "brian-griffin.wav").is_file()

    def test_dry_run_with_existing_voice_only_notes_it(self, clone_env) -> None:
        FakeVox.voices["brian"] = VoiceOut(name="brian", display_name="B", duration_s=1.0)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--dry-run"])
        assert res.exit_code == 0 and "already exists" in res.output.replace("\n", " ")

    def test_existing_voice_refused_before_any_download(self, clone_env) -> None:
        FakeVox.voices["brian"] = VoiceOut(name="brian", display_name="B", duration_s=1.0)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 5 and not clone_env.downloads and not FakeVox.created

    def test_force_overwrites(self, clone_env) -> None:
        FakeVox.voices["brian"] = VoiceOut(name="brian", display_name="B", duration_s=1.0)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--force"])
        assert res.exit_code == 0 and FakeVox.created

    def test_non_engine_voxcpm_is_loud_and_exit_2(self, clone_env) -> None:
        FakeVox.engine = "elevenlabs"
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 2
        assert "NOT A CLONE" in res.output and "elevenlabs" in res.output

    def test_refuses_without_tty_or_yes(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "dog"])
        assert res.exit_code == 5 and not clone_env.downloads

    def test_invalid_name(self, clone_env) -> None:
        assert runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--name", "Bad Name"]).exit_code == 5

    def test_llm_failure_falls_back_to_query_identity(self, clone_env) -> None:
        clone_env.llm.replies = [LLMError("quota"), LLMError("quota")]
        res = runner.invoke(app, ["voice", "clone", "Morgan Freeman", "--yes", "--pick", "1", "--no-persona",
                                  "--name", "morgan"])
        assert res.exit_code == 0, res.output
        assert FakeVox.created[0]["name"] == "morgan" and "using your words as-is" in res.output.replace("\n", " ")

    def test_llm_failure_without_name_refuses_unattended(self, clone_env) -> None:
        """'the dog from Family Guy' with the LLM down must not upload a voice called 'the'."""
        clone_env.llm.replies = [LLMError("quota"), LLMError("quota")]
        res = runner.invoke(app, ["voice", "clone", "the dog from Family Guy", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 5 and not FakeVox.created and not clone_env.downloads
        assert "--name" in res.output.replace("\n", " ")

    def test_fallback_voice_name_skips_the_article(self) -> None:
        ident = cl.fallback_identity("the dog from Family Guy")
        assert ident.voice_name == "dog" and ident.resolved_by == "fallback"

    def test_low_confidence_refused_unattended_unless_force(self, clone_env) -> None:
        clone_env.llm.replies = [json.dumps({**BRIAN, "confidence": 0.3})]
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 5 and not FakeVox.created and not clone_env.downloads
        assert "low-confidence" in res.output.replace("\n", " ")
        clone_env.llm.replies = [json.dumps({**BRIAN, "confidence": 0.3})]
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--force"])
        assert res.exit_code == 0 and FakeVox.created

    @staticmethod
    def _anonymous_titles(clone_env) -> None:
        clone_env.videos = [cl.Video("vid1", "Family Guy funniest scenes", "https://youtu.be/vid1", 600, "FG"),
                            cl.Video("vid2", "Family Guy compilation", "https://youtu.be/vid2", 700, "FG2")]

    def test_fictional_character_with_target_titled_source_proceeds_under_yes(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona"])
        assert res.exit_code == 0, res.output
        assert FakeVox.created and "unattended pick" in res.output.replace("\n", " ")

    def test_title_naming_the_voice_actor_counts(self) -> None:
        ident = cl.fallback_identity("the narrator")
        ident = cl.Identity(**{**ident.__dict__, "display_name": "The Narrator", "voice_actor": "Ron Howard", "franchise": "Arrested Development"})
        assert vc._title_names_target(ident, cl.Video("v", "Ron Howard reads his memoir", "u", 300, "c"))
        assert not vc._title_names_target(ident, cl.Video("v", "Arrested Development best of", "u", 300, "c"))

    def test_fictional_character_without_ecapa_needs_pick_even_when_title_names_them(self, clone_env, monkeypatch) -> None:
        # MFCC alone reported ONE speaker for every window of a scene compilation: not evidence.
        monkeypatch.setattr(
            vc.refclip, "analyze",
            lambda path, **kw: fake_analysis(str(path), clone_env.markers_by_video[Path(path).parent.name.split("-", 1)[1]], gated=False),
        )
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona"])
        assert res.exit_code == 5 and not FakeVox.created
        assert "ECAPA" in res.output.replace("\n", " ")

    def test_fictional_character_needs_pick_under_yes(self, clone_env) -> None:
        self._anonymous_titles(clone_env)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona"])
        assert res.exit_code == 5 and not FakeVox.created
        out = res.output.replace("\n", " ")
        assert "--pick" in out and "fictional character" in out
        assert (cl.workdir_for("brian-griffin") / "markers.json").is_file()  # inspectable before the re-run

    def test_multi_speaker_source_needs_pick_under_yes(self, clone_env, monkeypatch) -> None:
        self._anonymous_titles(clone_env)
        clone_env.llm.replies = [json.dumps({**BRIAN, "franchise": None, "voice_actor": None})]
        two = rc.SpeakerMap(segments=[], dominant=1, n_clusters=2)
        monkeypatch.setattr(vc.refclip, "analyze", lambda path, **kw: fake_analysis(str(path), clone_env.markers_by_video["vid1"], two, gated=False))
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona"])
        assert res.exit_code == 5 and "2 speakers" in res.output.replace("\n", " ") and not FakeVox.created

    def test_real_person_single_speaker_needs_no_pick(self, clone_env) -> None:
        clone_env.llm.replies = [json.dumps({**BRIAN, "franchise": None, "voice_actor": None})]
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona"])
        assert res.exit_code == 0, res.output
        assert FakeVox.created

    def test_pick_zero_rejected_before_any_work(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "0"])
        assert res.exit_code == 2 and not clone_env.downloads and not clone_env.search_calls

    def test_pick_seven_of_ten_is_valid(self, clone_env) -> None:
        clone_env.markers_by_video["vid1"] = [marker(i * 20.0, i * 20.0 + 11, 0.9 - i * 0.01) for i in range(10)]
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--no-persona", "--pick", "7"])
        assert res.exit_code == 0, res.output
        assert clone_env.prepared[0][1:] == (120.0, 131.0)

    def test_server_error_on_voice_lookup_is_a_message_not_a_traceback(self, clone_env, monkeypatch) -> None:
        def boom(self, name):
            raise VoxServerError("500 boom")

        monkeypatch.setattr(FakeVox, "get_voice", boom)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 1 and not isinstance(res.exception, VoxServerError)
        assert "500 boom" in res.output.replace("\n", " ") and not clone_env.downloads

    def test_failed_analysis_falls_through_to_the_next_video(self, clone_env, monkeypatch) -> None:
        real = vc.refclip.analyze

        def flaky(path, **kw):
            if "vid1" in str(path):
                raise RuntimeError("ffmpeg could not decode source.wav")
            return real(path, **kw)

        monkeypatch.setattr(vc.refclip, "analyze", flaky)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 0, res.output
        assert [d[0] for d in clone_env.downloads] == ["vid1", "vid2"]
        assert clone_env.prepared[0][1:] == (3.0, 14.0)
        assert "analysis failed" in res.output.replace("\n", " ")

    def test_missing_dependency_during_analysis_is_still_fatal(self, clone_env, monkeypatch) -> None:
        def boom(path, **kw):
            raise rc.CloneDepsMissing("missing optional dependency 'faster-whisper'")

        monkeypatch.setattr(vc.refclip, "analyze", boom)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 1 and len(clone_env.downloads) == 1

    def test_selection_is_recorded_in_the_workdir(self, clone_env) -> None:
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona"])
        assert res.exit_code == 0, res.output
        wd = cl.workdir_for("brian-griffin")
        clip = json.loads((wd / "clip.json").read_text())
        assert clip["start"] == 12.64 and clip["end"] == 24.94 and clip["voice"] == "brian"
        assert clip["prompt_text"] == "So I wanted to escape from that home." and clip["uploaded"] is True
        assert clip["engine"] == "voxcpm" and clip["audio_url"].endswith("abc.ogg") and clip["video"]["id"] == "vid1"
        root = json.loads((wd / "markers.json").read_text())
        assert root["candidates"][0]["start"] == 12.64
        assert Path(root["source"]).is_file() and Path(root["source"]).parent.name == "1-vid1"  # not moved away

    def test_rerun_reuses_nothing_stale_but_finds_the_chosen_wav(self, clone_env) -> None:
        runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1", "--no-persona", "--dry-run"])
        wd = cl.workdir_for("brian-griffin")
        assert (wd / "candidates" / "1-vid1" / "source.wav").is_file()
        assert (wd / "source.wav").resolve() == (wd / "candidates" / "1-vid1" / "source.wav").resolve()

    def test_missing_extra_gives_exact_fix(self, monkeypatch, clone_env) -> None:
        monkeypatch.setattr(vc.importlib.util, "find_spec", lambda name: None)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1"])
        assert res.exit_code == 1
        assert 'uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force' in res.output.replace("\n", " ")

    @pytest.mark.skipif(not PLUGIN_FILE.is_file(), reason="voice-persona plugin not in this checkout")
    def test_persona_generated_written_and_associated(self, monkeypatch, clone_env, tmp_path: Path) -> None:
        plugin = bridge.load_plugin(REPO_ROOT)
        pdir = tmp_path / "personas"
        pdir.mkdir()
        monkeypatch.setattr(plugin, "PERSONAS_DIR", pdir)
        seen = {}

        def fake_gen(llm, plugin_, **kw):
            seen.update(kw)
            return good_persona()

        monkeypatch.setattr(vc.cl, "generate_persona", fake_gen)
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1"])
        assert res.exit_code == 0, res.output
        assert (pdir / "brian-griffin.md").is_file()
        assert FakeVox.persona_puts == [("brian", "brian-griffin")]
        assert seen["voice"] == "brian" and seen["display_name"] == "Brian Griffin" and seen["brief"]

    @pytest.mark.skipif(not PLUGIN_FILE.is_file(), reason="voice-persona plugin not in this checkout")
    def test_existing_persona_file_reused_not_overwritten(self, monkeypatch, clone_env, tmp_path: Path) -> None:
        plugin = bridge.load_plugin(REPO_ROOT)
        pdir = tmp_path / "personas"
        pdir.mkdir()
        (pdir / "brian-griffin.md").write_text(good_persona(name="Original"))
        monkeypatch.setattr(plugin, "PERSONAS_DIR", pdir)
        monkeypatch.setattr(vc.cl, "generate_persona", lambda *a, **k: pytest.fail("must not regenerate"))
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1"])
        assert res.exit_code == 0, res.output
        assert "Original" in (pdir / "brian-griffin.md").read_text()
        assert FakeVox.persona_puts == [("brian", "brian-griffin")]

    @pytest.mark.skipif(not PLUGIN_FILE.is_file(), reason="voice-persona plugin not in this checkout")
    def test_persona_failure_is_degraded_not_fatal(self, monkeypatch, clone_env, tmp_path: Path) -> None:
        plugin = bridge.load_plugin(REPO_ROOT)
        monkeypatch.setattr(plugin, "PERSONAS_DIR", tmp_path / "p")
        monkeypatch.setattr(vc.cl, "generate_persona", lambda *a, **k: (_ for _ in ()).throw(cl.CloneError("quota")))
        res = runner.invoke(app, ["voice", "clone", "dog", "--yes", "--pick", "1"])
        assert res.exit_code == 2 and FakeVox.created  # voice exists, persona missing
        assert "persona" in res.output and not FakeVox.persona_puts

    def test_help_documents_flags(self) -> None:
        out = runner.invoke(app, ["voice", "clone", "--help"]).output
        for flag in ("--yes", "--pick", "--dry-run", "--no-persona", "--force-persona", "--max-videos", "--max-minutes", "--force", "--name"):
            assert flag in out


class TestConfirmIdentity:
    def test_edit_returns_the_results_of_the_new_search(self, monkeypatch) -> None:
        """Persona facts must come from the corrected identity, not the search for the first guess."""
        monkeypatch.setattr(vc.sys.stdin, "isatty", lambda: True)
        answers = iter(["edit the description", "yes"])
        monkeypatch.setattr(vc.questionary, "select", lambda *a, **k: SimpleNamespace(ask=lambda: next(answers)))
        monkeypatch.setattr(vc.questionary, "text", lambda *a, **k: SimpleNamespace(ask=lambda: "Stewie from Family Guy"))
        searches = []
        new_results = [{"title": "Stewie Griffin", "snippet": "football head", "url": "http://stewie"}]
        monkeypatch.setattr(cl, "web_search", lambda q, n=5, **k: searches.append(q) or new_results)
        stewie = {**BRIAN, "display_name": "Stewie Griffin", "slug": "stewie-griffin"}
        llm = StubLLM([json.dumps(BRIAN), json.dumps(stewie)])
        old = [{"title": "Brian", "snippet": "dog", "url": "http://brian"}]
        identity, results = vc._confirm_identity("the dog", llm, old, yes=False)
        assert identity.slug == "stewie-griffin" and results == new_results and searches == ["Stewie from Family Guy"]


# ---------------------------------------------------------------------------
# persona generate
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PLUGIN_FILE.is_file(), reason="voice-persona plugin not in this checkout")
class TestPersonaGenerate:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        plugin = bridge.load_plugin(REPO_ROOT)
        self.pdir = tmp_path / "personas"
        self.pdir.mkdir()
        (self.pdir / "peter-griffin.md").write_text((REPO_ROOT / "plugins/voice-persona/personas/peter-griffin.md").read_text())
        monkeypatch.setattr(plugin, "PERSONAS_DIR", self.pdir)
        self.llm = StubLLM([good_persona(), good_persona(name="Brian v2")])
        monkeypatch.setattr(persona_cmd, "CloneLLM", lambda cfg=None: self.llm)
        monkeypatch.setattr(cl, "web_search", lambda q, n=5, **k: [])
        FakeVox.voices["brian"] = VoiceOut(name="brian", display_name="Brian", duration_s=5.0)

    def test_generates_writes_and_associates(self) -> None:
        res = runner.invoke(app, ["persona", "generate", "brian-griffin", "--character", "Brian Griffin from Family Guy", "--voice", "brian"])
        assert res.exit_code == 0, res.output
        text = (self.pdir / "brian-griffin.md").read_text()
        assert "voices: [brian]" in text and "## Examples" in text
        assert FakeVox.persona_puts == [("brian", "brian-griffin")]

    def test_refuses_overwrite_then_force(self) -> None:
        args = ["persona", "generate", "brian-griffin", "-c", "Brian Griffin"]
        assert runner.invoke(app, args).exit_code == 0
        assert runner.invoke(app, args).exit_code == 5
        assert "Original" not in (self.pdir / "brian-griffin.md").read_text()
        assert runner.invoke(app, [*args, "--force"]).exit_code == 0
        assert "name: Brian v2" in (self.pdir / "brian-griffin.md").read_text()

    def test_dry_run_prints_and_writes_nothing(self) -> None:
        res = runner.invoke(app, ["persona", "generate", "brian-griffin", "-c", "Brian Griffin", "--dry-run"])
        assert res.exit_code == 0 and "## Rules for rewriting" in res.output
        assert not (self.pdir / "brian-griffin.md").exists()

    def test_invalid_id_and_voice(self) -> None:
        assert runner.invoke(app, ["persona", "generate", "Bad Id", "-c", "x"]).exit_code == 5
        assert runner.invoke(app, ["persona", "generate", "ok-id", "-c", "x", "--voice", "BAD"]).exit_code == 5

    def test_character_required(self) -> None:
        assert runner.invoke(app, ["persona", "generate", "brian-griffin"]).exit_code != 0

    def test_generation_failure_exit_1(self) -> None:
        self.llm.replies = ["junk", "junk"]
        res = runner.invoke(app, ["persona", "generate", "brian-griffin", "-c", "Brian"])
        assert res.exit_code == 1 and not (self.pdir / "brian-griffin.md").exists()

    def test_unknown_voice_is_not_fatal(self) -> None:
        res = runner.invoke(app, ["persona", "generate", "brian-griffin", "-c", "Brian", "--voice", "ghost"])
        assert res.exit_code == 0 and FakeVox.persona_puts == []
