"""Where the consent gate keeps its state: challenges, consent records, account voiceprints.

``PgConsentStore`` is production (migrations/004_consent.sql). ``MemoryConsentStore`` is the fallback the
tests use so they need no database; it implements the same ``ConsentStore`` interface and the same rules.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app import consent as c


def _ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _epoch(value: datetime) -> float:
    return value.timestamp()


SUPERSEDED_BY_SYSTEM = "system"


def _mark_superseded(record: c.ConsentRecord, keep: str) -> None:
    record.decision = c.REJECTED
    record.reviewer = SUPERSEDED_BY_SYSTEM
    record.review_note = f"superseded: a newer recording ({keep}) replaced this voice before anyone reviewed it"
    record.reviewed_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------- memory


class MemoryConsentStore:
    def __init__(self, ttl_seconds: float = c.CHALLENGE_TTL_SECONDS) -> None:
        self.challenges = c.ChallengeStore(ttl_seconds=ttl_seconds)
        self.records: dict[str, c.ConsentRecord] = {}
        self.voiceprints: dict[str, c.AccountVoiceprint] = {}
        self._lock = asyncio.Lock()

    async def issue_challenge(self, principal: str) -> c.Challenge:
        return self.challenges.issue(principal=principal)

    async def check_challenge(self, challenge_id: str, principal: str) -> c.Challenge:
        return self.challenges.check(challenge_id, principal=principal)

    async def redeem_challenge(self, challenge_id: str, principal: str) -> c.Redeemed:
        return self.challenges.redeem(challenge_id, principal=principal)

    async def add_record(self, record: c.ConsentRecord) -> None:
        self.records[record.id] = record

    async def get_record(self, record_id: str) -> Optional[c.ConsentRecord]:
        return self.records.get(record_id)

    async def list_records(self, decision: Optional[str] = None, limit: int = 200,
                           newest: bool = False) -> list[c.ConsentRecord]:
        """Oldest first (the review queue is first in, first out); ``newest`` keeps the latest ``limit``
        instead, still in chronological order."""
        rows = sorted((r for r in self.records.values() if decision is None or r.decision == decision),
                      key=lambda r: r.created_at)
        return rows[-limit:] if newest else rows[:limit]

    async def supersede_reviews(self, *, voice: str, keep: str) -> int:
        async with self._lock:
            done = 0
            for r in self.records.values():
                if r.voice == voice and r.id != keep and r.decision == c.REVIEW:
                    _mark_superseded(r, keep)
                    done += 1
            return done

    async def resolve_review(self, record_id: str, *, decision: str, reviewer: str, note: Optional[str]) -> c.ConsentRecord:
        async with self._lock:
            record = self.records.get(record_id)
            if record is None:
                raise c.ReviewError("unknown", "No such consent record.")
            if record.decision != c.REVIEW:
                raise c.ReviewError("resolved", f"That record is not waiting for review (it is {record.decision}).")
            record.decision = decision
            record.reviewer = reviewer
            record.review_note = note
            record.reviewed_at = datetime.now(timezone.utc)
            return record

    async def get_voiceprint(self, principal: str) -> Optional[c.AccountVoiceprint]:
        return self.voiceprints.get(principal)

    async def last_record(self, principal: str) -> Optional[c.ConsentRecord]:
        mine = [r for r in self.records.values() if r.principal == principal]
        return max(mine, key=lambda r: r.created_at) if mine else None

    async def set_voiceprint_if_absent(self, voiceprint: c.AccountVoiceprint) -> bool:
        if voiceprint.principal in self.voiceprints:
            return False
        self.voiceprints[voiceprint.principal] = voiceprint
        return True


# ---------------------------------------------------------------- postgres

_RECORD_COLUMNS = """id, voice, principal, challenge_id, sentence, code, transcript, phrase_score, code_ok,
    speaker_score_reference, speaker_score_account, decision, reason, terms_version, consent_audio_path,
    reference_audio_path, embedding_model, embedding, edition, created_at, reviewed_at, reviewer, review_note"""


def _row_to_record(row) -> c.ConsentRecord:
    return c.ConsentRecord(
        id=row["id"], voice=row["voice"], principal=row["principal"], challenge_id=row["challenge_id"],
        sentence=row["sentence"], code=row["code"], transcript=row["transcript"],
        phrase_score=row["phrase_score"], code_ok=row["code_ok"],
        speaker_score_reference=row["speaker_score_reference"],
        speaker_score_account=row["speaker_score_account"], decision=row["decision"], reason=row["reason"],
        terms_version=row["terms_version"], consent_audio_path=row["consent_audio_path"],
        reference_audio_path=row["reference_audio_path"], embedding_model=row["embedding_model"],
        embedding=list(row["embedding"]) if row["embedding"] is not None else None, edition=row["edition"],
        created_at=row["created_at"], reviewed_at=row["reviewed_at"], reviewer=row["reviewer"],
        review_note=row["review_note"],
    )


class PgConsentStore:
    def __init__(self, pool: asyncpg.Pool, ttl_seconds: float = c.CHALLENGE_TTL_SECONDS) -> None:
        self._pool = pool
        self._ttl = ttl_seconds

    # -- challenges

    async def issue_challenge(self, principal: str) -> c.Challenge:
        challenge = c.new_challenge(principal, time.time(), self._ttl)
        # Unredeemed challenges older than a day are noise; redeemed ones stay as the trail behind a record.
        await self._pool.execute(
            "DELETE FROM consent_challenges WHERE redeemed_at IS NULL AND expires_at < now() - interval '1 day'")
        await self._pool.execute(
            "INSERT INTO consent_challenges (id, principal, sentence, code, issued_at, expires_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            challenge.challenge_id, principal, challenge.sentence, challenge.code,
            _ts(challenge.created_at), _ts(challenge.expires_at))
        return challenge

    async def _row(self, challenge_id: str, principal: str):
        row = await self._pool.fetchrow(
            "SELECT id, principal, sentence, code, issued_at, expires_at, redeemed_at "
            "FROM consent_challenges WHERE id = $1", challenge_id)
        if row is None or row["principal"] != principal:
            raise c.challenge_error("unknown")
        return row

    def _usable(self, row, now: float) -> c.Challenge:
        if row["redeemed_at"] is not None:
            raise c.challenge_error("used")
        if now >= _epoch(row["expires_at"]):
            raise c.challenge_error("expired")
        return c.Challenge(
            challenge_id=row["id"], sentence=row["sentence"], code=row["code"],
            created_at=_epoch(row["issued_at"]), expires_at=_epoch(row["expires_at"]), principal=row["principal"])

    async def check_challenge(self, challenge_id: str, principal: str) -> c.Challenge:
        return self._usable(await self._row(challenge_id, principal), time.time())

    async def redeem_challenge(self, challenge_id: str, principal: str) -> c.Redeemed:
        now = time.time()
        row = await self._pool.fetchrow(
            "UPDATE consent_challenges SET redeemed_at = $3 "
            "WHERE id = $1 AND principal = $2 AND redeemed_at IS NULL AND expires_at > $3 "
            "RETURNING id, sentence, code, issued_at",
            challenge_id, principal, _ts(now))
        if row is None:  # work out why, for the right message
            self._usable(await self._row(challenge_id, principal), now)
            raise c.challenge_error("used")
        return c.Redeemed(
            challenge_id=row["id"], sentence=row["sentence"], code=row["code"],
            issued_at=_epoch(row["issued_at"]), redeemed_at=now)

    # -- records

    async def add_record(self, record: c.ConsentRecord) -> None:
        await self._pool.execute(
            f"INSERT INTO consent_records ({_RECORD_COLUMNS}) VALUES "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)",
            record.id, record.voice, record.principal, record.challenge_id, record.sentence, record.code,
            record.transcript, record.phrase_score, record.code_ok, record.speaker_score_reference,
            record.speaker_score_account, record.decision, record.reason, record.terms_version,
            record.consent_audio_path, record.reference_audio_path, record.embedding_model, record.embedding,
            record.edition, record.created_at, record.reviewed_at, record.reviewer, record.review_note)

    async def get_record(self, record_id: str) -> Optional[c.ConsentRecord]:
        row = await self._pool.fetchrow(f"SELECT {_RECORD_COLUMNS} FROM consent_records WHERE id = $1", record_id)
        return _row_to_record(row) if row else None

    async def list_records(self, decision: Optional[str] = None, limit: int = 200,
                           newest: bool = False) -> list[c.ConsentRecord]:
        """Oldest first (the review queue is first in, first out); ``newest`` keeps the latest ``limit``
        instead, still in chronological order."""
        if newest:
            rows = await self._pool.fetch(
                f"SELECT * FROM (SELECT {_RECORD_COLUMNS} FROM consent_records "
                "WHERE ($1::text IS NULL OR decision = $1) ORDER BY created_at DESC LIMIT $2) latest "
                "ORDER BY created_at", decision, limit)
        else:
            rows = await self._pool.fetch(
                f"SELECT {_RECORD_COLUMNS} FROM consent_records WHERE ($1::text IS NULL OR decision = $1) "
                "ORDER BY created_at LIMIT $2", decision, limit)
        return [_row_to_record(r) for r in rows]

    async def last_record(self, principal: str) -> Optional[c.ConsentRecord]:
        row = await self._pool.fetchrow(
            f"SELECT {_RECORD_COLUMNS} FROM consent_records WHERE principal = $1 ORDER BY created_at DESC LIMIT 1",
            principal)
        return _row_to_record(row) if row else None

    async def supersede_reviews(self, *, voice: str, keep: str) -> int:
        done = await self._pool.execute(
            "UPDATE consent_records SET decision = 'rejected', reviewer = $3, reviewed_at = now(), "
            "review_note = $4 WHERE voice = $1 AND id <> $2 AND decision = 'review'",
            voice, keep, SUPERSEDED_BY_SYSTEM,
            f"superseded: a newer recording ({keep}) replaced this voice before anyone reviewed it")
        return int(done.rsplit(" ", 1)[-1])

    async def resolve_review(self, record_id: str, *, decision: str, reviewer: str, note: Optional[str]) -> c.ConsentRecord:
        row = await self._pool.fetchrow(
            f"UPDATE consent_records SET decision = $2, reviewer = $3, review_note = $4, reviewed_at = now() "
            f"WHERE id = $1 AND decision = 'review' RETURNING {_RECORD_COLUMNS}",
            record_id, decision, reviewer, note)
        if row is not None:
            return _row_to_record(row)
        existing = await self.get_record(record_id)
        if existing is None:
            raise c.ReviewError("unknown", "No such consent record.")
        raise c.ReviewError("resolved", f"That record is not waiting for review (it is {existing.decision}).")

    # -- account voiceprints

    async def get_voiceprint(self, principal: str) -> Optional[c.AccountVoiceprint]:
        row = await self._pool.fetchrow(
            "SELECT principal, embedding, embedding_model, source_record, created_at "
            "FROM account_voiceprints WHERE principal = $1", principal)
        if row is None:
            return None
        return c.AccountVoiceprint(
            principal=row["principal"], embedding=list(row["embedding"]), embedding_model=row["embedding_model"],
            source_record=row["source_record"], created_at=row["created_at"])

    async def set_voiceprint_if_absent(self, voiceprint: c.AccountVoiceprint) -> bool:
        done = await self._pool.execute(
            "INSERT INTO account_voiceprints (principal, embedding, embedding_model, source_record, created_at) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (principal) DO NOTHING",
            voiceprint.principal, voiceprint.embedding, voiceprint.embedding_model,
            voiceprint.source_record, voiceprint.created_at)
        return done.endswith(" 1")
