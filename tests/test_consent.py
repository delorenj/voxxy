"""Story G5: the consent gate. Pure text and decision logic, principal derivation, and the routes
(own-voice recording, the review queue, synthesis with a pending voice, the self edition).

The gate takes its models through ``Providers``, so these tests use the scripted fakes in consent_fakes.py:
the transcriber says whatever the test scripts, and the embedder tells speakers apart by the pitch of the
test tone (220 Hz = the account's person unless a test says otherwise).
"""

from __future__ import annotations

import hashlib

import pytest
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from app import asr as asr_mod
from app import auth as auth_mod
from app import consent as consent_mod
from app import main
from app.auth import principal_for
from app.consent import ACCEPTED, REJECTED, REVIEW, Thresholds, decide
from app.voices import Voice
from consent_fakes import unit
from test_web_app import env  # noqa: F401  (the shared fixture: TestClient over app.main with fakes)

SENTENCE = consent_mod.SENTENCES[0]


# ------------------------------------------------------------------ the text checks

@pytest.mark.parametrize("said, code, expected", [
    ("four eight three nine one seven", "483917", "ok"),
    ("The code is 483917.", "483917", "ok"),
    ("483, 917", "483917", "ok"),
    ("4 8 3 9 1 7", "483917", "ok"),
    ("four hundred eighty three nine hundred seventeen", "483917", "ok"),
    ("forty eight three nine one seven", "483917", "ok"),
    ("one oh two three four five", "102345", "ok"),
    ("483 927", "483917", "ok"),       # one digit wrong: within one edit
    ("483 97", "483917", "ok"),        # one digit dropped
    ("483 9117", "483917", "ok"),      # one digit added
    ("483 937 000", "483917", "ok"),   # trailing digits after a one-off code are free
    ("482 927", "483917", "missing"),  # two digits wrong
    ("483", "483917", "missing"),
    ("four eighty-two, nine seventeen", "482917", "ok"),
    ("four hundred and eighty two nine hundred seventeen", "482917", "ok"),
    ("oh four double five six one", "045561", "ok"),
    ("for eight to nine one seven", "482917", "ok"),           # ASR homophones between digits
    ("code is for eight two nine one seven", "482917", "ok"),  # a leading "for" is a word, the code is 8 digits of speech

    ("hello there", "483917", "missing"),
    ("oh well", "483917", "missing"),  # "oh" alone is an interjection, not a zero
])
def test_code_check(said: str, code: str, expected: str) -> None:
    assert asr_mod.check_code(code, said) == expected


def test_the_code_edit_budget_is_one() -> None:
    assert asr_mod.CODE_MAX_EDITS == 1
    assert asr_mod.code_distance("483917", "483 917") == 0
    assert asr_mod.code_distance("483917", "483 927") == 1
    assert asr_mod.code_distance("483917", "121 212") >= 4          # another code is never close


def test_the_spike_self_test_cases() -> None:
    """The cases scripts/g5_calibrate.py selftest pins, against the rule the app now runs."""
    S = "Please call me back when the weather clears, and bring the blue folder from the kitchen table."
    cases = [
        (S + " My code is 4 8 2 9 1 7.", "482917", True),
        (S + " 482 917", "482917", True),
        (S + " four hundred eighty two nine hundred seventeen", "482917", True),
        (S.replace("blue", "glue") + " 482917", "482917", True),      # one ASR slip
        (S + " 482 918", "482917", True),                                 # one digit wrong
        (S + " 481 918", "482917", False),                                # two digits wrong
        (S, "482917", False),                                             # no code
        ("Four eight two nine one seven.", "482917", False),              # code only
        (consent_mod.SENTENCES[0] + " 482917", "482917", False),          # another sentence
        ("Please call me back when the weather clears. 482917", "482917", False),   # half the sentence
    ]
    for text, code, want in cases:
        got = asr_mod.check_transcript(S, code, text)
        ok = got.phrase_score >= Thresholds.phrase_ok and got.code == "ok"
        assert ok is want, (text, got)


def test_phrase_score_tolerates_small_asr_errors_but_not_missing_words() -> None:
    assert asr_mod.phrase_score(SENTENCE, SENTENCE.upper() + " 4 8 3 9 1 7") == 1.0
    assert asr_mod.phrase_score(SENTENCE, SENTENCE.replace("harbor", "harbour")) == 1.0        # spelling variant snaps
    assert asr_mod.phrase_score("I am happy today", "I'm happy today") == 1.0                  # contraction
    assert 0.8 < asr_mod.phrase_score(SENTENCE, SENTENCE.replace("lazy dog", "lazy")) < 1.0
    assert asr_mod.phrase_score(SENTENCE, "Hello, how are you?") < 0.2
    assert asr_mod.phrase_score(SENTENCE, "") == 0.0


