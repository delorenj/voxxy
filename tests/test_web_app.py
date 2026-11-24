"""The Voxxy web app's server side: shell and assets, /app-config, describe-a-voice, own-voice consent.

Driven with a bare TestClient (no lifespan, no postgres, no engines), like test_synthesize_routing.py:
``app.main._repo`` is an in-memory repo that supports upsert/delete, and ``_engine`` is a fake VoxCPM that
records the text it was asked to speak.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Optional

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import consent as consent_mod
from app import consent_store as consent_store_mod
from app import edition as edition_mod
from app import main
from app import voices as voices_mod
from app.auth import is_public_request
from app.engines import DEFAULT_ENGINE_LANGUAGES, EngineOrchestrator, SynthResult
from app.voices import Voice
from consent_fakes import fake_load_audio, fake_providers


class FakeVoxcpm:
    name = "voxcpm"
    accepts_reference = True

    def __init__(self, wav: bytes) -> None:
        self.wav = wav
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    async def generate(self, **kwargs) -> SynthResult:
        self.calls.append(kwargs)
        return SynthResult(wav_bytes=self.wav, sample_rate=16_000, engine=self.name)


class MemoryRepo:
    def __init__(self, *voices: Voice) -> None:
        self.voices = {v.name: v for v in voices}

    async def get(self, name: str) -> Optional[Voice]:
        return self.voices.get(name)

    async def list(self) -> list[Voice]:
        return sorted(self.voices.values(), key=lambda v: v.name)

    async def upsert(self, *, name, display_name, wav_path, duration_s, source_path=None, prompt_text=None,
                     tags=None, vibevoice_ref_path=None, vibevoice_speaker_tag=None) -> Voice:
        v = Voice(name=name, display_name=display_name, wav_path=wav_path, duration_s=duration_s,
                  prompt_text=prompt_text, tags=list(tags or []), vibevoice_ref_path=vibevoice_ref_path)
        self.voices[name] = v
        return v

    async def set_tags(self, name: str, tags: list[str]) -> Optional[Voice]:
        v = self.voices.get(name)
        if v is None:
            return None
        v.tags = list(tags)
        return v

    async def delete(self, name: str) -> bool:
        return self.voices.pop(name, None) is not None


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path, make_wav):
    """A TestClient over app.main with fakes. ``env.client``, ``.engine``, ``.repo``, ``.dir``."""
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    monkeypatch.delenv("VOX_EDITION", raising=False)
    monkeypatch.delenv("VOX_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("VOX_DESIGN_ENGINE", raising=False)
    monkeypatch.setattr(voices_mod, "VOICES_DIR", tmp_path)
    (tmp_path / "house-f1.wav").write_bytes(make_wav(seconds=10.0))
    repo = MemoryRepo(
        Voice(name="house-f1", display_name="House F1", wav_path="house-f1.wav", duration_s=10.0,
              prompt_text="t", tags=["house"]),
        Voice(name="arnold", display_name="Arnold", wav_path="arnold.wav", duration_s=10.0,
              prompt_text="t", tags=[]),
    )
    engine = FakeVoxcpm(make_wav(seconds=3.0, sr=16_000))
    monkeypatch.setattr(main, "_repo", repo)
    monkeypatch.setattr(main, "_engine", EngineOrchestrator(
        [engine], reference_engines=["voxcpm"], engine_languages=dict(DEFAULT_ENGINE_LANGUAGES)))
    for var in ("VOX_OPERATOR_KEY", "VOX_CONSENT_DIR", "VOX_CONSENT_ACCEPT", "VOX_CONSENT_REJECT",
                "VOX_CONSENT_PHRASE_OK", "VOX_CONSENT_PHRASE_REVIEW"):
        monkeypatch.delenv(var, raising=False)
    providers = fake_providers()
    store = consent_store_mod.MemoryConsentStore()
    monkeypatch.setattr(main, "_consent_gate", consent_mod.ConsentGate(store, providers))
    monkeypatch.setattr(consent_mod.vp_mod, "load_audio_16k", fake_load_audio)
    monkeypatch.setattr(main.audio_codec, "to_ogg_opus", lambda wav, input_format=None: b"OggS-fake")
    monkeypatch.setattr(main.audio_cache, "put", lambda data: "abc123")

    class Env:
        client = TestClient(main.app)
        pass

    e = Env()
    e.engine, e.repo, e.dir, e.monkeypatch = engine, repo, tmp_path, monkeypatch  # type: ignore[attr-defined]
    e.store, e.asr, e.embedder = store, providers.transcriber, providers.embedder  # type: ignore[attr-defined]
    return e


# ------------------------------------------------------------------ shell, assets, auth paths

def test_root_serves_the_app_shell(env) -> None:
    r = env.client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>Voxxy</title>" in r.text
    assert "Voxxy · part of HeyMa" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_static_assets_are_served_under_ui(env) -> None:
    js = env.client.get("/ui/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    css = env.client.get("/ui/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    mark = env.client.get("/ui/voxxy-mark.svg")
    assert mark.status_code == 200 and "svg" in mark.headers["content-type"]
    assert env.client.get("/ui/nope.js").status_code == 404


def test_static_prefix_cannot_escape_the_web_directory(env) -> None:
    assert env.client.get("/ui/../main.py").status_code in (400, 404)
    assert env.client.get("/ui/%2e%2e/main.py").status_code in (400, 404)


def test_every_html_asset_the_shell_references_exists(env) -> None:
    import re

    html = env.client.get("/").text
    refs = re.findall(r'(?:href|src)="(/ui/[^"]+)"', html)
    assert refs
    for ref in refs:
        assert env.client.get(ref).status_code == 200, ref


def test_web_paths_are_public_but_data_routes_stay_protected(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "secret")
    c = env.client
    assert c.get("/").status_code == 200
    assert c.get("/ui/app.js").status_code == 200
    assert c.get("/app-config").status_code == 200
    assert c.get("/voices").status_code == 401
    assert c.post("/voices/design", json={}).status_code == 401
    assert c.post("/voices/consent/challenge").status_code == 401
    assert c.post("/voices/own").status_code == 401
    assert c.post("/voices/design/preview", json={}).status_code == 401
    assert c.get("/voices", headers={"X-API-Key": "secret"}).status_code == 200


@pytest.mark.parametrize("method,path,public", [
    ("GET", "/", True),
    ("HEAD", "/", True),
    ("GET", "/app-config", True),
    ("GET", "/ui/app.js", True),
    ("GET", "/ui/sub/dir/file.css", True),
    ("HEAD", "/ui/app.css", True),
    ("POST", "/ui/app.js", False),
    ("POST", "/app-config", False),
    ("GET", "/ui", False),            # not the asset prefix itself
    ("GET", "/uix/app.js", False),    # a sibling, not the prefix
    ("GET", "/voices", False),
    ("GET", "/voices/design", False),
    ("DELETE", "/voices/rick", False),
])
def test_is_public_request_for_web_paths(method: str, path: str, public: bool) -> None:
    assert is_public_request(method, path) is public


# ------------------------------------------------------------------ /app-config

def test_app_config_defaults_to_hosted_and_strict(env) -> None:
    body = env.client.get("/app-config").json()
    assert body["edition"] == "hosted"
    assert body["auth_required"] is False
    assert body["features"]["upload_clip"] is False
    assert body["features"]["real_name_check"] is True
    assert body["features"]["consent_verification"] is True
    assert body["features"]["consent_reference_upload"] is False
    assert body["public_base_url"].startswith("http")


def test_app_config_self_edition_offers_upload(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_EDITION", "self")
    body = env.client.get("/app-config").json()
    assert body["edition"] == "self"
    assert body["features"]["upload_clip"] is True
    assert body["features"]["real_name_check"] is False


@pytest.mark.parametrize("raw", ["", "HOSTED", "enterprise", "selfhosted", " "])
def test_unknown_edition_falls_back_to_hosted(env, monkeypatch, raw: str) -> None:
    monkeypatch.setenv("VOX_EDITION", raw)
    assert env.client.get("/app-config").json()["edition"] == "hosted"


def test_app_config_reports_whether_a_key_is_required_but_never_the_key(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "super-secret-value")
    r = env.client.get("/app-config")
    assert r.json()["auth_required"] is True
    assert "super-secret-value" not in r.text
    monkeypatch.setenv("VOX_API_KEY", "   ")
    assert env.client.get("/app-config").json()["auth_required"] is False


def test_app_config_public_base_url_prefers_the_configured_one(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_PUBLIC_BASE_URL", "https://vox.example/")
    assert env.client.get("/app-config").json()["public_base_url"] == "https://vox.example"
    monkeypatch.delenv("VOX_PUBLIC_BASE_URL")
    r = env.client.get("/app-config", headers={"x-forwarded-proto": "https", "x-forwarded-host": "voices.example"})
    assert r.json()["public_base_url"] == "https://voices.example"


# ------------------------------------------------------------------ the real-person heuristic

@pytest.mark.parametrize("description", [
    "sounds like Morgan Freeman",
    "A deep voice that sounds like morgan freeman",
    "the voice of Oprah",
    "voice of oprah winfrey, warm",
    "an impression of Arnold Schwarzenegger",
    "impersonating donald trump",
    "in the style of David Attenborough, calm",
    "Make it Morgan Freeman but younger",
    "Sounds like Scarlett Johansson in Her",
    "similar to joe rogan",
    "Taylor Swift singing slowly",
    "Young Morgan Freeman",
    "reminds me of barack obama",
    "Exactly like Oprah, warm and bold",
    "deep narrator, like morgan freeman",
    "An Obama-style speaker, measured cadence",
])
def test_names_of_real_people_are_refused(description: str) -> None:
    assert edition_mod.refuse_real_person(description) is not None


@pytest.mark.parametrize("description", [
    "A calm woman in her forties, slow and warm, with a light Irish accent",
    "A young man, quick and bright, friendly and a little playful, American",
    "An older man with a deep, gravelly voice, steady pace, serious and kind",
    "sounds like a tired old man at the end of a long day",
    "Voice of a friendly narrator for children's stories",
    "An impression of a cheerful robot, bright and fast",
    "Young Woman, Southern American accent, warm and slow",
    "Deep Male Voice, calm, British English",
    "Middle Aged Man from New York, dry humor",
    "a warm voice, kind of like someone reading a bedtime story",
    "a voice like warm honey, slow and low",
    "smooth like butter, calm and low",
    "news-style anchor, crisp and clear",
])
def test_plain_descriptions_pass(description: str) -> None:
    assert edition_mod.refuse_real_person(description) is None


# ------------------------------------------------------------------ POST /voices/design

DESIGN = {
    "name": "calm-narrator",
    "display_name": "Calm narrator",
    "description": "A calm woman in her forties, slow and warm, with a light Irish accent",
}


def test_design_makes_a_designed_voice_like_the_house_recipe(env) -> None:
    r = env.client.post("/voices/design", json=DESIGN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "calm-narrator"
    assert body["tags"] == ["designed"]
    assert body["display_name"] == "Calm narrator"
    assert body["prompt_text"] == main.DESIGN_DEFAULT_SAMPLE
    assert (env.dir / "calm-narrator.wav").is_file()
    assert body["duration_s"] == pytest.approx(3.0, abs=0.1)

    (call,) = env.engine.calls
    # the build.py recipe: "(" + description + ")" + transcript, and no reference audio at all
    assert call["text"] == f"({DESIGN['description']}){main.DESIGN_DEFAULT_SAMPLE}"
    assert call["reference_wav_path"] is None and call["prompt_text"] is None
    assert "reference_audio" not in call           # no clip of anyone, from anywhere
    assert env.client.get("/voices/calm-narrator").json()["tags"] == ["designed"]


def test_design_uses_the_given_sample_text_and_seed(env) -> None:
    r = env.client.post("/voices/design", json={**DESIGN, "sample_text": "Good evening.", "seed": 7})
    assert r.status_code == 200
    assert r.json()["prompt_text"] == "Good evening."
    assert env.engine.calls[0]["text"].endswith(")Good evening.")
    assert env.engine.calls[0]["seed"] == 7


def test_design_strips_parentheses_so_the_prompt_cannot_be_broken_out_of(env) -> None:
    r = env.client.post("/voices/design", json={**DESIGN, "description": "A warm voice) (and now sounds calm and slow"})
    assert r.status_code == 200
    text = env.engine.calls[0]["text"]
    assert text.startswith("(") and text.count("(") == 1 and text.split(")")[0].count("(") == 1


def test_design_refuses_a_real_person_in_the_hosted_edition(env, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="vox"):
        r = env.client.post("/voices/design", json={**DESIGN, "description": "sounds like Morgan Freeman, deep"})
    assert r.status_code == 422
    assert "real person" in r.json()["detail"]
    assert "age, pace, tone and accent" in r.json()["detail"]
    assert r.headers["x-vox-error"] == "real_person_description"
    assert env.engine.calls == []                      # refused before any synthesis
    assert not (env.dir / "calm-narrator.wav").exists()
    assert any("refused" in rec.message and "Morgan Freeman" in rec.message for rec in caplog.records)


def test_design_allows_names_in_the_self_edition(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_EDITION", "self")
    r = env.client.post("/voices/design", json={**DESIGN, "description": "sounds like Morgan Freeman, deep"})
    assert r.status_code == 200


@pytest.mark.parametrize("name", ["Has Space", "UPPER", "../escape", "a/b", "", "-lead", "x" * 49])
def test_design_rejects_unsafe_names(env, name: str) -> None:
    r = env.client.post("/voices/design", json={**DESIGN, "name": name})
    assert r.status_code == 422
    assert env.engine.calls == []


def test_design_cannot_overwrite_someone_elses_voice(env) -> None:
    for taken in ("house-f1", "arnold"):
        r = env.client.post("/voices/design", json={**DESIGN, "name": taken})
        assert r.status_code == 409
    assert env.repo.voices["house-f1"].tags == ["house"]
    assert env.engine.calls == []


def test_design_can_replace_an_earlier_designed_voice(env) -> None:
    assert env.client.post("/voices/design", json=DESIGN).status_code == 200
    assert env.client.post("/voices/design", json={**DESIGN, "display_name": "Calmer"}).status_code == 200
    assert env.repo.voices["calm-narrator"].display_name == "Calmer"


def test_design_requires_a_real_description(env) -> None:
    assert env.client.post("/voices/design", json={**DESIGN, "description": "hi"}).status_code == 422
    assert env.client.post("/voices/design", json={"name": "x"}).status_code == 422


def test_design_uses_the_configured_design_engine(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_DESIGN_ENGINE", "elsewhere")
    r = env.client.post("/voices/design", json=DESIGN)
    assert r.status_code == 503  # no eligible engine named "elsewhere": fail closed, no stock fallback


def test_design_preview_returns_a_url_and_stores_nothing(env) -> None:
    r = env.client.post("/voices/design/preview", json={"description": DESIGN["description"]})
    assert r.status_code == 200
    assert r.json()["audio_url"].endswith("/audio/abc123.ogg")
    assert env.engine.calls[0]["text"].startswith(f"({DESIGN['description']})")
    assert "calm-narrator" not in env.repo.voices
    assert list(env.dir.glob("*.wav")) == [env.dir / "house-f1.wav"]


def test_design_preview_refuses_real_people_too(env) -> None:
    r = env.client.post("/voices/design/preview", json={"description": "the voice of Oprah Winfrey"})
    assert r.status_code == 422
    assert env.engine.calls == []


# ------------------------------------------------------------------ consent challenge + POST /voices/own

def _challenge(env) -> dict:
    """A fresh challenge, with the fake ASR set to hear the sentence and the code read correctly."""
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.say(ch)
    return ch


def _own(env, challenge: dict, wav: bytes, *, name: str = "my-voice", display: str = "My voice", filename: str = "rec.wav"):
    return env.client.post(
        "/voices/own",
        data={"challenge_id": challenge["challenge_id"], "name": name, "display_name": display},
        files={"audio": (filename, wav, "audio/wav")},
    )


def test_challenge_shape(env) -> None:
    r = env.client.post("/voices/consent/challenge")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"challenge_id", "sentence", "code", "expires_at"}
    assert len(body["code"]) == 6 and body["code"].isdigit()
    assert body["sentence"] in consent_mod.SENTENCES
    assert body["record_min_seconds"] == 8 and body["record_max_seconds"] == 30
    other = env.client.post("/voices/consent/challenge").json()
    assert other["challenge_id"] != body["challenge_id"]


def test_challenge_ttl_is_ten_minutes() -> None:
    store = consent_mod.ChallengeStore()
    c = store.issue(now=1000.0)
    assert c.expires_at - c.created_at == 600
    store.check(c.challenge_id, now=1599.0)
    with pytest.raises(consent_mod.ChallengeError) as exc:
        store.check(c.challenge_id, now=1600.0)
    assert exc.value.kind == "expired"


def test_challenge_is_single_use_and_unknown_ids_fail() -> None:
    store = consent_mod.ChallengeStore()
    c = store.issue(now=0.0)
    redeemed = store.redeem(c.challenge_id, now=5.0)
    assert (redeemed.sentence, redeemed.code) == (c.sentence, c.code)
    with pytest.raises(consent_mod.ChallengeError) as used:
        store.redeem(c.challenge_id, now=6.0)
    assert used.value.kind == "used"
    with pytest.raises(consent_mod.ChallengeError) as unknown:
        store.redeem("nope", now=6.0)
    assert unknown.value.kind == "unknown"


def test_challenge_store_is_bounded() -> None:
    store = consent_mod.ChallengeStore()
    for i in range(consent_mod._MAX_OPEN + 50):
        store.issue(now=float(i))
    assert len(store._items) <= consent_mod._MAX_OPEN


def test_own_voice_is_stored_tagged_and_verified(env, make_wav) -> None:
    ch = _challenge(env)
    r = _own(env, ch, make_wav(seconds=9.0, sr=16_000))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "my-voice"
    assert body["tags"] == ["own", "consent-verified"]
    assert body["consent"] == "verified"
    assert body["consent_result"]["decision"] == "accepted" and body["consent_result"]["usable"] is True
    assert body["duration_s"] == pytest.approx(9.0, abs=0.1)
    assert (env.dir / "my-voice.wav").is_file()

    record = next(iter(env.store.records.values()))
    assert record.id == body["consent_result"]["record_id"]
    assert record.challenge_id == ch["challenge_id"]
    assert record.code == ch["code"] and record.sentence == ch["sentence"]
    assert record.decision == "accepted" and record.code_ok is True and record.phrase_score == 1.0
    assert (env.dir / "_consent" / record.consent_audio_path).is_file()


def test_own_voice_challenge_cannot_be_reused(env, make_wav) -> None:
    ch = _challenge(env)
    assert _own(env, ch, make_wav(seconds=9.0)).status_code == 200
    again = _own(env, ch, make_wav(seconds=9.0), name="second")
    assert again.status_code == 410
    assert again.headers["x-vox-error"] == "challenge_used"
    assert "second" not in env.repo.voices


def test_own_voice_unknown_and_expired_challenges(env, make_wav, monkeypatch) -> None:
    r = _own(env, {"challenge_id": "never-issued"}, make_wav(seconds=9.0))
    assert r.status_code == 404
    assert r.headers["x-vox-error"] == "challenge_unknown"

    ch = _challenge(env)
    env.store.challenges._items[ch["challenge_id"]].expires_at = 0.0
    r = _own(env, ch, make_wav(seconds=9.0))
    assert r.status_code == 410
    assert r.headers["x-vox-error"] == "challenge_expired"
    assert "my-voice" not in env.repo.voices


def test_own_voice_too_short_is_refused_without_burning_the_challenge(env, make_wav) -> None:
    ch = _challenge(env)
    short = _own(env, ch, make_wav(seconds=3.0))
    assert short.status_code == 422
    assert "at least 8 seconds" in short.json()["detail"]
    assert "my-voice" not in env.repo.voices
    assert _own(env, ch, make_wav(seconds=9.0)).status_code == 200   # same challenge, second try works


def test_own_voice_empty_and_unreadable_recordings(env) -> None:
    ch = _challenge(env)
    assert _own(env, ch, b"").status_code == 422
    r = _own(env, ch, b"this is not audio at all" * 50, filename="rec.webm")
    assert r.status_code == 422
    assert "record again" in r.json()["detail"]
    assert "my-voice" not in env.repo.voices


def test_own_voice_will_not_take_over_another_voices_name(env, make_wav) -> None:
    ch = _challenge(env)
    r = _own(env, ch, make_wav(seconds=9.0), name="house-f1")
    assert r.status_code == 409
    assert env.repo.voices["house-f1"].tags == ["house"]
    # the challenge was not spent by the failed attempt
    assert _own(env, ch, make_wav(seconds=9.0)).status_code == 200


def test_own_voice_rejects_unsafe_names(env, make_wav) -> None:
    ch = _challenge(env)
    assert _own(env, ch, make_wav(seconds=9.0), name="../../etc/passwd").status_code == 422


def test_own_voice_trims_timer_jitter_before_the_gate_and_refuses_anything_longer(env, make_wav) -> None:
    """A second of jitter past 30 s is trimmed BEFORE the gate, so what is judged is what the voice is made
    from; a longer clip is refused outright (the gate never sees audio it could not have judged)."""
    ch = _challenge(env)
    r = _own(env, ch, make_wav(seconds=30.6, sr=8_000))
    assert r.status_code == 200, r.text
    assert r.json()["duration_s"] == pytest.approx(main.REF_AUDIO_MAX_SECONDS, abs=0.1)
    kept = next(iter(env.store.records.values()))
    assert sf.info(env.dir / "_consent" / kept.consent_audio_path).duration == pytest.approx(30.0, abs=0.1)
    ch = _challenge(env)
    long = _own(env, ch, make_wav(seconds=40.0, sr=8_000))
    assert long.status_code == 422 and "keep it under 30" in long.json()["detail"]
    assert len(env.store.records) == 1                       # refused before the gate: challenge unspent


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_own_voice_accepts_a_browser_container_via_ffmpeg(env, make_wav, tmp_path) -> None:
    """MediaRecorder gives webm/opus or mp4, which libsndfile cannot read; the server converts."""
    src = tmp_path / "src.wav"
    src.write_bytes(make_wav(seconds=9.0, sr=16_000))
    out = tmp_path / "rec.webm"
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c:a", "libopus", str(out)],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("this ffmpeg cannot write webm/opus")
    ch = _challenge(env)
    r = _own(env, ch, out.read_bytes(), filename="recording.webm")
    assert r.status_code == 200, r.text
    assert r.json()["duration_s"] == pytest.approx(9.0, abs=0.3)


def test_plain_upload_still_works_in_the_hosted_edition(env, make_wav) -> None:
    """The fleet uploads through POST /voices; only the web UI hides the path in hosted."""
    r = env.client.post(
        "/voices",
        data={"name": "fleet-voice", "display_name": "Fleet", "tags": "uploaded"},
        files={"audio": ("clip.wav", make_wav(seconds=5.0), "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["tags"] == ["uploaded"]
    assert (env.dir / "fleet-voice.wav").is_file()


def test_deleting_an_own_voice_keeps_its_consent_record(env, make_wav) -> None:
    ch = _challenge(env)
    assert _own(env, ch, make_wav(seconds=9.0)).status_code == 200
    assert env.client.delete("/voices/my-voice").status_code == 200
    assert not (env.dir / "my-voice.wav").exists()
    # the consent record and its audio outlive the voice (product doc 4.1 step 6)
    record = next(iter(env.store.records.values()))
    assert (env.dir / "_consent" / record.consent_audio_path).is_file()


# ------------------------------------------------------------------ review fixes

@pytest.mark.parametrize("description", ["        ", "((((((((((", "\n\n\n\n\n\n\n\n", "(  a  )(   )"])
@pytest.mark.parametrize("path", ["/voices/design", "/voices/design/preview"])
@pytest.mark.parametrize("edition", ["hosted", "self"])
def test_a_description_that_is_blank_after_tidying_is_422_not_500(env, monkeypatch, description, path, edition) -> None:
    monkeypatch.setenv("VOX_EDITION", edition)
    body = {"description": description}
    if path == "/voices/design":
        body.update(name="blank-one", display_name="Blank")
    assert env.client.post(path, json=body).status_code == 422
    assert "blank-one" not in env.repo.voices


def test_a_display_name_of_only_spaces_is_refused(env, make_wav) -> None:
    assert env.client.post("/voices/design", json={**DESIGN, "display_name": "   "}).status_code == 422
    ch = _challenge(env)
    r = _own(env, ch, make_wav(seconds=9.0), display="   ")
    assert r.status_code == 422
    # the refusal did not burn the challenge
    assert _own(env, ch, make_wav(seconds=9.0)).status_code == 200


def test_post_voices_cannot_write_outside_the_voices_folder(env, make_wav) -> None:
    r = env.client.post(
        "/voices", data={"name": "../escaped", "display_name": "X"},
        files={"audio": ("clip.wav", make_wav(seconds=5.0), "audio/wav")},
    )
    assert r.status_code == 422
    assert not (env.dir.parent / "escaped.wav").exists()
    assert "../escaped" not in env.repo.voices


def _webm(make_wav, tmp_path, suffix: str = ".webm", extra: list[str] | None = None) -> bytes:
    src = tmp_path / "src.wav"
    src.write_bytes(make_wav(seconds=9.0, sr=16_000))
    out = tmp_path / f"rec{suffix}"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-c:a", "libopus" if suffix == ".webm" else "aac"]
    proc = subprocess.run(cmd + (extra or []) + [str(out)], capture_output=True)
    if proc.returncode != 0:
        pytest.skip(f"this ffmpeg cannot write {suffix}")
    return out.read_bytes()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
@pytest.mark.parametrize("suffix", [".webm", ".m4a"])
def test_post_voices_decodes_formats_soundfile_cannot_read(env, make_wav, tmp_path, suffix) -> None:
    data = _webm(make_wav, tmp_path, suffix)
    r = env.client.post(
        "/voices", data={"name": "from-phone", "display_name": "Phone"},
        files={"audio": (f"clip{suffix}", data, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["duration_s"] == pytest.approx(9.0, abs=0.3)
    assert (env.dir / "from-phone.wav").is_file()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_an_mp4_with_the_moov_atom_at_the_end_still_decodes(env, make_wav, tmp_path) -> None:
    data = _webm(make_wav, tmp_path, ".mp4")  # ffmpeg's default for mp4 puts moov last
    ch = _challenge(env)
    r = _own(env, ch, data, filename="recording.mp4")
    assert r.status_code == 200, r.text
    assert r.json()["duration_s"] == pytest.approx(9.0, abs=0.3)


def test_unreadable_upload_to_post_voices_is_422(env) -> None:
    r = env.client.post(
        "/voices", data={"name": "junk", "display_name": "Junk"},
        files={"audio": ("clip.m4a", b"not audio at all", "audio/mp4")},
    )
    assert r.status_code == 422
