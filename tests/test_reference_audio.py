"""app.reference: what a per-request reference clip must look like before any engine sees it.

Contract (docs: README "Per-request reference audio"): base64 of a mono integer-PCM WAV,
at most 2 MiB decoded, at most 15 s, at least 0.5 s, 8-48 kHz, and a
``reference_sample_rate`` (when sent) that matches the header.
"""

from __future__ import annotations

import base64

import pytest

from app import reference
from app.reference import ReferenceAudioError, decode_reference


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_valid_clip_decodes_with_its_shape(make_wav) -> None:
    clip = make_wav(seconds=12.0, sr=16_000)
    ref = decode_reference(_b64(clip), declared_sample_rate=16_000, prompt_text="hello there")
    assert ref.wav_bytes == clip
    assert ref.sample_rate == 16_000
    assert ref.duration_s == pytest.approx(12.0)
    assert ref.prompt_text == "hello there"


def test_exactly_fifteen_seconds_is_allowed(make_wav) -> None:
    assert decode_reference(_b64(make_wav(seconds=15.0))).duration_s == pytest.approx(15.0)


@pytest.mark.parametrize("subtype", ["PCM_16", "PCM_24", "PCM_32", "PCM_U8"])
def test_every_integer_pcm_width_is_accepted(make_wav, subtype: str) -> None:
    assert decode_reference(_b64(make_wav(subtype=subtype))).sample_rate == 16_000


def test_repr_never_shows_audio_or_transcript(make_wav) -> None:
    ref = decode_reference(_b64(make_wav()), prompt_text="my secret words")
    text = repr(ref)
    assert "secret" not in text
    assert "bytes=" in text and "prompt_text=set" in text


@pytest.mark.parametrize(
    ("build", "fragment"),
    [
        (lambda w: "not base64 at all!!", "not valid base64"),
        (lambda w: _b64(b""), "empty"),
        (lambda w: _b64(b"\x00\x01" * 8000), "not a readable WAV"),
        (lambda w: _b64(w(fmt="FLAC", subtype="PCM_16")), "must be a WAV"),
        (lambda w: _b64(w(subtype="FLOAT")), "integer PCM"),
        (lambda w: _b64(w(channels=2)), "mono"),
        (lambda w: _b64(w(seconds=15.5)), "limit is 15"),
        (lambda w: _b64(w(seconds=0.1)), "at least 0.5"),
        (lambda w: _b64(w(sr=96_000, seconds=1.0)), "outside"),
        # 12 s of 48 kHz 32-bit mono: under 15 s, over 2 MiB.
        (lambda w: _b64(w(seconds=12.0, sr=48_000, subtype="PCM_32")), "2097152"),
    ],
    ids=["garbage", "empty", "raw-pcm", "flac", "float", "stereo", "too-long", "too-short",
         "sample-rate", "too-big"],
)
def test_bad_clips_are_refused_with_a_shape_only_message(make_wav, build, fragment: str) -> None:
    with pytest.raises(ReferenceAudioError, match=fragment):
        decode_reference(build(make_wav))


def test_declared_sample_rate_must_match_the_header(make_wav) -> None:
    with pytest.raises(ReferenceAudioError, match="does not match the WAV header"):
        decode_reference(_b64(make_wav(sr=16_000)), declared_sample_rate=24_000)


def test_oversized_base64_is_refused_on_length_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Valid base64 that would decode fine; the length check alone refuses it.
    monkeypatch.setattr(reference, "REQUEST_REF_MAX_B64_CHARS", 8)
    with pytest.raises(ReferenceAudioError, match="larger than"):
        decode_reference("A" * 12)