def test_no_sentence_needs_numerals_which_asr_would_write_as_digits() -> None:
    assert not any(any(ch.isdigit() for ch in s) for s in consent_mod.SENTENCES)


# ------------------------------------------------------------------ decide(): every branch

T = Thresholds()   # the spike's bars: accept 0.70, reject 0.10, sentence 0.80 / 0.50, speech 6 s / 8 s


@pytest.mark.parametrize("kw, decision, reason", [
    (dict(phrase=1.0, code="ok", reference=None, account=None), ACCEPTED, consent_mod.REASON_ACCEPTED),
    (dict(phrase=0.9, code="ok", reference=0.9, account=0.8), ACCEPTED, consent_mod.REASON_ACCEPTED),
    (dict(phrase=0.80, code="ok", reference=0.70, account=0.70), ACCEPTED, consent_mod.REASON_ACCEPTED),  # at the bars
    (dict(phrase=0.3, code="ok", reference=0.9, account=0.9), REJECTED, consent_mod.REASON_PHRASE),
    (dict(phrase=0.49, code="ok", reference=None, account=None), REJECTED, consent_mod.REASON_PHRASE),
    (dict(phrase=1.0, code="missing", reference=0.9, account=0.9), REJECTED, consent_mod.REASON_CODE),
    (dict(phrase=1.0, code="ok", reference=0.09, account=0.9), REJECTED, consent_mod.REASON_REFERENCE_MISMATCH),
    (dict(phrase=1.0, code="ok", reference=0.9, account=0.09), REJECTED, consent_mod.REASON_ACCOUNT_MISMATCH),
    (dict(phrase=1.0, code="ok", reference=None, account=-0.1), REJECTED, consent_mod.REASON_ACCOUNT_MISMATCH),
    (dict(phrase=1.0, code="ok", reference=0.9, account=0.9, consent_speech=5.9), REJECTED, consent_mod.REASON_SPEECH),
    (dict(phrase=1.0, code="ok", reference=None, account=None, reference_speech=7.9), REJECTED,
     consent_mod.REASON_REFERENCE_SPEECH),
    (dict(phrase=1.0, code="ok", reference=0.9, account=0.9, consent_speech=6.0, reference_speech=8.0), ACCEPTED,
     consent_mod.REASON_ACCEPTED),                                                           # exactly the minimum
    (dict(phrase=0.7, code="ok", reference=0.9, account=0.9), REVIEW, consent_mod.REASON_ASR_UNSURE),
    (dict(phrase=1.0, code="ok", reference=0.10, account=0.9), REVIEW, consent_mod.REASON_SPEAKER_UNSURE),  # at the refuse line
    (dict(phrase=1.0, code="ok", reference=0.5, account=0.9), REVIEW, consent_mod.REASON_SPEAKER_UNSURE),
    (dict(phrase=1.0, code="ok", reference=0.9, account=0.5), REVIEW, consent_mod.REASON_SPEAKER_UNSURE),
    (dict(phrase=1.0, code="ok", reference=None, account=None, model_changed=True), REVIEW, consent_mod.REASON_MODEL_CHANGED),
    (dict(phrase=1.0, code="ok", reference=0.9, account=None, first_voice=True), REVIEW, consent_mod.REASON_FIRST_VOICE),
    (dict(phrase=1.0, code="ok", reference=None, account=None, first_voice=True), REVIEW, consent_mod.REASON_FIRST_VOICE),
    (dict(phrase=1.0, code="missing", reference=None, account=None, first_voice=True), REJECTED, consent_mod.REASON_CODE),
    (dict(phrase=1.0, code="ok", reference=0.05, account=None, first_voice=True), REJECTED,
     consent_mod.REASON_REFERENCE_MISMATCH),                                         # refusals still refuse
])

def test_decide(kw, decision, reason) -> None:
    got = decide(t=T, **kw)
    assert (got.decision, got.reason) == (decision, reason)


def test_first_voice_rule_can_be_switched_off() -> None:
    t = Thresholds(review_first_voice=False)
    assert decide(phrase=1.0, code="ok", reference=None, account=None, first_voice=True, t=t).decision == ACCEPTED


