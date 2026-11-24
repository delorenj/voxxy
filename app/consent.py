"""The consent gate: the one function every likeness-making path goes through.

"Likeness-making" is anything that turns a recording of a voice into a voice Voxxy will speak with. Today
the gated path is ``POST /voices/own`` (record yourself). Any later clone path must call
``ConsentGate.check`` before it registers a voice and obey the ``Outcome``. The two ungated paths, a
per-request ``reference_audio_b64`` and the plain ``POST /voices`` upload, are the fleet's own tools: they
are open only where ``ungated_clone_allowed(principal)`` says so (the root key, or any key in the self
edition), and that one function is what per-customer keys flip.

What the gate does for one attempt, in order (product doc 4.1):

1. the challenge (a random sentence plus a 6-digit code, issued per principal, valid 10 minutes, single use)
   must be unused and unexpired;
2. ASR (faster-whisper) transcribes the consent recording: the sentence and the code must be in it;
3. speaker embeddings (TitaNet-large, after cutting the silence out) compare the consent clip with the
   optional longer reference clip, and whatever will become the voice with the principal's account
   voiceprint (the first accepted own voice fixes the voiceprint; every later one must match it);
4. ``decide`` turns the numbers into ``accepted``, ``review`` or ``rejected``;
5. a consent record is written for every attempt, with the audio kept, whatever the decision. The voice
   carries a ``consent-record:<id>`` tag, so a later review decision acts only on the voice that record made.

The thresholds are the G5 spike's, measured on real recordings (``Thresholds``); everything is
env-configurable (``VOX_CONSENT_*``), and they should be recalibrated after about 50 reviewed real consent
pairs. The check stops casual misuse. It does not prove consent against
someone who plays a recording of another person into the microphone, and a voice match across different
microphones is imperfect (app/voiceprint.py); the review queue is the backstop.

Edition: ``hosted`` enforces the decision. ``self`` runs the same checks and records them, but a failed
check only warns: the voice is still made, tagged ``consent-unchecked-self``. That is the operator's
responsibility and is not advertised.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol

import numpy as np

from app import asr as asr_mod
from app import auth as auth_mod
from app import edition as edition_mod
from app import models as models_mod
from app import voiceprint as vp_mod

logger = logging.getLogger("vox.consent")

CHALLENGE_TTL_SECONDS = 600  # 10 minutes
_MAX_OPEN = 2000  # bound memory in the in-memory store: oldest entries are dropped past this

# Voice tags the gate owns. TAG_LEGACY_UNVERIFIED is what v1 (before G5) put on every own voice.
TAG_VERIFIED = "consent-verified"
TAG_PENDING = "consent-pending"
TAG_UNCHECKED_SELF = "consent-unchecked-self"
TAG_LEGACY_UNVERIFIED = "unverified-consent"
CONSENT_TAGS = (TAG_VERIFIED, TAG_PENDING, TAG_UNCHECKED_SELF, TAG_LEGACY_UNVERIFIED)

# Ties a voice to the consent record that made it. The review decision acts on a voice only when the
# record's tag is on it, so approving an old recording can never verify a newer voice of the same name.
RECORD_TAG_PREFIX = "consent-record:"


def record_tag(record_id: str) -> str:
    return RECORD_TAG_PREFIX + record_id


def record_id_of(tags: list[str]) -> Optional[str]:
    for tag in tags:
        if tag.startswith(RECORD_TAG_PREFIX):
            return tag[len(RECORD_TAG_PREFIX):]
    return None


def public_tags(tags: list[str]) -> list[str]:
    """The tags a client sees: bookkeeping tags stay on the server."""
    return [t for t in tags if not t.startswith(RECORD_TAG_PREFIX)]

ACCEPTED, REVIEW, REJECTED, UNCHECKED = "accepted", "review", "rejected", "unchecked"

OPEN_PRINCIPAL = "open"

# Short, easy to read aloud, unambiguous phonetics, no names of real people, no numerals (ASR writes
# "twelve" as "12", which would cost the person points for saying the sentence right).
SENTENCES = (
    "The quick brown fox jumped over the lazy dog while the morning fog lifted off the harbor.",
    "I am recording this voice myself, and I am happy for it to be used to speak my own words.",
    "Please call me back when the weather clears, and bring the blue folder from the kitchen table.",
    "Every evening the lighthouse keeper climbs the stairs, lights the lamp and watches the water.",
    "My voice is my own, and I am choosing to make a copy of it today, in my own words.",
    "A small green bicycle leaned against the old stone wall beside the market on Saturday.",
    "We planted a dozen tomato seeds in the garden, and by July the vines were taller than the fence.",
    "Today I am reading these words aloud so that this voice can be made from my recording.",
)


# ---------------------------------------------------------------- challenges


@dataclass(slots=True)
class Challenge:
    challenge_id: str
    sentence: str
    code: str
    created_at: float
    expires_at: float
    used_at: float | None = None
    principal: str = OPEN_PRINCIPAL


@dataclass(slots=True)
class Redeemed:
    """What a successful redemption proves, for the consent record."""

    challenge_id: str
    sentence: str
    code: str
    issued_at: float
    redeemed_at: float


class ChallengeError(Exception):
    """A challenge cannot be redeemed. ``kind`` is ``unknown``, ``expired`` or ``used``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def new_challenge(principal: str, now: float, ttl: float = CHALLENGE_TTL_SECONDS) -> Challenge:
    return Challenge(
        challenge_id=secrets.token_urlsafe(18),
        sentence=secrets.choice(SENTENCES),
        code=f"{secrets.randbelow(10**6):06d}",
        created_at=now,
        expires_at=now + ttl,
        principal=principal,
    )


