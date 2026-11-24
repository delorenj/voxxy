"""ASR half of the consent check: did the person say the sentence and the code?

``Transcriber`` is the provider interface (tests inject a fake); ``WhisperTranscriber`` is faster-whisper
``base.en`` int8 on CPU, loaded lazily once per process. The text checks below are pure functions:

- ``phrase_score``: 1 minus the word error of the challenge sentence against its best-matching stretch of
  the transcript, 0..1. Extra words before or after (the code, "um", a restart) are free; a near-identical
  spelling counts as a match. Spoken numbers are folded to digits first.
- ``code_distance`` / ``check_code``: edits between the 6-digit code and the best-matching stretch of the
  transcript's digits (said as digits, "483 917", or as number words, "four hundred eighty three ...").
  Within ``CODE_MAX_EDITS`` (1) passes: the code proves the recording is fresh, it is not a secret, and one
  edit lets about 55 in a million random codes through while forgiving the one-digit slip ASR makes.

The rule is the G5 spike's (scripts/g5_calibrate.py ``phrase_check``, measured on 25 synthesized readings
through Opus and room noise: genuine readings score 0.95 or more, the same audio against another sentence
0.235 or less, a wrong code is always 4+ edits off).
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Protocol

import numpy as np

logger = logging.getLogger("vox.asr")

SAMPLE_RATE = 16_000

# ---------------------------------------------------------------- provider interface


class Transcriber(Protocol):
    model_id: str

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> str:
        """The words in 16 kHz mono float32 ``samples``, as plain text. May block; the caller threads it."""
        ...


class WhisperTranscriber:
    """faster-whisper on CPU. ``load()`` is idempotent and thread-safe; ``transcribe`` loads on demand."""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self.model_id = f"faster-whisper:{os.environ.get('VOX_ASR_MODEL', 'base.en')}:int8"

    def load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            from app import models

            path = models.ensure_asr_model()
            threads = int(os.environ.get("VOX_CONSENT_THREADS", "4"))
            logger.info("loading ASR model %s (cpu, int8, %d threads)", path, threads)
            self._model = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)

    def transcribe(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        if sample_rate != SAMPLE_RATE:
            raise ValueError("WhisperTranscriber takes 16 kHz audio")
        self.load()
        assert self._model is not None
        # The settings the spike measured: beam 5, timestamps ON (with them off Whisper drops the code at the
        # end of the reading: 53 of 75 pass instead of 72), no cross-segment conditioning. NEVER pass the
        # sentence or the code as initial_prompt or hotwords: the text must come from the audio alone.
        segments, _info = self._model.transcribe(
            np.ascontiguousarray(samples, dtype=np.float32), language="en", beam_size=5,
            condition_on_previous_text=False, without_timestamps=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


# ---------------------------------------------------------------- text normalisation

_UNITS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
# ASR's usual homophones for digits, honoured only between two other digits
_DIGIT_HOMOPHONES = {"for": 4, "fore": 4, "to": 2, "too": 2, "ate": 8, "won": 1, "tree": 3, "sex": 6}
_SPELLING = {"harbour": "harbor", "colour": "color", "neighbour": "neighbor", "grey": "gray", "ok": "okay"}
_CONTRACTIONS = {
    "i'm": "i am", "i've": "i have", "i'll": "i will", "i'd": "i would", "it's": "it is",
    "that's": "that is", "we're": "we are", "we've": "we have", "you're": "you are",
    "don't": "do not", "can't": "can not", "won't": "will not", "isn't": "is not",
    "let's": "let us", "there's": "there is", "what's": "what is",
}
_ZERO_WORDS = ("o", "oh", "zero")


def _tokens(text: str) -> list[str]:
    text = text.lower().replace("\u2019", "'")
    out: list[str] = []
    for raw in text.split():  # expand contractions on whole words first ("I'm" -> "i am")
        bare = raw.strip(".,;:!?\"()")
        out.append(_CONTRACTIONS.get(bare, raw))
    text = " ".join(out)
    text = re.sub(r"(\d),(\d)", r"\1\2", text)          # 482,917 -> 482917
    text = re.sub(r"[-\u2010\u2013\u2014/]", " ", text)
    text = text.replace("'", "")
    text = re.sub(r"(\d)([a-z])|([a-z])(\d)", lambda m: " ".join(g for g in m.groups() if g), text)
    return [_SPELLING.get(t, t) for t in re.findall(r"[a-z]+|\d+", text)]


def normalise(text: str) -> list[str]:
    """Lowercase word tokens with spoken numbers folded to digit strings.

    "four eighty-two, nine seventeen" becomes ["4", "82", "9", "17"]: the code check reads the digit tokens
    as one stream, so how the person grouped them does not matter. Covers "N hundred [and] M", "double" and
    "triple", "oh" as zero and the usual digit homophones (for, to, ate, won), the last two kinds only next
    to other digits.
    """
    toks = _tokens(text)
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev_digit = bool(out) and out[-1].isdigit()
        next_digit = (nxt.isdigit() or (nxt in _UNITS and nxt not in ("o", "oh")) or nxt in _TEENS
                      or nxt in _TENS)
        if t in ("double", "triple") and nxt in _UNITS:
            out.append(str(_UNITS[nxt]) * (2 if t == "double" else 3))
            i += 2
            continue
        if t in ("o", "oh") and not (prev_digit or next_digit):
            out.append(t)  # a plain interjection
            i += 1
            continue
        if t in _DIGIT_HOMOPHONES and (prev_digit and next_digit):
            out.append(str(_DIGIT_HOMOPHONES[t]))
            i += 1
            continue
        if t in _UNITS and nxt == "hundred":
            value = _UNITS[t] * 100
            i += 2
            if i < len(toks) and toks[i] == "and":
                i += 1
            if i < len(toks) and toks[i] in _TEENS:
                value += _TEENS[toks[i]]
                i += 1
            elif i < len(toks) and toks[i] in _TENS:
                value += _TENS[toks[i]]
                i += 1
                if i < len(toks) and toks[i] in _UNITS and toks[i] not in _ZERO_WORDS:
                    value += _UNITS[toks[i]]
                    i += 1
            elif i < len(toks) and toks[i] in _UNITS and toks[i] not in ("o", "oh"):
                value += _UNITS[toks[i]]
                i += 1
            out.append(f"{value:03d}")
            continue
        if t in _TENS:
            if nxt in _UNITS and nxt not in _ZERO_WORDS:
                out.append(str(_TENS[t] + _UNITS[nxt]))
                i += 2
            else:
                out.append(str(_TENS[t]))
                i += 1
            continue
        if t in _TEENS:
            out.append(str(_TEENS[t]))
        elif t in _UNITS:
            out.append(str(_UNITS[t]))
        else:
            out.append(t)
        i += 1
    return out


def words(text: str) -> list[str]:
    """The normalised word tokens of ``text`` (see ``normalise``)."""
    return normalise(text)


# ---------------------------------------------------------------- the sentence


def _tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if a.isdigit() or b.isdigit():
        return False
    return len(a) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def phrase_score(sentence: str, transcript: str) -> float:
    """1 minus the word error of ``sentence`` against its best-aligned stretch of ``transcript`` (0..1).

    Semi-global word alignment: the transcript may carry extra words before and after at no cost; inside
    the stretch every substitution, insertion and deletion costs one. A near-identical spelling (character
    similarity 0.8 or more, four letters or more) counts as a match.
    """
    s, t = normalise(sentence), normalise(transcript)
    n, m = len(s), len(t)
    if not n:
        return 0.0
    prev = [0] * (m + 1)  # free start anywhere in the transcript
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (0 if _tok_eq(s[i - 1], t[j - 1]) else 1))
        prev = cur
    return max(0.0, 1.0 - min(prev) / n)  # free end anywhere


# ---------------------------------------------------------------- the code

CODE_MAX_EDITS = 1


def digit_stream(transcript: str) -> str:
    """Every digit the transcript says, in order: numerals as written, number words as numbers.

    "four eight three" -> "483", "four hundred eighty three" -> "483", "483, 917" -> "483917". Words that
    are not numbers are skipped (the sentence has plenty).
    """
    return "".join(t for t in normalise(transcript) if t.isdigit())


def code_distance(code: str, transcript: str) -> int:
    """Edit distance from ``code`` to its best-matching substring of the transcript's digit stream."""
    d = digit_stream(transcript)
    n, m = len(code), len(d)
    prev = [0] * (m + 1)
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (code[i - 1] != d[j - 1]))
        prev = cur
    return min(prev)


def check_code(code: str, transcript: str) -> str:
    """``"ok"`` (the code is in what was said, within ``CODE_MAX_EDITS`` edits) or ``"missing"``."""
    return "ok" if code_distance(code, transcript) <= CODE_MAX_EDITS else "missing"


@dataclass(frozen=True)
class TranscriptCheck:
    transcript: str
    phrase_score: float
    code: str  # ok | missing
    code_edits: int


def check_transcript(sentence: str, code: str, transcript: str) -> TranscriptCheck:
    edits = code_distance(code, transcript)
    return TranscriptCheck(transcript, phrase_score(sentence, transcript),
                           "ok" if edits <= CODE_MAX_EDITS else "missing", edits)