def test_rejection_reasons_are_plain_and_never_accusatory() -> None:
    for text in (consent_mod.REASON_PHRASE, consent_mod.REASON_CODE, consent_mod.REASON_REFERENCE_MISMATCH,
                 consent_mod.REASON_ACCOUNT_MISMATCH, consent_mod.REASON_ASR_UNSURE, consent_mod.REASON_SPEAKER_UNSURE,
                 consent_mod.REASON_SPEECH, consent_mod.REASON_FIRST_VOICE, consent_mod.REASON_REFERENCE_SPEECH, consent_mod.REASON_CHALLENGE_USED,
                 consent_mod.REASON_VOICE_UNCHECKED):
        assert not any(w in text.lower() for w in ("fake", "fraud", "cheat", "impersonat", "stole", "suspicious", "liar"))


def test_thresholds_default_to_the_spikes_numbers_and_come_from_env(monkeypatch) -> None:
    for var in ("VOX_CONSENT_ACCEPT", "VOX_CONSENT_REJECT", "VOX_CONSENT_PHRASE_OK", "VOX_CONSENT_PHRASE_REVIEW",
                "VOX_CONSENT_MIN_SPEECH", "VOX_CONSENT_MIN_REFERENCE_SPEECH", "VOX_CONSENT_REVIEW_FIRST"):
        monkeypatch.delenv(var, raising=False)
    # TitaNet-large raw cosine: accept 0.70, refuse below 0.10; sentence 0.80 / 0.50; 6 s / 8 s of speech;
    # a hosted account's first voice goes to a person
    assert Thresholds.from_env() == Thresholds(0.70, 0.10, 0.80, 0.50, 6.0, 8.0, True)
    monkeypatch.setenv("VOX_CONSENT_REVIEW_FIRST", "off")
    assert Thresholds.from_env().review_first_voice is False
    monkeypatch.setenv("VOX_CONSENT_ACCEPT", "0.75")
    monkeypatch.setenv("VOX_CONSENT_REJECT", "0.2")
    monkeypatch.setenv("VOX_CONSENT_PHRASE_OK", "not-a-number")
    monkeypatch.setenv("VOX_CONSENT_MIN_SPEECH", "4")
    t = Thresholds.from_env()
    assert (t.accept, t.reject, t.phrase_ok, t.min_speech) == (0.75, 0.2, 0.80, 4.0)


# ------------------------------------------------------------------ principal

def test_principal_is_open_when_auth_is_off(monkeypatch) -> None:
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    assert principal_for(Headers({"x-api-key": "whatever"})) == "open"
    assert principal_for(Headers({})) == "open"


def test_principal_is_the_first_16_hex_of_sha256_of_the_presented_key(monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "secret-key")
    expected = hashlib.sha256(b"secret-key").hexdigest()[:16]
    assert len(expected) == 16
    assert principal_for(Headers({"x-api-key": "secret-key"})) == expected
    assert principal_for(Headers({"authorization": "Bearer secret-key"})) == expected
    with pytest.raises(HTTPException) as bad:      # a key that fails the check is no identity at all
        principal_for(Headers({"x-api-key": "another"}))
    assert bad.value.status_code == 401


def test_the_identity_comes_only_from_the_header_that_passed_the_check(monkeypatch) -> None:
    """The real key in X-API-Key plus made-up text in Authorization must not become the made-up identity."""
    monkeypatch.setenv("VOX_API_KEY", "secret-key")
    root = hashlib.sha256(b"secret-key").hexdigest()[:16]
    assert principal_for(Headers({"x-api-key": "secret-key", "authorization": "Bearer made-up"})) == root
    assert principal_for(Headers({"authorization": "Bearer secret-key", "x-api-key": "made-up"})) == root
    assert auth_mod.root_principal() == root
    with pytest.raises(HTTPException):
        principal_for(Headers({"x-api-key": "wrong", "authorization": "Bearer also-wrong"}))
    with pytest.raises(HTTPException):
        principal_for(Headers({}))


def test_non_ascii_header_text_is_a_plain_mismatch_not_a_crash(monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "secret-key")
    assert auth_mod.headers_have_valid_api_key(Headers({"x-api-key": "sécret"}), "secret-key") is False


def test_challenge_store_binds_a_challenge_to_its_principal() -> None:
    store = consent_mod.ChallengeStore()
    c = store.issue(now=0.0, principal="alice")
    with pytest.raises(consent_mod.ChallengeError) as exc:
        store.check(c.challenge_id, now=1.0, principal="bob")
    assert exc.value.kind == "unknown"
    assert store.check(c.challenge_id, now=1.0, principal="alice").sentence == c.sentence


# ------------------------------------------------------------------ helpers for the route tests