def challenge_error(kind: str) -> ChallengeError:
    messages = {
        "unknown": "That challenge is not one this server issued. Start again.",
        "used": "That challenge was already used. Start again to get a new sentence.",
        "expired": "That challenge expired. Start again to get a new sentence.",
    }
    return ChallengeError(kind, messages[kind])


@dataclass
class ChallengeStore:
    """In-memory challenges: the fallback the tests use, and the logic the postgres store mirrors."""

    ttl_seconds: float = CHALLENGE_TTL_SECONDS
    _items: dict[str, Challenge] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(self, now: float | None = None, principal: str = OPEN_PRINCIPAL) -> Challenge:
        now = time.time() if now is None else now
        challenge = new_challenge(principal, now, self.ttl_seconds)
        with self._lock:
            self._prune(now)
            self._items[challenge.challenge_id] = challenge
        return challenge

    def check(self, challenge_id: str, now: float | None = None, principal: str | None = None) -> Challenge:
        """Look a challenge up without using it. Raises ChallengeError when it cannot be used."""
        now = time.time() if now is None else now
        with self._lock:
            return self._usable(challenge_id, now, principal)

    def redeem(self, challenge_id: str, now: float | None = None, principal: str | None = None) -> Redeemed:
        """Use a challenge exactly once. Raises ChallengeError when it cannot be used."""
        now = time.time() if now is None else now
        with self._lock:
            challenge = self._usable(challenge_id, now, principal)
            challenge.used_at = now
            return Redeemed(
                challenge_id=challenge.challenge_id, sentence=challenge.sentence, code=challenge.code,
                issued_at=challenge.created_at, redeemed_at=now,
            )

    # -- internals (lock held)

    def _usable(self, challenge_id: str, now: float, principal: str | None) -> Challenge:
        challenge = self._items.get(challenge_id)
        # Another principal's challenge is "unknown", not "forbidden": do not confirm it exists.
        if challenge is None or (principal is not None and challenge.principal != principal):
            raise challenge_error("unknown")
        if challenge.used_at is not None:
            raise challenge_error("used")
        if now >= challenge.expires_at:
            raise challenge_error("expired")
        return challenge

    def _prune(self, now: float) -> None:
        # Keep used/expired entries for a while so the answer to a late request is "used" or
        # "expired" rather than "unknown"; drop them after an hour.
        stale = [k for k, c in self._items.items() if now > c.expires_at + 3600]
        for key in stale:
            del self._items[key]
        while len(self._items) >= _MAX_OPEN:
            del self._items[next(iter(self._items))]


