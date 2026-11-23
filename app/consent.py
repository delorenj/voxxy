"""Consent challenges for "record your own voice".

The hosted web app lets a person add a voice only by recording themselves live,
reading a sentence the server picked plus a short code. This module issues and
redeems those challenges.

v1 STATE: the store is **in memory** and per process. A restart forgets every open
challenge (the person fetches a new one) and two server processes would not share
them. That is acceptable for one core container; the story that adds the ASR check
(G5) moves the challenge and the consent record into postgres.

v1 does NOT verify consent. It proves the recording was requested after a fresh
challenge and uploaded once, nothing more. The transcript check (does the audio say
the sentence and the code) and the speaker-embedding match against a reference clip
are story G5, so voices made through this flow are marked ``unverified-consent``.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

CHALLENGE_TTL_SECONDS = 600  # 10 minutes
_MAX_OPEN = 2000  # bound memory: oldest entries are dropped past this

# Short, easy to read aloud, unambiguous phonetics, no names of real people.
SENTENCES = (
    "The quick brown fox jumped over the lazy dog while the morning fog lifted off the harbor.",
    "I am recording this voice myself, and I am happy for it to be used to speak my own words.",
    "Please call me back when the weather clears, and bring the blue folder from the kitchen table.",
    "Every evening the lighthouse keeper climbs the stairs, lights the lamp and watches the water.",
    "My voice is my own, and I am choosing to make a copy of it today, in my own words.",
    "A small green bicycle leaned against the old stone wall beside the market on Saturday.",
    "We planted twelve tomato seeds in the garden, and by July the vines were taller than the fence.",
    "Today I am reading these words aloud so that this voice can be made from my recording.",
)


@dataclass(slots=True)
class Challenge:
    challenge_id: str
    sentence: str
    code: str
    created_at: float
    expires_at: float
    used_at: float | None = None


@dataclass(slots=True)
class Redeemed:
    """What a successful redemption proves, for the consent sidecar."""

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


@dataclass
class ChallengeStore:
    ttl_seconds: float = CHALLENGE_TTL_SECONDS
    _items: dict[str, Challenge] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(self, now: float | None = None) -> Challenge:
        now = time.time() if now is None else now
        challenge = Challenge(
            challenge_id=secrets.token_urlsafe(18),
            sentence=secrets.choice(SENTENCES),
            code=f"{secrets.randbelow(10**6):06d}",
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._prune(now)
            self._items[challenge.challenge_id] = challenge
        return challenge

    def check(self, challenge_id: str, now: float | None = None) -> Challenge:
        """Look a challenge up without using it. Raises ChallengeError when it cannot be used."""
        now = time.time() if now is None else now
        with self._lock:
            return self._usable(challenge_id, now)

    def redeem(self, challenge_id: str, now: float | None = None) -> Redeemed:
        """Use a challenge exactly once. Raises ChallengeError when it cannot be used."""
        now = time.time() if now is None else now
        with self._lock:
            challenge = self._usable(challenge_id, now)
            challenge.used_at = now
            return Redeemed(
                challenge_id=challenge.challenge_id,
                sentence=challenge.sentence,
                code=challenge.code,
                issued_at=challenge.created_at,
                redeemed_at=now,
            )

    # -- internals (lock held)

    def _usable(self, challenge_id: str, now: float) -> Challenge:
        challenge = self._items.get(challenge_id)
        if challenge is None:
            raise ChallengeError("unknown", "That challenge is not one this server issued. Start again.")
        if challenge.used_at is not None:
            raise ChallengeError("used", "That challenge was already used. Start again to get a new sentence.")
        if now >= challenge.expires_at:
            raise ChallengeError("expired", "That challenge expired. Start again to get a new sentence.")
        return challenge

    def _prune(self, now: float) -> None:
        # Keep used/expired entries for a while so the answer to a late request is "used" or
        # "expired" rather than "unknown"; drop them after an hour.
        stale = [k for k, c in self._items.items() if now > c.expires_at + 3600]
        for key in stale:
            del self._items[key]
        while len(self._items) >= _MAX_OPEN:
            del self._items[next(iter(self._items))]