def _challenge(env) -> dict:  # noqa: F811
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.say(ch)
    return ch


def _own(env, ch: dict, make_wav, *, name="my-voice", hz=220.0, seconds=9.0, reference=None, ref_hz=220.0,
         ref_seconds=12.0, headers=None):
    files = {"audio": ("rec.wav", make_wav(seconds=seconds, hz=hz), "audio/wav")}
    if reference:
        files["reference"] = ("long.wav", make_wav(seconds=ref_seconds, hz=ref_hz), "audio/wav")
    return env.client.post(
        "/voices/own", headers=headers or {},
        data={"challenge_id": ch["challenge_id"], "name": name, "display_name": name.title()}, files=files)


def _records(env):
    return list(env.store.records.values())


def _speak(env, voice: str):
    return env.client.post("/synthesize-url", json={"text": "hello", "voice": voice})


# ------------------------------------------------------------------ accepted / rejected / review (hosted)

def test_first_accepted_own_voice_fixes_the_account_voiceprint(env, make_wav) -> None:
    ch = _challenge(env)
    r = _own(env, ch, make_wav)
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["own", "consent-verified"]
    vp = env.store.voiceprints["open"]
    assert vp.embedding_model == "fake-titanet" and vp.source_record == _records(env)[0].id
    rec = _records(env)[0]
    assert rec.speaker_score_account is None and rec.speaker_score_reference is None  # nothing to compare yet
    assert rec.terms_version == "draft-1" and rec.edition == "hosted" and rec.embedding_model == "fake-titanet"
    assert rec.transcript.startswith(SENTENCE[:10]) or rec.transcript


def test_second_voice_from_the_same_person_is_accepted_and_scored_against_the_voiceprint(env, make_wav) -> None:
    assert _own(env, _challenge(env), make_wav, name="first").status_code == 200
    r = _own(env, _challenge(env), make_wav, name="second")
    assert r.status_code == 200, r.text
    second = [x for x in _records(env) if x.voice == "second"][0]
    assert second.speaker_score_account == pytest.approx(1.0, abs=1e-5)
    assert second.decision == "accepted"


def test_second_voice_from_a_different_person_is_refused_plainly(env, make_wav) -> None:
    env.embedder.speakers[440] = unit(0.02)
    assert _own(env, _challenge(env), make_wav, name="first").status_code == 200
    r = _own(env, _challenge(env), make_wav, name="second", hz=440.0)
    assert r.status_code == 422
    assert r.json()["detail"] == consent_mod.REASON_ACCOUNT_MISMATCH
    assert r.headers["x-vox-error"] == "consent_rejected"
    assert "second" not in env.repo.voices and not (env.dir / "second.wav").exists()
    rec = [x for x in _records(env) if x.voice == "second"][0]
    assert rec.decision == "rejected" and rec.speaker_score_account == pytest.approx(0.02, abs=1e-5)
    assert (env.dir / "_consent" / rec.consent_audio_path).is_file()   # audio kept for the record