# ---------------------------------------------------------------- thresholds and the decision


@dataclass(frozen=True)
class Thresholds:
    """The numbers behind ``decide``, from the G5 calibration spike (TitaNet-large, raw cosine, silence cut
    out first; voxxy-g5-spike.md). Env-configurable; recalibrate after about 50 reviewed real consent pairs.

    accept / reject: speaker cosine at or above ``accept`` passes, below ``reject`` fails, between goes to
    review. The pair is wide on purpose: the spike measured 14-17% equal error, so most genuine pairs land
    in the review queue (fine while subscribers are few) and 0 of 4800 phone strangers reached ``accept``.
    phrase_ok / phrase_review: sentence score (1 minus word error, see app/asr.py); at or above ``phrase_ok``
    the sentence is heard, below ``phrase_review`` it is not, between is "ASR unsure" and goes to review.
    min_speech / min_reference_speech: seconds of actual speech (after the VAD) the consent clip and the
    longer sample must hold before a speaker comparison means anything.
    """

    accept: float = 0.70
    reject: float = 0.10
    phrase_ok: float = 0.80
    phrase_review: float = 0.50
    min_speech: float = 6.0
    min_reference_speech: float = 8.0

    @classmethod
    def from_env(cls) -> "Thresholds":
        def num(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default

        return cls(
            accept=num("VOX_CONSENT_ACCEPT", cls.accept),
            reject=num("VOX_CONSENT_REJECT", cls.reject),
            phrase_ok=num("VOX_CONSENT_PHRASE_OK", cls.phrase_ok),
            phrase_review=num("VOX_CONSENT_PHRASE_REVIEW", cls.phrase_review),
            min_speech=num("VOX_CONSENT_MIN_SPEECH", cls.min_speech),
            min_reference_speech=num("VOX_CONSENT_MIN_REFERENCE_SPEECH", cls.min_reference_speech),
        )


# Plain, non-accusatory words for the person. Nothing here says "fake", "fraud" or "someone else".
REASON_PHRASE = (
    "We could not hear the whole sentence in your recording. Find a quiet spot, read the sentence "
    "out loud and then the code, and try again with a new sentence."
)
REASON_CODE = (
    "We could not hear the code in your recording. Read the sentence, then the six digits, "
    "and try again with a new sentence."
)
REASON_SPEECH = (
    "We could not hear enough speech in your recording. Read the whole sentence and the code at a steady "
    "pace, close to the microphone, and try again with a new sentence."
)
REASON_REFERENCE_SPEECH = (
    "We could not hear enough speech in the longer sample. Use a clip with at least ten seconds of you "
    "talking, or leave the longer sample out."
)
REASON_CHALLENGE_USED = "That sentence was already used by another recording. Start again to get a new one."
REASON_REFERENCE_MISMATCH = (
    "The longer sample did not sound like the same voice as the sentence recording. Record both in the "
    "same place with the same microphone and try again, or leave the longer sample out."
)
REASON_ACCOUNT_MISMATCH = (
    "This account already has a voice of its own, and this recording does not sound like the same person. "
    "A hosted account holds one person's own voice."
)
REASON_ASR_UNSURE = "We were not sure we heard every word, so a person will take a look at your recording."
REASON_SPEAKER_UNSURE = "We could not tell for sure that the recordings match, so a person will take a look at your recording."
REASON_MODEL_CHANGED = "This account's voiceprint was made by an older version of the check, so a person will take a look at your recording."
REASON_ACCEPTED = "Your recording matched the sentence, the code and your voice."
REASON_UNAVAILABLE = "The consent check could not run."


@dataclass(frozen=True)
class Decision:
    decision: str
    reason: str


def decide(
    *, phrase: float, code: str, reference: float | None, account: float | None,
    model_changed: bool = False, t: Thresholds,
    consent_speech: float | None = None, reference_speech: float | None = None,
) -> Decision:
    """accepted / review / rejected from the numbers. Pure; every branch is unit-tested.

    ``consent_speech`` / ``reference_speech`` are seconds of speech found by the VAD (None = not measured).
    ``code`` is ``ok`` (within one edit of the code) or ``missing``.
    """
    if phrase < t.phrase_review:
        return Decision(REJECTED, REASON_PHRASE)
    if code != "ok":
        return Decision(REJECTED, REASON_CODE)
    if consent_speech is not None and consent_speech < t.min_speech:
        return Decision(REJECTED, REASON_SPEECH)
    if reference_speech is not None and reference_speech < t.min_reference_speech:
        return Decision(REJECTED, REASON_REFERENCE_SPEECH)
    if reference is not None and reference < t.reject:
        return Decision(REJECTED, REASON_REFERENCE_MISMATCH)
    if account is not None and account < t.reject:
        return Decision(REJECTED, REASON_ACCOUNT_MISMATCH)
    if model_changed:
        return Decision(REVIEW, REASON_MODEL_CHANGED)
    if phrase < t.phrase_ok:
        return Decision(REVIEW, REASON_ASR_UNSURE)
    if any(s < t.accept for s in (reference, account) if s is not None):
        return Decision(REVIEW, REASON_SPEAKER_UNSURE)
    return Decision(ACCEPTED, REASON_ACCEPTED)


# ---------------------------------------------------------------- records and the store interface


class ConsentUnavailable(RuntimeError):
    """The ASR or embedding model cannot be loaded or run. Hosted answers 503 and burns no challenge."""


class ReviewError(Exception):
    """A review cannot be resolved. ``kind`` is ``unknown`` or ``resolved``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ConsentRecord:
    id: str
    voice: str
    principal: str
    challenge_id: str
    sentence: str
    code: str
    decision: str
    reason: str
    transcript: Optional[str] = None
    phrase_score: Optional[float] = None
    code_ok: Optional[bool] = None
    speaker_score_reference: Optional[float] = None
    speaker_score_account: Optional[float] = None
    terms_version: Optional[str] = None
    consent_audio_path: Optional[str] = None
    reference_audio_path: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding: Optional[list[float]] = None  # what would become the account voiceprint
    edition: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed_at: Optional[datetime] = None
    reviewer: Optional[str] = None
    review_note: Optional[str] = None


@dataclass
class AccountVoiceprint:
    principal: str
    embedding: list[float]
    embedding_model: str
    source_record: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ConsentStore(Protocol):
    async def issue_challenge(self, principal: str) -> Challenge: ...
    async def check_challenge(self, challenge_id: str, principal: str) -> Challenge: ...
    async def redeem_challenge(self, challenge_id: str, principal: str) -> Redeemed: ...
    async def add_record(self, record: ConsentRecord) -> None: ...
    async def get_record(self, record_id: str) -> Optional[ConsentRecord]: ...
    async def list_records(self, decision: Optional[str] = None, limit: int = 200,
                           newest: bool = False) -> list[ConsentRecord]: ...
    async def resolve_review(self, record_id: str, *, decision: str, reviewer: str, note: Optional[str]) -> ConsentRecord: ...
    async def supersede_reviews(self, *, voice: str, keep: str) -> int: ...
    async def get_voiceprint(self, principal: str) -> Optional[AccountVoiceprint]: ...
    async def set_voiceprint_if_absent(self, voiceprint: AccountVoiceprint) -> bool: ...


# ---------------------------------------------------------------- providers


@dataclass
class Providers:
    """The two models the gate needs. Tests pass fakes; production gets ``default_providers()``."""

    transcriber: asr_mod.Transcriber
    embedder: vp_mod.Embedder
    # Cuts the silence out of 16 kHz audio before it is embedded (silero VAD in production; tests pass identity).
    speech: Callable[[np.ndarray], np.ndarray] = vp_mod.speech_only

    def load(self) -> None:
        """Load both models now (blocking). Raises ConsentUnavailable when either cannot load."""
        try:
            for part in (self.transcriber, self.embedder):
                load = getattr(part, "load", None)
                if callable(load):
                    load()
        except Exception as exc:  # noqa: BLE001 - missing files, no network, bad checksum, runtime errors
            logger.error("consent models unavailable: %s", exc)
            raise ConsentUnavailable("consent check unavailable") from exc


def default_providers() -> Providers:
    return Providers(asr_mod.WhisperTranscriber(), vp_mod.TitanetEmbedder())


# ---------------------------------------------------------------- what a voice's consent state allows


REASON_VOICE_PENDING = (
    "This voice is waiting for a consent check and cannot be used for speech yet. "
    "It becomes available once someone has reviewed the recording."
)


def consent_state(tags: list[str], edition: str | None = None) -> Optional[str]:
    """verified | pending | unchecked, from a voice's tags; None for a voice the gate never touched.

    ``unverified-consent`` is the v1 tag (before G5). The startup retag turns it into pending (hosted) or
    unchecked (self); until then it reads as pending, the strict side. ``consent-unchecked-self`` reads as
    pending in the hosted edition, which will not speak with it (``synthesis_block_reason``).
    """
    if TAG_PENDING in tags or TAG_LEGACY_UNVERIFIED in tags:
        return "pending"
    if TAG_VERIFIED in tags:
        return "verified"
    if TAG_UNCHECKED_SELF in tags:
        edition = edition or edition_mod.current_edition()
        return "pending" if edition == edition_mod.EDITION_HOSTED else "unchecked"
    return None


REASON_VOICE_UNCHECKED = (
    "This voice was made without a consent check (on a self-hosted install), so the hosted edition will not "
    "speak with it. Record it again here."
)


def synthesis_block_reason(tags: list[str], edition: str | None = None) -> Optional[str]:
    """A plain reason this voice may not be spoken with, or None. The single place that decides it.

    Hosted blocks every voice the check did not pass: pending ones, voices from before the check existed,
    and ``consent-unchecked-self`` ones (a self-edition leftover must not start speaking after a switch).
    """
    edition = edition or edition_mod.current_edition()
    hosted = edition == edition_mod.EDITION_HOSTED
    if TAG_PENDING in tags:
        return REASON_VOICE_PENDING
    if TAG_LEGACY_UNVERIFIED in tags and hosted:
        return REASON_VOICE_PENDING + " (It was recorded before the consent check existed.)"
    if TAG_UNCHECKED_SELF in tags and hosted:
        return REASON_VOICE_UNCHECKED
    return None


def ungated_clone_allowed(principal: str, edition: str | None = None) -> bool:
    """May this caller turn a clip into a voice without the consent gate (a per-request
    ``reference_audio_b64``, the plain ``POST /voices`` upload)? THE one switch for it.

    Self edition: yes, the operator's own responsibility. Hosted: only the root key, which is the fleet's.
    Today every authenticated caller holds the root key, so the answer is yes for all of them; per-customer
    keys (product doc 7.1) get their own principals and this returns False for them, with no other change.
    """
    edition = edition or edition_mod.current_edition()
    if edition != edition_mod.EDITION_HOSTED:
        return True
    return principal == auth_mod.root_principal()


# ---------------------------------------------------------------- the gate


def consent_dir() -> Path:
    """Where consent audio is kept (the record's audio, per product doc 4.1 step 6)."""
    from app import voices as voices_mod

    raw = os.environ.get("VOX_CONSENT_DIR", "").strip()
    return Path(raw) if raw else voices_mod.VOICES_DIR / "_consent"


def terms_version() -> str:
    return os.environ.get("VOX_TERMS_VERSION", "").strip() or "draft-1"


@dataclass
class Outcome:
    """What the caller of ``ConsentGate.check`` does next."""

    record: ConsentRecord
    decision: str            # accepted | review | rejected | unchecked
    reason: str              # plain words for the person
    create_voice: bool       # false only for a hosted rejection
    voice_tags: list[str]    # consent tags for the new voice, incl. its record tag (the caller adds ``own``)
    voice_audio: str         # the clip that becomes the voice: the reference if given, else the consent clip
    warning: Optional[str] = None  # self edition: a failed check that did not stop the voice

    @property
    def state(self) -> str:
        """verified | pending | unchecked | rejected: the voice's consent state as the UI words it."""
        if not self.create_voice:
            return "rejected"
        if TAG_PENDING in self.voice_tags:
            return "pending"
        return "verified" if TAG_VERIFIED in self.voice_tags else "unchecked"


class ConsentGate:
    def __init__(
        self,
        store: ConsentStore,
        providers: Providers,
        *,
        thresholds: Callable[[], Thresholds] = Thresholds.from_env,
        edition: Callable[[], str] = edition_mod.current_edition,
        max_parallel: int = 2,
    ) -> None:
        self.store = store
        self.providers = providers
        self._thresholds = thresholds
        self._edition = edition
        self._slots = asyncio.Semaphore(max_parallel)

    # -- model readiness

    async def ensure_ready(self) -> None:
        """Load the models off the event loop. Raises ConsentUnavailable (-> 503) when they cannot load."""
        await asyncio.to_thread(self.providers.load)

    # -- the single entry for likeness-making

    async def check(
        self, *, principal: str, challenge_id: str, voice: str,
        consent_audio: str, reference_audio: str | None = None,
    ) -> Outcome:
        """Run every check for one attempt and write the consent record. See the module docstring.

        ``consent_audio`` / ``reference_audio`` are readable audio files; they are copied into the consent
        directory for the record, so the caller may delete its own copies. Raises ``ChallengeError``
        (unknown, expired or used challenge) and, in the hosted edition, ``ConsentUnavailable``. A
        challenge is not used up by an unavailable check: the person can retry with the same sentence.
        """
        edition = self._edition()
        hosted = edition == edition_mod.EDITION_HOSTED
        challenge = await self.store.check_challenge(challenge_id, principal)
        thresholds = self._thresholds()

        analysis: Optional[_Analysis] = None
        try:
            async with self._slots:
                analysis = await asyncio.to_thread(
                    self._analyse, challenge, consent_audio, reference_audio, thresholds)
        except ConsentUnavailable:
            if hosted:
                raise
        record_id = uuid.uuid4().hex
        try:
            redeemed = await self.store.redeem_challenge(challenge_id, principal)  # the single use happens here
        except ChallengeError:
            # Lost a race on one challenge: still an attempt, so it still gets a record and keeps its audio.
            consent_path, reference_path = _keep_audio(record_id, consent_audio, reference_audio)
            await self.store.add_record(ConsentRecord(
                id=record_id, voice=voice, principal=principal, challenge_id=challenge.challenge_id,
                sentence=challenge.sentence, code=challenge.code, decision=REJECTED, reason=REASON_CHALLENGE_USED,
                terms_version=terms_version(), consent_audio_path=consent_path,
                reference_audio_path=reference_path, edition=edition))
            raise

        consent_path, reference_path = _keep_audio(record_id, consent_audio, reference_audio)
        record = ConsentRecord(
            id=record_id, voice=voice, principal=principal, challenge_id=redeemed.challenge_id,
            sentence=redeemed.sentence, code=redeemed.code, decision=UNCHECKED, reason=REASON_UNAVAILABLE,
            terms_version=terms_version(), consent_audio_path=consent_path,
            reference_audio_path=reference_path, edition=edition,
        )
        voice_audio = reference_audio or consent_audio
        own_tag = record_tag(record_id)
        if analysis is None:  # self edition, models unavailable: recorded, not checked
            await self.store.add_record(record)
            return Outcome(record, UNCHECKED, REASON_UNAVAILABLE, True, [TAG_UNCHECKED_SELF, own_tag], voice_audio,
                           warning=REASON_UNAVAILABLE)

        decision, reference_score, account_score = await self._score_and_decide(
            principal, record_id, analysis, thresholds)
        record.decision, record.reason = decision.decision, decision.reason
        record.transcript = analysis.transcript
        record.phrase_score = analysis.phrase_score
        record.code_ok = analysis.code == "ok"
        record.speaker_score_reference = reference_score
        record.speaker_score_account = account_score
        record.embedding_model = analysis.model_id
        clips = analysis.clips()
        record.embedding = [float(x) for x in vp_mod.mean_embedding(*clips)] if clips else None
        await self.store.add_record(record)

        if decision.decision == ACCEPTED:
            return Outcome(record, ACCEPTED, decision.reason, True, [TAG_VERIFIED, own_tag], voice_audio)
        if not hosted:
            return Outcome(record, decision.decision, decision.reason, True, [TAG_UNCHECKED_SELF, own_tag],
                           voice_audio, warning=decision.reason)
        if decision.decision == REVIEW:
            return Outcome(record, REVIEW, decision.reason, True, [TAG_PENDING, own_tag], voice_audio)
        return Outcome(record, REJECTED, decision.reason, False, [], voice_audio)

    async def _score_and_decide(
        self, principal: str, record_id: str, analysis: "_Analysis", t: Thresholds,
    ) -> tuple[Decision, Optional[float], Optional[float]]:
        """Score the clips against the account voiceprint and decide. When the decision is ACCEPTED on an
        account with no voiceprint, this attempt fixes it; if a concurrent attempt fixed it first, the
        attempt is scored against that voiceprint and decided again (two first recordings at once must not
        both be "the account's person")."""
        for _ in range(2):
            stored = await self.store.get_voiceprint(principal)
            model_changed = stored is not None and stored.embedding_model != analysis.model_id
            account_score = None
            clips = analysis.clips()
            if stored is not None and not model_changed and clips:
                vp = np.asarray(stored.embedding, dtype=np.float32)
                # Whatever becomes the voice must be the account's person: the consent clip and, when given, the reference.
                account_score = min(vp_mod.cosine(e, vp) for e in clips)
            reference_score = (
                vp_mod.cosine(analysis.consent_embedding, analysis.reference_embedding)
                if analysis.consent_embedding is not None and analysis.reference_embedding is not None else None
            )
            decision = decide(
                phrase=analysis.phrase_score, code=analysis.code, reference=reference_score,
                account=account_score, model_changed=model_changed, t=t,
                consent_speech=analysis.consent_speech_s, reference_speech=analysis.reference_speech_s,
            )
            if decision.decision == ACCEPTED and stored is None:
                won = await self.store.set_voiceprint_if_absent(AccountVoiceprint(
                    principal=principal, embedding=[float(x) for x in vp_mod.mean_embedding(*clips)],
                    embedding_model=analysis.model_id, source_record=record_id))
                if not won:
                    continue  # someone fixed the voiceprint while this one was being checked: decide again
            return decision, reference_score, account_score
        raise RuntimeError("account voiceprint kept appearing and vanishing")  # pragma: no cover - cannot happen

    async def voice_replaced(self, outcome: "Outcome") -> int:
        """Call after a voice was (re)registered from ``outcome``: reviews still queued for an earlier voice
        of that name are superseded, so the operator never approves a recording whose voice is gone."""
        return await self.store.supersede_reviews(voice=outcome.record.voice, keep=outcome.record.id)

    async def resolve_review(
        self, record_id: str, *, decision: str, reviewer: str, note: Optional[str],
    ) -> ConsentRecord:
        """Operator verdict on a queued record: ``approve`` makes it accepted (and fixes the account
        voiceprint if there is none yet), ``reject`` makes it rejected. The caller updates the voice."""
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        record = await self.store.resolve_review(
            record_id, decision=ACCEPTED if decision == "approve" else REJECTED, reviewer=reviewer, note=note)
        if decision == "approve" and record.embedding and record.embedding_model:
            await self.store.set_voiceprint_if_absent(AccountVoiceprint(
                principal=record.principal, embedding=record.embedding, embedding_model=record.embedding_model,
                source_record=record.id))
        return record

    # -- analysis (blocking; runs in a worker thread)

    def _analyse(
        self, challenge: Challenge, consent_audio: str, reference_audio: str | None, t: Thresholds,
    ) -> "_Analysis":
        try:
            samples = vp_mod.load_audio_16k(consent_audio)
            transcript = self.providers.transcriber.transcribe(samples, vp_mod.SAMPLE_RATE)
            # The speaker model hears speech only: silence and hiss halve same-person scores (spike).
            consent_speech = self.providers.speech(samples)
            consent_speech_s = len(consent_speech) / vp_mod.SAMPLE_RATE
            consent_embedding = None
            if consent_speech_s >= t.min_speech:
                consent_embedding = self.providers.embedder.embed(consent_speech, vp_mod.SAMPLE_RATE)
            reference_embedding = None
            reference_speech_s = None
            if reference_audio:
                reference_speech = self.providers.speech(vp_mod.load_audio_16k(reference_audio))
                reference_speech_s = len(reference_speech) / vp_mod.SAMPLE_RATE
                if reference_speech_s >= t.min_reference_speech:
                    reference_embedding = self.providers.embedder.embed(reference_speech, vp_mod.SAMPLE_RATE)
        except ConsentUnavailable:
            raise
        except models_mod.ModelUnavailable as exc:
            raise ConsentUnavailable("consent check unavailable") from exc
        except Exception as exc:  # noqa: BLE001 - a model that loads but cannot run is just as unavailable
            logger.exception("consent analysis failed")
            raise ConsentUnavailable("consent check unavailable") from exc
        text = asr_mod.check_transcript(challenge.sentence, challenge.code, transcript)
        return _Analysis(
            transcript=transcript, phrase_score=text.phrase_score, code=text.code,
            consent_embedding=consent_embedding, reference_embedding=reference_embedding,
            consent_speech_s=consent_speech_s, reference_speech_s=reference_speech_s,
            model_id=self.providers.embedder.model_id,
        )


@dataclass
class _Analysis:
    transcript: str
    phrase_score: float
    code: str
    consent_embedding: Optional[np.ndarray]      # None when the consent clip holds too little speech
    reference_embedding: Optional[np.ndarray]    # None when there is no longer sample, or it holds too little speech
    model_id: str
    consent_speech_s: float = 0.0
    reference_speech_s: Optional[float] = None

    def clips(self) -> list[np.ndarray]:
        return [e for e in (self.consent_embedding, self.reference_embedding) if e is not None]


def _keep_audio(record_id: str, consent_audio: str, reference_audio: str | None) -> tuple[str, Optional[str]]:
    """Copy the attempt's audio next to the record, whatever the decision. Paths are relative to ``consent_dir()``."""
    base = consent_dir()
    base.mkdir(parents=True, exist_ok=True)
    consent_name = f"{record_id}.consent{Path(consent_audio).suffix or '.wav'}"
    shutil.copyfile(consent_audio, base / consent_name)
    reference_name = None
    if reference_audio:
        reference_name = f"{record_id}.reference{Path(reference_audio).suffix or '.wav'}"
        shutil.copyfile(reference_audio, base / reference_name)
    return consent_name, reference_name
