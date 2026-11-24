"""Story G5, slow: the real models (TitaNet-large via sherpa-onnx, silero VAD, faster-whisper base.en int8, CPU) on three
short clips synthesized with Voxxy's house voices (tests/fixtures): house-f1 reading a sentence and a code,
house-f1 reading something else, house-m1 reading the sentence and the code.

Skipped when the model files are not on this host (they download on first use in the container; here we only
look, never fetch). A pass means the pipeline works end to end on real audio. It does NOT calibrate the
thresholds: these are synthetic voices, which is what the calibration spike on real recordings is for. The
clips are 6-9 s long, shorter than a real consent recording, so the speech minimums are lowered for them.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app import consent as consent_mod
from app import consent_store
from app import models as models_mod

pytestmark = pytest.mark.slow

FIXTURES = Path(__file__).parent / "fixtures"
SENTENCE = consent_mod.SENTENCES[0]
CODE = "483917"


def _speaker_model() -> Path | None:
    for cand in (models_mod.speaker_model_path(),
                 Path.home() / ".cache" / "phonograph" / models_mod.DEFAULT_SPEAKER_MODEL_FILE,
                 Path.home() / "audio" / "models" / models_mod.DEFAULT_SPEAKER_MODEL_FILE):
        if cand.is_file():
            return cand
    return None


def _asr_model() -> str | None:
    explicit = models_mod.os.environ.get("VOX_ASR_MODEL", "").strip()
    if explicit and Path(explicit).is_dir():
        return explicit
    local = models_mod.models_dir() / f"whisper-{models_mod.DEFAULT_ASR_MODEL}"
    if (local / "model.bin").is_file():
        return str(local)
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download(models_mod.DEFAULT_ASR_REPO, revision=models_mod.DEFAULT_ASR_REVISION,
                                 allow_patterns=list(models_mod.ASR_FILES), local_files_only=True)
    except Exception:  # noqa: BLE001 - not cached on this host
        return None


@pytest.fixture()
def real_gate(tmp_path, monkeypatch):
    speaker, asr = _speaker_model(), _asr_model()
    if speaker is None or asr is None:
        pytest.skip("consent model files are not on this host")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    monkeypatch.setenv("VOX_MODELS_DIR", str(tmp_path / "models"))
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / models_mod.DEFAULT_SPEAKER_MODEL_FILE).symlink_to(speaker)
    monkeypatch.setenv("VOX_ASR_MODEL", asr)
    monkeypatch.setenv("VOX_CONSENT_DIR", str(tmp_path / "consent"))
    monkeypatch.setenv("VOX_CONSENT_MIN_SPEECH", "3")
    monkeypatch.setenv("VOX_CONSENT_MIN_REFERENCE_SPEECH", "4")
    for var in ("VOX_EDITION", "VOX_CONSENT_ACCEPT", "VOX_CONSENT_REJECT", "VOX_CONSENT_PHRASE_OK", "VOX_CONSENT_PHRASE_REVIEW"):
        monkeypatch.delenv(var, raising=False)
    store = consent_store.MemoryConsentStore()
    gate = consent_mod.ConsentGate(store, consent_mod.default_providers())
    asyncio.run(gate.ensure_ready())
    return gate


def _challenge(gate, sentence=SENTENCE, code=CODE) -> str:
    ch = asyncio.run(gate.store.issue_challenge("tester"))
    stored = gate.store.challenges._items[ch.challenge_id]
    stored.sentence, stored.code = sentence, code
    return ch.challenge_id


def _check(gate, clip: str, *, voice="v", reference: str | None = None, challenge=None):
    return asyncio.run(gate.check(
        principal="tester", challenge_id=challenge or _challenge(gate), voice=voice,
        consent_audio=str(FIXTURES / clip), reference_audio=str(FIXTURES / reference) if reference else None))


def test_real_models_accept_the_sentence_and_code_then_hold_the_account_to_that_voice(real_gate) -> None:
    first = _check(real_gate, "consent-f1-challenge.ogg", voice="first")
    assert first.decision == consent_mod.ACCEPTED, (first.reason, first.record)
    assert first.record.phrase_score >= 0.80 and first.record.code_ok is True
    assert len(first.record.embedding) == 192 and first.record.embedding_model.startswith("sherpa-onnx:")
    assert asyncio.run(real_gate.store.get_voiceprint("tester")) is not None

    same = _check(real_gate, "consent-f1-challenge.ogg", voice="second")        # the same voice again
    assert same.decision == consent_mod.ACCEPTED
    assert same.record.speaker_score_account > 0.9

    other = _check(real_gate, "consent-m1-challenge.ogg", voice="third")        # a different voice
    assert other.decision in (consent_mod.REJECTED, consent_mod.REVIEW)      # never accepted
    assert other.record.speaker_score_account < 0.7
    assert other.record.phrase_score >= 0.80 and other.record.code_ok is True   # the words were fine; the voice was not


def test_real_models_match_a_longer_sample_by_the_same_voice(real_gate) -> None:
    out = _check(real_gate, "consent-f1-challenge.ogg", reference="consent-f1-other.ogg")
    assert out.decision == consent_mod.ACCEPTED
    assert out.record.speaker_score_reference > 0.8
    assert out.voice_audio.endswith("consent-f1-other.ogg")                      # the longer sample becomes the voice


def test_real_models_reject_a_recording_of_something_else(real_gate) -> None:
    out = _check(real_gate, "consent-f1-other.ogg")                              # no sentence, no code in it
    assert out.decision == consent_mod.REJECTED and out.create_voice is False
    assert out.record.phrase_score < 0.5
    assert (Path(consent_mod.consent_dir()) / out.record.consent_audio_path).is_file()