def test_a_score_between_the_bars_goes_to_review_and_the_voice_cannot_speak(env, make_wav) -> None:
    env.embedder.speakers[330] = unit(0.52)
    assert _own(env, _challenge(env), make_wav, name="first").status_code == 200
    r = _own(env, _challenge(env), make_wav, name="second", hz=330.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == ["own", "consent-pending"] and body["consent"] == "pending"
    assert body["consent_result"]["decision"] == "review" and body["consent_result"]["usable"] is False
    assert (env.dir / "second.wav").is_file()                           # stored, but not usable
    blocked = _speak(env, "second")
    assert blocked.status_code == 403
    assert "waiting for a consent check" in blocked.json()["detail"]
    assert blocked.headers["x-vox-error"] == "consent_pending"
    assert _speak(env, "first").status_code == 200                      # the verified one still speaks


def test_wrong_sentence_is_rejected_and_nothing_but_the_record_is_stored(env, make_wav) -> None:
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.say(ch, sentence=False)
    r = _own(env, ch, make_wav)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_PHRASE
    assert "my-voice" not in env.repo.voices and not (env.dir / "my-voice.wav").exists()
    rec = _records(env)[0]
    assert rec.decision == "rejected" and rec.code_ok is True and rec.transcript == env.asr.text
    assert "open" not in env.store.voiceprints
    # the attempt used the challenge up: a new sentence is needed
    assert _own(env, ch, make_wav).status_code == 410


def test_missing_code_is_rejected(env, make_wav) -> None:
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.say(ch, code=False)
    r = _own(env, ch, make_wav)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_CODE
    assert _records(env)[0].code_ok is False


def _wrong_code(code: str, digits: int) -> str:
    """``code`` with its last ``digits`` digits changed, by exactly ``digits`` edits as the gate measures them.

    A blind "+1" is not enough: where a changed digit lands on its neighbour (code 45 -> 56) the gate's
    substring edit distance sees one deletion, not two substitutions, and the test flaked on random codes."""
    from app.asr import code_distance

    for shift in range(1, 10):
        flipped = "".join(str((int(d) + shift) % 10) for d in code[len(code) - digits:])
        candidate = code[:len(code) - digits] + flipped
        if code_distance(code, candidate) == digits:
            return candidate
    raise AssertionError(f"no {digits}-edit variant of {code}")


def test_a_code_one_digit_off_is_accepted_the_spikes_one_edit_rule(env, make_wav) -> None:
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.text = f"{ch['sentence']} {' '.join(_wrong_code(ch['code'], 1))}"
    r = _own(env, ch, make_wav)
    assert r.status_code == 200 and r.json()["tags"] == ["own", "consent-verified"]
    assert _records(env)[0].decision == "accepted" and _records(env)[0].code_ok is True


def test_a_code_two_digits_off_is_rejected(env, make_wav) -> None:
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.text = f"{ch['sentence']} {' '.join(_wrong_code(ch['code'], 2))}"
    r = _own(env, ch, make_wav)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_CODE
    assert _records(env)[0].decision == "rejected" and _records(env)[0].code_ok is False


def _mostly_heard(env, ch: dict) -> None:
    """Script the ASR so a quarter of the sentence is lost: a review, not a refusal."""
    words = ch["sentence"].split()
    env.asr.text = " ".join(words[: int(len(words) * 0.75)]) + " " + " ".join(ch["code"])


def test_a_sentence_that_was_mostly_heard_goes_to_review(env, make_wav) -> None:
    ch = env.client.post("/voices/consent/challenge").json()
    _mostly_heard(env, ch)
    r = _own(env, ch, make_wav)
    assert r.status_code == 200, r.text
    assert r.json()["consent_result"]["decision"] == "review"
    assert 0.5 <= _records(env)[0].phrase_score < 0.8


# ------------------------------------------------------------------ the optional longer sample

def test_a_longer_sample_becomes_the_voice_and_is_matched_against_the_consent_clip(env, make_wav) -> None:
    r = _own(env, _challenge(env), make_wav, reference=True, ref_seconds=14.0)
    assert r.status_code == 200, r.text
    assert r.json()["duration_s"] == pytest.approx(14.0, abs=0.2)       # made from the longer sample
    rec = _records(env)[0]
    assert rec.speaker_score_reference == pytest.approx(1.0, abs=1e-5)
    assert rec.reference_audio_path and (env.dir / "_consent" / rec.reference_audio_path).is_file()


def test_a_longer_sample_by_someone_else_is_refused(env, make_wav) -> None:
    env.embedder.speakers[440] = unit(0.02)
    r = _own(env, _challenge(env), make_wav, reference=True, ref_hz=440.0)
    assert r.status_code == 422 and r.json()["detail"] == consent_mod.REASON_REFERENCE_MISMATCH
    assert "my-voice" not in env.repo.voices


def test_a_longer_sample_must_be_at_least_10_seconds(env, make_wav) -> None:
    ch = _challenge(env)
    r = _own(env, ch, make_wav, reference=True, ref_seconds=5.0)
    assert r.status_code == 422 and "longer sample" in r.json()["detail"]
    assert _records(env) == []                                           # refused before the gate: challenge unspent
    assert _own(env, ch, make_wav).status_code == 200


def test_the_account_check_covers_the_longer_sample_too(env, make_wav) -> None:
    """Consent clip matches the account, the longer sample (what becomes the voice) does not: worst score counts."""
    env.embedder.speakers[330] = unit(0.52)
    assert _own(env, _challenge(env), make_wav, name="first").status_code == 200
    r = _own(env, _challenge(env), make_wav, name="second", reference=True, ref_hz=330.0)
    rec = [x for x in _records(env) if x.voice == "second"][0]
    assert rec.speaker_score_account == pytest.approx(0.52, abs=1e-5)
    assert rec.speaker_score_reference == pytest.approx(0.52, abs=1e-5)
    assert r.status_code == 200 and r.json()["consent"] == "pending"


# ------------------------------------------------------------------ models unavailable, principals

def test_models_unavailable_is_503_in_hosted_and_does_not_burn_the_challenge(env, make_wav) -> None:
    env.asr.fail = True
    ch = _challenge(env)
    r = _own(env, ch, make_wav)
    assert r.status_code == 503 and r.json()["detail"] == "consent check unavailable"
    assert "my-voice" not in env.repo.voices and _records(env) == []
    env.asr.fail = False
    assert _own(env, ch, make_wav).status_code == 200                    # same challenge works once models load


def test_models_that_load_but_crash_are_also_503_and_the_challenge_survives(env, make_wav, monkeypatch) -> None:
    ch = _challenge(env)
    real = env.asr.transcribe

    def boom(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.asr, "transcribe", boom)
    assert _own(env, ch, make_wav).status_code == 503
    monkeypatch.setattr(env.asr, "transcribe", real)
    assert _own(env, ch, make_wav).status_code == 200


def test_no_gate_at_all_is_503(env, monkeypatch) -> None:
    monkeypatch.setattr(main, "_consent_gate", None)
    assert env.client.post("/voices/consent/challenge").status_code == 503


def test_a_challenge_belongs_to_the_key_that_asked_for_it(env, make_wav, monkeypatch) -> None:
    ch = _challenge(env)                      # issued while auth is off: principal "open"
    monkeypatch.setenv("VOX_API_KEY", "k1")
    r = _own(env, ch, make_wav, headers={"X-API-Key": "k1"})
    assert r.status_code == 404 and r.headers["x-vox-error"] == "challenge_unknown"


def test_the_voiceprint_is_stored_under_the_hashed_key(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "k1")
    h = {"X-API-Key": "k1"}
    ch = env.client.post("/voices/consent/challenge", headers=h).json()
    env.asr.say(ch)
    assert _own(env, ch, make_wav, headers=h).status_code == 200
    assert list(env.store.voiceprints) == [hashlib.sha256(b"k1").hexdigest()[:16]]
    assert _records(env)[0].principal == hashlib.sha256(b"k1").hexdigest()[:16]


# ------------------------------------------------------------------ the review queue

def _review_env(env, make_wav, monkeypatch, *, hz=330.0):
    """One verified voice ("first") and one waiting for review ("second")."""
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    env.embedder.speakers[330] = unit(0.52)
    assert _own(env, _challenge(env), make_wav, name="first").status_code == 200
    assert _own(env, _challenge(env), make_wav, name="second", hz=hz).status_code == 200
    return {"X-API-Key": "op-key"}


def test_hosted_first_voice_waits_for_a_person_then_sets_the_voiceprint(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_CONSENT_REVIEW_FIRST", "1")
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    op = {"X-API-Key": "op-key"}
    r = _own(env, _challenge(env), make_wav, name="first")
    assert r.status_code == 200, r.text
    assert consent_mod.public_tags(env.repo.voices["first"].tags) == ["own", "consent-pending"]
    assert _records(env)[0].reason == consent_mod.REASON_FIRST_VOICE
    assert "open" not in env.store.voiceprints and _speak(env, "first").status_code == 403
    item = env.client.get("/consent/reviews", headers=op).json()[0]
    assert env.client.post(f"/consent/reviews/{item['id']}", headers=op, json={"decision": "approve"}).status_code == 200
    assert env.store.voiceprints["open"].source_record == item["id"] and _speak(env, "first").status_code == 200
    # Once a person has vouched for the voiceprint, the same person's later voices pass on their own.
    r = _own(env, _challenge(env), make_wav, name="second")
    assert r.status_code == 200 and consent_mod.public_tags(env.repo.voices["second"].tags) == ["own", "consent-verified"]


def test_hosted_first_voice_still_refuses_a_wrong_code(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_CONSENT_REVIEW_FIRST", "1")
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.say(ch, code=False)
    r = _own(env, ch, make_wav, name="first")
    assert r.status_code == 422 and _records(env)[0].reason == consent_mod.REASON_CODE
    assert "first" not in env.repo.voices


def test_review_routes_refuse_with_503_when_no_operator_key_is_configured(env) -> None:
    assert env.client.get("/consent/reviews").status_code == 503
    r = env.client.post("/consent/reviews/abc", json={"decision": "approve"})
    assert r.status_code == 503 and r.json()["detail"] == "no operator key configured"
    assert env.client.get("/consent/reviews/abc/audio").status_code == 503


def test_review_routes_need_the_operator_key(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    assert env.client.get("/consent/reviews").status_code == 403
    assert env.client.get("/consent/reviews", headers={"X-API-Key": "nope"}).status_code == 403
    assert env.client.get("/consent/reviews", headers=op).status_code == 200


def test_the_review_queue_lists_pending_records_with_what_the_reviewer_needs(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    items = env.client.get("/consent/reviews", headers=op).json()
    assert [i["voice"] for i in items] == ["second"]
    item = items[0]
    assert item["decision"] == "review" and item["reason"] == consent_mod.REASON_SPEAKER_UNSURE
    assert item["speaker_score_account"] == pytest.approx(0.52, abs=1e-5)
    assert item["transcript"] and item["code"] and item["sentence"] and item["principal"] == "open"
    everything = env.client.get("/consent/reviews?status=all", headers=op).json()
    assert {i["voice"] for i in everything} == {"first", "second"}


def test_approving_makes_the_voice_verified_and_usable(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    item = env.client.get("/consent/reviews", headers=op).json()[0]
    assert _speak(env, "second").status_code == 403
    r = env.client.post(f"/consent/reviews/{item['id']}", headers=op, json={"decision": "approve", "note": "sounds like her"})
    assert r.status_code == 200, r.text
    assert (r.json()["decision"], r.json()["reviewer"], r.json()["review_note"]) == ("accepted", "operator", "sounds like her")
    assert r.json()["reviewed_at"]
    assert consent_mod.public_tags(env.repo.voices["second"].tags) == ["own", "consent-verified"]
    assert _speak(env, "second").status_code == 200
    assert env.client.get("/consent/reviews", headers=op).json() == []


def test_rejecting_deletes_the_voice_but_keeps_the_record_and_audio(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    item = env.client.get("/consent/reviews", headers=op).json()[0]
    r = env.client.post(f"/consent/reviews/{item['id']}", headers=op, json={"decision": "reject"})
    assert r.status_code == 200 and r.json()["decision"] == "rejected"
    assert "second" not in env.repo.voices and not (env.dir / "second.wav").exists()
    rec = env.store.records[item["id"]]
    assert (env.dir / "_consent" / rec.consent_audio_path).is_file()


def test_a_review_can_be_resolved_once_and_unknown_ids_404(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    item = env.client.get("/consent/reviews", headers=op).json()[0]
    url = f"/consent/reviews/{item['id']}"
    assert env.client.post(url, headers=op, json={"decision": "approve"}).status_code == 200
    assert env.client.post(url, headers=op, json={"decision": "reject"}).status_code == 409
    assert env.client.post("/consent/reviews/nope", headers=op, json={"decision": "approve"}).status_code == 404
    assert env.client.post(url, headers=op, json={"decision": "maybe"}).status_code == 422


def test_approving_the_first_ever_review_sets_the_account_voiceprint(env, make_wav, monkeypatch) -> None:
    """A first voice that goes to review (a quarter of the sentence unheard) fixes the voiceprint only once approved."""
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    op = {"X-API-Key": "op-key"}
    ch = env.client.post("/voices/consent/challenge").json()
    _mostly_heard(env, ch)
    assert _own(env, ch, make_wav).status_code == 200
    assert env.store.voiceprints == {}
    item = env.client.get("/consent/reviews", headers=op).json()[0]
    assert env.client.post(f"/consent/reviews/{item['id']}", headers=op, json={"decision": "approve"}).status_code == 200
    assert "open" in env.store.voiceprints


def test_the_reviewer_can_fetch_the_audio(env, make_wav, monkeypatch) -> None:
    op = _review_env(env, make_wav, monkeypatch)
    item = env.client.get("/consent/reviews", headers=op).json()[0]
    r = env.client.get(f"/consent/reviews/{item['id']}/audio", headers=op)
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert env.client.get(f"/consent/reviews/{item['id']}/audio?which=reference", headers=op).status_code == 404


def test_the_operator_key_gets_through_the_key_gate_on_review_routes_only(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "main-key")
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    op, main_key = {"X-API-Key": "op-key"}, {"X-API-Key": "main-key"}
    assert env.client.get("/consent/reviews", headers=op).status_code == 200
    assert env.client.get("/voices", headers=op).status_code == 401                       # not a master key
    assert env.client.get("/consent/reviews", headers=main_key).status_code == 403         # the main key is not enough
    assert env.client.get("/consent/reviews").status_code == 401


# ------------------------------------------------------------------ synthesis guard for every route that speaks

def test_a_pending_voice_is_refused_on_the_openai_route_too(env) -> None:
    env.repo.voices["waiting"] = Voice(name="waiting", display_name="W", wav_path="house-f1.wav", duration_s=5.0,
                                       prompt_text=None, tags=["own", "consent-pending"])
    r = env.client.post("/v1/audio/speech", json={"model": "tts-1", "input": "hi", "voice": "waiting"})
    assert r.status_code == 403
    assert "consent check" in r.json()["error"]["message"]


def test_legacy_unverified_voices_are_blocked_in_hosted_and_allowed_in_self(env, monkeypatch) -> None:
    env.repo.voices["old"] = Voice(name="old", display_name="Old", wav_path="house-f1.wav", duration_s=10.0,
                                   prompt_text=None, tags=["own", "unverified-consent"])
    assert _speak(env, "old").status_code == 403
    monkeypatch.setenv("VOX_EDITION", "self")
    assert _speak(env, "old").status_code == 200


def test_fleet_voices_and_plain_uploads_are_untouched_by_the_gate(env, make_wav) -> None:
    assert _speak(env, "house-f1").status_code == 200
    up = env.client.post("/voices", data={"name": "fleet", "display_name": "Fleet"},
                         files={"audio": ("c.wav", make_wav(seconds=5.0), "audio/wav")})
    assert up.status_code == 200 and up.json()["tags"] == [] and up.json()["consent"] is None
    assert _records(env) == []


def test_synthesis_block_reason_and_consent_state() -> None:
    assert consent_mod.synthesis_block_reason(["own", "consent-pending"], "self")
    assert consent_mod.synthesis_block_reason(["own", "consent-verified"], "hosted") is None
    assert consent_mod.synthesis_block_reason(["own", "consent-unchecked-self"], "self") is None
    assert consent_mod.synthesis_block_reason(["own", "consent-unchecked-self"], "hosted") == consent_mod.REASON_VOICE_UNCHECKED
    assert consent_mod.synthesis_block_reason(["house"], "hosted") is None
    assert consent_mod.consent_state(["own", "consent-verified"]) == "verified"
    assert consent_mod.consent_state(["unverified-consent"]) == "pending"
    assert consent_mod.consent_state(["consent-unchecked-self"], "self") == "unchecked"
    assert consent_mod.consent_state(["consent-unchecked-self"], "hosted") == "pending"   # hosted will not speak with it
    assert consent_mod.public_tags(["own", "consent-verified", "consent-record:abc"]) == ["own", "consent-verified"]
    assert consent_mod.record_id_of(["own", "consent-record:abc"]) == "abc"
    assert consent_mod.record_id_of(["own"]) is None
    assert consent_mod.consent_state(["house"]) is None


# ------------------------------------------------------------------ self edition: checks run, failures only warn

def test_self_edition_records_the_check_but_a_failure_only_warns(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_EDITION", "self")
    ch = env.client.post("/voices/consent/challenge").json()
    env.asr.say(ch, sentence=False)                       # would be a hosted rejection
    r = _own(env, ch, make_wav)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tags"] == ["own", "consent-unchecked-self"]
    assert body["consent_result"]["warning"] == consent_mod.REASON_PHRASE and body["consent_result"]["usable"] is True
    assert _records(env)[0].decision == "rejected" and _records(env)[0].edition == "self"   # recorded as it was
    assert "open" not in env.store.voiceprints            # a failed check never sets the voiceprint
    assert _speak(env, "my-voice").status_code == 200


def test_self_edition_a_passing_check_is_verified_like_hosted(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_EDITION", "self")
    r = _own(env, _challenge(env), make_wav)
    assert r.json()["tags"] == ["own", "consent-verified"] and r.json()["consent_result"]["warning"] is None


def test_self_edition_still_makes_the_voice_when_the_models_cannot_load(env, make_wav, monkeypatch) -> None:
    monkeypatch.setenv("VOX_EDITION", "self")
    env.asr.fail = True
    r = _own(env, _challenge(env), make_wav)
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == ["own", "consent-unchecked-self"]
    assert _records(env)[0].decision == "unchecked" and _records(env)[0].transcript is None


# ------------------------------------------------------------------ v1 voices

@pytest.mark.parametrize("edition, expected", [("hosted", "consent-pending"), ("self", "consent-unchecked-self")])
def test_v1_unverified_voices_are_retagged_at_startup(env, monkeypatch, edition, expected) -> None:
    import asyncio

    class RetagRepo:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def retag(self, old, new) -> int:
            self.calls.append((old, new))
            return 2

    repo = RetagRepo()
    monkeypatch.setattr(main, "_repo", repo)
    monkeypatch.setenv("VOX_EDITION", edition)
    asyncio.run(main._retag_legacy_consent())
    assert repo.calls == [("unverified-consent", expected)]
