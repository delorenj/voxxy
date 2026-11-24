"""Story G5: PgConsentStore and the VoiceRepo additions against a real postgres.

Skipped unless VOX_TEST_DATABASE_URL points at a THROWAWAY database (this file creates the schema from
init.sql and migrations/*.sql, then empties the consent tables and the voices table between tests; never
point it at the live `vox` database). For example:

    docker run -d --rm --name voxxy-test-pg -e POSTGRES_PASSWORD=x -p 55432:5432 postgres:16-alpine
    VOX_TEST_DATABASE_URL=postgresql://postgres:x@localhost:55432/postgres uv run pytest tests/test_consent_store_pg.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

from app import consent as consent_mod
from app import consent_store
from app.voices import VoiceRepo

DSN = os.environ.get("VOX_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="VOX_TEST_DATABASE_URL not set (needs a throwaway postgres)")
ROOT = Path(__file__).resolve().parents[1]


def run(coro_fn):
    async def inner():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute((ROOT / "init.sql").read_text())
            for sql in sorted((ROOT / "migrations").glob("*.sql")):
                await pool.execute(sql.read_text())
                await pool.execute(sql.read_text())  # migrations are idempotent
            await pool.execute("TRUNCATE consent_challenges, consent_records, account_voiceprints, voices")
            return await coro_fn(pool)
        finally:
            await pool.close()

    return asyncio.run(inner())


def _record(**kw) -> consent_mod.ConsentRecord:
    base = dict(id="r1", voice="v", principal="p", challenge_id="c1", sentence="s", code="123456",
                decision=consent_mod.REVIEW, reason="why", transcript="t", phrase_score=0.9, code_ok=True,
                speaker_score_reference=0.5, speaker_score_account=0.55, terms_version="draft-1",
                consent_audio_path="r1.consent.wav", embedding_model="m", embedding=[0.1, 0.2, 0.3], edition="hosted")
    base.update(kw)
    return consent_mod.ConsentRecord(**base)


def test_challenges_are_issued_checked_redeemed_once_and_bound_to_the_principal() -> None:
    async def scenario(pool):
        store = consent_store.PgConsentStore(pool)
        ch = await store.issue_challenge("alice")
        assert len(ch.code) == 6 and ch.sentence in consent_mod.SENTENCES
        assert (await store.check_challenge(ch.challenge_id, "alice")).sentence == ch.sentence
        with pytest.raises(consent_mod.ChallengeError) as other:
            await store.check_challenge(ch.challenge_id, "bob")
        assert other.value.kind == "unknown"
        redeemed = await store.redeem_challenge(ch.challenge_id, "alice")
        assert (redeemed.sentence, redeemed.code) == (ch.sentence, ch.code)
        for call in (store.redeem_challenge, store.check_challenge):
            with pytest.raises(consent_mod.ChallengeError) as used:
                await call(ch.challenge_id, "alice")
            assert used.value.kind == "used"
        with pytest.raises(consent_mod.ChallengeError) as nope:
            await store.redeem_challenge("missing", "alice")
        assert nope.value.kind == "unknown"

    run(scenario)


def test_an_expired_challenge_cannot_be_used() -> None:
    async def scenario(pool):
        store = consent_store.PgConsentStore(pool, ttl_seconds=-5)
        ch = await store.issue_challenge("alice")
        for call in (store.check_challenge, store.redeem_challenge):
            with pytest.raises(consent_mod.ChallengeError) as exc:
                await call(ch.challenge_id, "alice")
            assert exc.value.kind == "expired"

    run(scenario)


def test_two_racing_redemptions_succeed_exactly_once() -> None:
    async def scenario(pool):
        store = consent_store.PgConsentStore(pool)
        ch = await store.issue_challenge("alice")
        results = await asyncio.gather(*[store.redeem_challenge(ch.challenge_id, "alice") for _ in range(4)],
                                       return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 1

    run(scenario)


def test_records_round_trip_and_the_review_queue_resolves_once() -> None:
    async def scenario(pool):
        store = consent_store.PgConsentStore(pool)
        await store.add_record(_record())
        await store.add_record(_record(id="r2", decision=consent_mod.ACCEPTED))
        got = await store.get_record("r1")
        assert got.embedding == pytest.approx([0.1, 0.2, 0.3]) and got.phrase_score == pytest.approx(0.9)
        assert got.speaker_score_account == pytest.approx(0.55) and got.code_ok is True
        assert [r.id for r in await store.list_records("review")] == ["r1"]
        assert {r.id for r in await store.list_records()} == {"r1", "r2"}
        done = await store.resolve_review("r1", decision=consent_mod.ACCEPTED, reviewer="operator", note="ok")
        assert (done.decision, done.reviewer, done.review_note) == ("accepted", "operator", "ok") and done.reviewed_at
        assert await store.list_records("review") == []
        with pytest.raises(consent_mod.ReviewError) as again:
            await store.resolve_review("r1", decision=consent_mod.REJECTED, reviewer="operator", note=None)
        assert again.value.kind == "resolved"
        with pytest.raises(consent_mod.ReviewError) as unknown:
            await store.resolve_review("zzz", decision=consent_mod.REJECTED, reviewer="operator", note=None)
        assert unknown.value.kind == "unknown"
        with pytest.raises(asyncpg.CheckViolationError):
            await store.add_record(_record(id="r3", decision="maybe"))

    run(scenario)


def test_superseding_reviews_and_the_newest_view() -> None:
    async def scenario(pool):
        store = consent_store.PgConsentStore(pool)
        await store.add_record(_record(id="old"))
        await store.add_record(_record(id="other-voice", voice="elsewhere"))
        await store.add_record(_record(id="kept", decision=consent_mod.ACCEPTED))
        await store.add_record(_record(id="new"))
        assert await store.supersede_reviews(voice="v", keep="new") == 1
        old = await store.get_record("old")
        assert (old.decision, old.reviewer) == ("rejected", "system") and "superseded" in old.review_note
        assert (await store.get_record("new")).decision == "review"
        assert (await store.get_record("other-voice")).decision == "review"      # another voice: untouched
        assert (await store.get_record("kept")).decision == "accepted"
        assert await store.supersede_reviews(voice="v", keep="new") == 0           # idempotent
        assert [r.id for r in await store.list_records(limit=2)] == ["old", "other-voice"]
        assert [r.id for r in await store.list_records(limit=2, newest=True)] == ["kept", "new"]
        assert [r.id for r in await store.list_records("review", limit=5, newest=True)] == ["other-voice", "new"]

    run(scenario)


def test_the_first_voiceprint_wins() -> None:
    async def scenario(pool):
        store = consent_store.PgConsentStore(pool)
        assert await store.get_voiceprint("p") is None
        assert await store.set_voiceprint_if_absent(consent_mod.AccountVoiceprint("p", [1.0, 0.0], "m", "r1")) is True
        assert await store.set_voiceprint_if_absent(consent_mod.AccountVoiceprint("p", [0.0, 1.0], "m2", "r2")) is False
        vp = await store.get_voiceprint("p")
        assert (vp.embedding, vp.embedding_model, vp.source_record) == ([1.0, 0.0], "m", "r1")

    run(scenario)


def test_voice_repo_retags_v1_voices_and_sets_tags() -> None:
    async def scenario(pool):
        repo = VoiceRepo(pool)
        for name, tags in (("a", ["own", "unverified-consent"]), ("b", ["own", "unverified-consent"]), ("c", ["house"])):
            await repo.upsert(name=name, display_name=name, wav_path=f"{name}.wav", duration_s=1.0, tags=tags)
        assert await repo.retag("unverified-consent", "consent-pending") == 2
        assert (await repo.get("a")).tags == ["own", "consent-pending"]
        assert (await repo.get("c")).tags == ["house"]
        assert await repo.retag("unverified-consent", "consent-pending") == 0   # idempotent
        assert (await repo.set_tags("a", ["own", "consent-verified"])).tags == ["own", "consent-verified"]
        assert await repo.set_tags("missing", []) is None

    run(scenario)
