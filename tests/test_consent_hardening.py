"""Story G5, review round: the holes found in the first build, each pinned by the attack that proved it.

1. identity: the real key in X-API-Key beside made-up Authorization text must not become a made-up identity
2. the gate judges the very audio the voice is made from (capped before the gate, nothing dropped after)
3. a review decision acts only on the voice its own record made
4. two first recordings at once cannot both become "the account's person"
5. the ungated clone paths (per-request reference clip, POST /voices) hang off one switch
6. a self-edition voice stays silent in hosted
plus the minor items (lost-race record, newest-first listing, operator key, audio type) and the new speech minimum.
"""

from __future__ import annotations

import asyncio
import hashlib

import pytest
import soundfile as sf

from app import auth as auth_mod
from app import consent as consent_mod
from app import consent_store as consent_store_mod
from app import main
from app.consent import ACCEPTED, REJECTED, REVIEW
from app.voices import Voice
from consent_fakes import fake_providers, unit
from test_consent import _challenge, _mostly_heard, _own, _records, _review_env, _speak
from test_web_app import env  # noqa: F401  (the shared fixture)

ROOT_KEY = "root-key"
ROOT = hashlib.sha256(ROOT_KEY.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ 1. identity

def test_a_made_up_bearer_beside_the_real_x_api_key_is_still_the_root_identity(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", ROOT_KEY)
    env.embedder.speakers[440] = unit(0.02)
    real = {"X-API-Key": ROOT_KEY}
    mixed = {"X-API-Key": ROOT_KEY, "Authorization": "Bearer made-up-identity"}

    ch = env.client.post("/voices/consent/challenge", headers=real).json()
    env.asr.say(ch)
    assert _own(env, ch, make_wav, name="first", headers=real).status_code == 200
    assert list(env.store.voiceprints) == [ROOT]

    # a different person, arriving with the mixed headers, is held to the same account voiceprint
    ch = env.client.post("/voices/consent/challenge", headers=mixed).json()
    env.asr.say(ch)
    r = _own(env, ch, make_wav, name="second", hz=440.0, headers=mixed)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_ACCOUNT_MISMATCH
    assert list(env.store.voiceprints) == [ROOT]                      # no second identity appeared
    assert {r.principal for r in _records(env)} == {ROOT}


def test_a_request_without_a_passing_key_has_no_identity(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", ROOT_KEY)
    assert env.client.post("/voices/consent/challenge", headers={"Authorization": "Bearer nope"}).status_code == 401


# ------------------------------------------------------------------ 2. the gate judges what the voice is made from

def test_the_voice_is_made_from_exactly_the_audio_the_gate_judged(env, make_wav) -> None:
    ch = _challenge(env)
    r = _own(env, ch, make_wav, seconds=30.8)
    assert r.status_code == 200, r.text
    rec = _records(env)[0]
    kept = sf.read(env.dir / "_consent" / rec.consent_audio_path)[0]
    voice = sf.read(env.dir / "my-voice.wav")[0]
    assert len(kept) == len(voice)
    assert len(voice) / 16_000 == pytest.approx(30.0, abs=0.01)


def test_a_clip_past_the_cap_is_refused_before_the_gate(env, make_wav) -> None:
    ch = _challenge(env)
    assert _own(env, ch, make_wav, seconds=45.0).status_code == 422
    assert _own(env, ch, make_wav, seconds=9.0, reference=True, ref_seconds=45.0).status_code == 422
    assert _records(env) == []                                        # the challenge was never spent
    assert _own(env, ch, make_wav).status_code == 200


# ------------------------------------------------------------------ 3. a review acts on its own voice only

def _two_pending_recordings_of_one_name(env, make_wav, monkeypatch):
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    env.embedder.speakers[440] = unit(0.52)
    ch1 = env.client.post("/voices/consent/challenge").json()
    _mostly_heard(env, ch1)
    assert _own(env, ch1, make_wav, name="me").status_code == 200
    ch2 = env.client.post("/voices/consent/challenge").json()
    _mostly_heard(env, ch2)
    assert _own(env, ch2, make_wav, name="me", hz=440.0).status_code == 200
    first, second = sorted(_records(env), key=lambda r: r.created_at)
    return {"X-API-Key": "op-key"}, first, second


def test_a_newer_recording_supersedes_the_queued_one_for_the_same_name(env, make_wav, monkeypatch) -> None:
    op, first, second = _two_pending_recordings_of_one_name(env, make_wav, monkeypatch)
    assert (first.decision, first.reviewer) == (REJECTED, "system") and "superseded" in first.review_note
    queue = env.client.get("/consent/reviews", headers=op).json()
    assert [q["id"] for q in queue] == [second.id]
    stale = env.client.post(f"/consent/reviews/{first.id}", headers=op, json={"decision": "approve"})
    assert stale.status_code == 409                                    # not waiting for review any more
    assert consent_mod.TAG_PENDING in env.repo.voices["me"].tags


def test_approving_a_stale_record_never_verifies_a_newer_voice(env, make_wav, monkeypatch) -> None:
    """Defence in depth: even if an old record is somehow still in the queue, the record tag stops it."""
    op, first, second = _two_pending_recordings_of_one_name(env, make_wav, monkeypatch)
    first.decision, first.reviewer, first.review_note = REVIEW, None, None   # put it back in the queue
    for decision in ("approve", "reject"):
        r = env.client.post(f"/consent/reviews/{first.id}", headers=op, json={"decision": decision})
        assert r.status_code == 200
        first.decision = REVIEW                                                  # and again for the other verdict
        voice = env.repo.voices["me"]
        assert consent_mod.TAG_PENDING in voice.tags and consent_mod.TAG_VERIFIED not in voice.tags
        assert consent_mod.record_id_of(voice.tags) == second.id
        assert _speak(env, "me").status_code == 403
    # the voice's own record still works
    ok = env.client.post(f"/consent/reviews/{second.id}", headers=op, json={"decision": "approve"})
    assert ok.status_code == 200
    assert consent_mod.TAG_VERIFIED in env.repo.voices["me"].tags and _speak(env, "me").status_code == 200


def test_clients_never_see_the_record_tag(env, make_wav) -> None:
    r = _own(env, _challenge(env), make_wav)
    assert r.json()["tags"] == ["own", "consent-verified"]
    assert consent_mod.record_id_of(env.repo.voices["my-voice"].tags) == r.json()["consent_result"]["record_id"]
    assert env.client.get("/voices/my-voice").json()["tags"] == ["own", "consent-verified"]
    assert all(not t.startswith("consent-record:") for v in env.client.get("/voices").json() for t in v["tags"])


# ------------------------------------------------------------------ 4. two first recordings at once

class _SlowStore(consent_store_mod.MemoryConsentStore):
    """Pauses on its reads the way postgres does, so concurrent attempts interleave."""

    async def get_voiceprint(self, principal):
        found = await super().get_voiceprint(principal)
        await asyncio.sleep(0.05)
        return found


def _wav_file(tmp_path, make_wav, name: str, hz: float) -> str:
    path = tmp_path / name
    path.write_bytes(make_wav(seconds=9.0, hz=hz))
    return str(path)


def _fix_challenge(store, challenge, sentence, code) -> None:
    stored = store.challenges._items[challenge.challenge_id]
    stored.sentence, stored.code = sentence, code


def test_two_simultaneous_first_recordings_by_different_people_are_not_both_accepted(env, make_wav, tmp_path) -> None:
    providers = fake_providers()
    providers.embedder.speakers[440] = unit(0.02)
    store = _SlowStore()
    gate = consent_mod.ConsentGate(store, providers)
    sentence, code = consent_mod.SENTENCES[0], "483917"
    providers.transcriber.text = f"{sentence} 4 8 3 9 1 7"
    a, b = _wav_file(tmp_path, make_wav, "a.wav", 220.0), _wav_file(tmp_path, make_wav, "b.wav", 440.0)

    async def go():
        ca, cb = await store.issue_challenge("acct"), await store.issue_challenge("acct")
        for c in (ca, cb):
            _fix_challenge(store, c, sentence, code)
        return await asyncio.gather(
            gate.check(principal="acct", challenge_id=ca.challenge_id, voice="a", consent_audio=a),
            gate.check(principal="acct", challenge_id=cb.challenge_id, voice="b", consent_audio=b))

    out = asyncio.run(go())
    assert sorted(o.decision for o in out) == [ACCEPTED, REJECTED]
    loser = next(o for o in out if o.decision == REJECTED)
    assert loser.reason == consent_mod.REASON_ACCOUNT_MISMATCH and loser.record.speaker_score_account is not None
    assert len(store.voiceprints) == 1


def test_two_simultaneous_first_recordings_by_the_same_person_are_both_accepted(env, make_wav, tmp_path) -> None:
    providers = fake_providers()
    store = _SlowStore()
    gate = consent_mod.ConsentGate(store, providers)
    sentence, code = consent_mod.SENTENCES[0], "483917"
    providers.transcriber.text = f"{sentence} 4 8 3 9 1 7"
    a, b = _wav_file(tmp_path, make_wav, "a.wav", 220.0), _wav_file(tmp_path, make_wav, "b.wav", 220.0)

    async def go():
        ca, cb = await store.issue_challenge("acct"), await store.issue_challenge("acct")
        for c in (ca, cb):
            _fix_challenge(store, c, sentence, code)
        return await asyncio.gather(
            gate.check(principal="acct", challenge_id=ca.challenge_id, voice="a", consent_audio=a),
            gate.check(principal="acct", challenge_id=cb.challenge_id, voice="b", consent_audio=b))

    assert [o.decision for o in asyncio.run(go())] == [ACCEPTED, ACCEPTED]


# ------------------------------------------------------------------ 5. the ungated clone paths hang off one switch

def test_ungated_clone_allowed_is_the_single_switch(monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", ROOT_KEY)
    assert consent_mod.ungated_clone_allowed(ROOT, "hosted") is True          # the root key is the fleet's
    assert consent_mod.ungated_clone_allowed("some-customer", "hosted") is False
    assert consent_mod.ungated_clone_allowed("some-customer", "self") is True  # self edition: the operator's call
    monkeypatch.delenv("VOX_API_KEY")
    assert consent_mod.ungated_clone_allowed("open", "hosted") is True


def test_a_key_that_may_not_clone_ungated_is_refused_on_every_clip_path(env, make_wav, monkeypatch) -> None:
    monkeypatch.setattr(consent_mod, "ungated_clone_allowed", lambda principal, edition=None: False)
    clip = "AAAA"
    for path, body in (
        ("/synthesize", {"text": "hi", "reference_audio_b64": clip}),
        ("/synthesize-url", {"text": "hi", "reference_audio_b64": clip}),
        ("/sink/desk/say", {"text": "hi", "reference_audio_b64": clip}),
    ):
        r = env.client.post(path, json=body)
        assert r.status_code == 403, (path, r.text)
        assert r.headers["x-vox-error"] == "clone_requires_consent"
    up = env.client.post("/voices", data={"name": "grab", "display_name": "Grab"},
                         files={"audio": ("a.wav", make_wav(seconds=10.0), "audio/wav")})
    assert up.status_code == 403 and up.headers["x-vox-error"] == "clone_requires_consent"
    assert "grab" not in env.repo.voices and env.engine.calls == []
    # what the consent flow makes, and plain synthesis with a registered voice, stay open
    assert _own(env, _challenge(env), make_wav).status_code == 200
    assert _speak(env, "my-voice").status_code == 200
    assert env.client.post("/synthesize", json={"text": "hi", "voice": "my-voice"}).status_code == 200


def test_the_fleet_key_still_clones_per_request_and_uploads(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", ROOT_KEY)
    h = {"X-API-Key": ROOT_KEY}
    up = env.client.post("/voices", headers=h, data={"name": "fleet", "display_name": "Fleet"},
                         files={"audio": ("a.wav", make_wav(seconds=10.0), "audio/wav")})
    assert up.status_code == 200, up.text


# ------------------------------------------------------------------ 6. a self-edition voice is silent in hosted

def test_a_self_edition_voice_does_not_speak_after_a_switch_to_hosted(env, monkeypatch) -> None:
    env.repo.voices["old"] = Voice(name="old", display_name="Old", wav_path="house-f1.wav", duration_s=10.0,
                                   prompt_text=None, tags=["own", "consent-unchecked-self"])
    blocked = _speak(env, "old")
    assert blocked.status_code == 403 and blocked.headers["x-vox-error"] == "consent_pending"
    assert blocked.json()["detail"] == consent_mod.REASON_VOICE_UNCHECKED
    assert env.client.get("/voices/old").json()["consent"] == "pending"
    monkeypatch.setenv("VOX_EDITION", "self")
    assert _speak(env, "old").status_code == 200
    assert env.client.get("/voices/old").json()["consent"] == "unchecked"


# ------------------------------------------------------------------ minor items

def test_the_loser_of_a_race_on_one_challenge_still_leaves_a_record_and_its_audio(env, make_wav, tmp_path) -> None:
    providers = fake_providers()
    store = consent_store_mod.MemoryConsentStore()
    gate = consent_mod.ConsentGate(store, providers)
    sentence, code = consent_mod.SENTENCES[0], "483917"
    providers.transcriber.text = f"{sentence} 4 8 3 9 1 7"
    a, b = _wav_file(tmp_path, make_wav, "a.wav", 220.0), _wav_file(tmp_path, make_wav, "b.wav", 220.0)

    async def go():
        ch = await store.issue_challenge("acct")
        _fix_challenge(store, ch, sentence, code)
        return await asyncio.gather(
            gate.check(principal="acct", challenge_id=ch.challenge_id, voice="a", consent_audio=a),
            gate.check(principal="acct", challenge_id=ch.challenge_id, voice="b", consent_audio=b),
            return_exceptions=True)

    results = asyncio.run(go())
    assert sum(isinstance(r, consent_mod.ChallengeError) for r in results) == 1
    assert sum(isinstance(r, consent_mod.Outcome) for r in results) == 1
    lost = [r for r in store.records.values() if r.reason == consent_mod.REASON_CHALLENGE_USED]
    assert len(lost) == 1 and lost[0].decision == REJECTED
    assert (consent_mod.consent_dir() / lost[0].consent_audio_path).is_file()
    assert len(store.records) == 2


def test_all_view_keeps_the_newest_records_when_there_are_more_than_the_limit() -> None:
    async def go():
        store = consent_store_mod.MemoryConsentStore()
        for i in range(5):
            rec = consent_mod.ConsentRecord(
                id=f"r{i}", voice="v", principal="p", challenge_id=f"c{i}", sentence="s", code="1",
                decision=REVIEW, reason="x")
            rec.created_at = rec.created_at.replace(microsecond=i)
            await store.add_record(rec)
        return ([r.id for r in await store.list_records(limit=2)],
                [r.id for r in await store.list_records(limit=2, newest=True)])

    oldest, newest = asyncio.run(go())
    assert oldest == ["r0", "r1"] and newest == ["r3", "r4"]


def test_the_all_view_route_is_newest_last_and_the_review_view_is_a_queue(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    everything = env.client.get("/consent/reviews?status=all", headers=op).json()
    assert [r["decision"] for r in everything] == ["accepted", "review"]
    assert [r["id"] for r in env.client.get("/consent/reviews", headers=op).json()] == [everything[1]["id"]]


def test_an_operator_key_equal_to_the_api_key_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "same")
    monkeypatch.setenv("VOX_OPERATOR_KEY", "same")
    assert auth_mod.operator_key_conflict() is True
    assert auth_mod.resolve_operator_key_from_env() is None             # and the routes answer 503, not "open"
    monkeypatch.setenv("VOX_OPERATOR_KEY", "different")
    assert auth_mod.operator_key_conflict() is False
    assert auth_mod.resolve_operator_key_from_env() == "different"


def test_review_routes_answer_503_when_the_operator_key_equals_the_api_key(env, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "same")
    monkeypatch.setenv("VOX_OPERATOR_KEY", "same")
    assert env.client.get("/consent/reviews", headers={"X-API-Key": "same"}).status_code == 503


def test_review_audio_is_served_in_the_format_it_was_kept_in(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    rec = env.client.get("/consent/reviews", headers=op).json()[0]
    stored = env.store.records[rec["id"]]
    kept = env.dir / "_consent"
    ogg = kept / f"{stored.id}.consent.ogg"
    sf.write(ogg, sf.read(kept / stored.consent_audio_path)[0], 16_000, format="OGG", subtype="VORBIS")
    stored.consent_audio_path = ogg.name
    r = env.client.get(f"/consent/reviews/{stored.id}/audio", headers=op)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/ogg" and r.content[:4] == b"OggS"


def test_a_voice_replaced_by_a_fresh_accepted_recording_clears_its_old_review(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    ch = env.client.post("/voices/consent/challenge").json()
    _mostly_heard(env, ch)
    assert _own(env, ch, make_wav, name="me").status_code == 200
    assert env.client.get("/consent/reviews", headers={"X-API-Key": "op-key"}).json() != []
    assert _own(env, _challenge(env), make_wav, name="me").status_code == 200
    assert env.client.get("/consent/reviews", headers={"X-API-Key": "op-key"}).json() == []
    assert consent_mod.TAG_VERIFIED in env.repo.voices["me"].tags


# ------------------------------------------------------------------ the speech minimum (VAD)

def _speech_of(seconds: float):
    return lambda samples: samples[: int(seconds * 16_000)]


def test_too_little_speech_in_the_consent_clip_is_a_plain_refusal_and_never_embedded(env, make_wav) -> None:
    main._consent_gate.providers.speech = _speech_of(5.0)
    r = _own(env, _challenge(env), make_wav)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_SPEECH
    assert env.embedder.calls == 0 and "my-voice" not in env.repo.voices
    rec = _records(env)[0]
    assert rec.decision == REJECTED and rec.embedding is None and "open" not in env.store.voiceprints


def test_too_little_speech_in_the_longer_sample_is_refused_too(env, make_wav) -> None:
    main._consent_gate.providers.speech = lambda samples: samples[: int((6.5 if len(samples) < 16_000 * 11 else 7.0) * 16_000)]
    r = _own(env, _challenge(env), make_wav, reference=True, ref_seconds=14.0)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_REFERENCE_SPEECH


def test_the_speech_minimums_come_from_the_environment(env, make_wav, monkeypatch) -> None:
    main._consent_gate.providers.speech = _speech_of(5.0)
    monkeypatch.setenv("VOX_CONSENT_MIN_SPEECH", "4.5")
    assert _own(env, _challenge(env), make_wav).status_code == 200


def test_the_embedder_hears_speech_only(env, make_wav) -> None:
    heard = []
    real = env.embedder.embed
    env.embedder.embed = lambda samples, sr: heard.append(len(samples) / sr) or real(samples, sr)
    main._consent_gate.providers.speech = _speech_of(7.0)
    assert _own(env, _challenge(env), make_wav, seconds=9.0).status_code == 200
    assert heard == [pytest.approx(7.0)]
