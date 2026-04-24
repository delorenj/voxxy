"""ffmpeg + ffprobe subprocess helpers for voice preprocessing.

Design notes:
- FfmpegMissing is raised at call time, not at import time, so the CLI can
  import audio.py without ffmpeg installed and only fail when audio operations
  are actually attempted (e.g. `voice add`). This keeps `voxxy health` and
  `voxxy voice list` working on machines without ffmpeg.
- shutil.which is used for PATH-based detection rather than `ffmpeg --version`
  subprocess because which is instant (no subprocess overhead) and accurate:
  if which returns None, the binary is not on PATH, period.
- preprocess writes to a caller-provided dst path. The caller owns temp file
  lifecycle (creation + deletion). This keeps the function pure (no side effects
  beyond writing dst) and testable without mocking file deletion.
- The ffmpeg invocation uses `-y` to overwrite dst if it already exists. This is
  intentional: the CLI creates a temp file first (so the OS assigns a unique path),
  then calls preprocess to overwrite it. Without `-y`, ffmpeg would prompt and hang.
- sample_rate default 24000 Hz matches voxcpm's output format and is the format
  that voices are stored in after upload. Sending a 24kHz mono WAV to the server's
  /voices endpoint means the server's own resample step is a no-op.
- trim_seconds default 8.0 seconds matches the plan's `voice add --trim-seconds`
  default. VibeVoice quality degrades on clips longer than ~10s; voxcpm accepts
  up to 30s but doesn't benefit much past 8s for most voices.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class FfmpegMissing(RuntimeError):
    """Raised when ffmpeg or ffprobe is not on PATH.

    Includes a platform-appropriate install hint to avoid sending users to
    documentation when the fix is a single package manager command.
    """


class AudioProbeError(RuntimeError):
    """Raised when ffprobe fails to parse the given audio file."""


def _require_ffmpeg() -> None:
    """Raise FfmpegMissing if ffmpeg is not on PATH."""
    if shutil.which("ffmpeg") is None:
        raise FfmpegMissing(
            "ffmpeg not found on PATH. Install it:\n"
            "  Debian/Ubuntu: sudo apt install ffmpeg\n"
            "  macOS:         brew install ffmpeg"
        )


def _require_ffprobe() -> None:
    """Raise FfmpegMissing if ffprobe is not on PATH."""
    if shutil.which("ffprobe") is None:
        raise FfmpegMissing(
            "ffprobe not found on PATH. Install it:\n"
            "  Debian/Ubuntu: sudo apt install ffmpeg\n"
            "  macOS:         brew install ffmpeg"
        )


@dataclass(slots=True)
class AudioInfo:
    """Parsed metadata from an audio file."""

    duration: float
    channels: int
    sample_rate: int
    codec: str


def probe(path: Path) -> AudioInfo:
    """Return audio metadata for the given file using ffprobe.

    Parses the first audio stream's codec, channels, and sample_rate, plus
    the container-level duration (more reliable than stream duration for
    formats like WAV that don't always embed it in the stream).

    Raises:
        FfmpegMissing: if ffprobe is not on PATH
        AudioProbeError: if ffprobe fails or the file has no audio stream
    """
    _require_ffprobe()

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-of", "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AudioProbeError(
            f"ffprobe failed for {path}:\n{result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AudioProbeError(f"ffprobe output was not valid JSON: {exc}") from exc

    # Find the first audio stream
    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    if audio_stream is None:
        raise AudioProbeError(f"No audio stream found in {path}")

    # Duration: prefer format-level (present in WAV, MP3, OGG containers);
    # fall back to stream-level for formats that only embed it in the stream.
    fmt = data.get("format", {})
    duration_str = fmt.get("duration") or audio_stream.get("duration", "0")
    duration = float(duration_str)

    return AudioInfo(
        duration=duration,
        channels=int(audio_stream.get("channels", 1)),
        sample_rate=int(audio_stream.get("sample_rate", 0)),
        codec=audio_stream.get("codec_name", "unknown"),
    )


def _build_preprocess_argv(
    src: Path,
    dst: Path,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    trim_seconds: float = 8.0,
) -> list[str]:
    """Return the ffmpeg argv for a preprocess invocation.

    Pure function — no side effects, no subprocess. Extracted so tests can
    assert on the argument list without running ffmpeg.
    """
    return [
        "ffmpeg",
        "-y",                       # overwrite dst if exists (temp file pre-created)
        "-i", str(src),
        "-ac", str(channels),       # channel count
        "-ar", str(sample_rate),    # sample rate
        "-t", str(trim_seconds),    # trim duration
        str(dst),
    ]


def preprocess(
    src: Path,
    dst: Path,
    *,
    sample_rate: int = 24000,
    channels: int = 1,
    trim_seconds: float = 8.0,
) -> None:
    """Transcode src to a clean WAV file at dst.

    Applies:
    - Mono downmix (if channels=1 and src is stereo)
    - Sample rate conversion to sample_rate Hz
    - Duration trim to trim_seconds

    The output is always a WAV (PCM) file suitable for uploading to /voices.
    Raises:
        FfmpegMissing: if ffmpeg is not on PATH
        RuntimeError: if ffmpeg exits non-zero
    """
    _require_ffmpeg()

    # Probe input so we can log meaningful before/after info
    try:
        src_info = probe(src)
        logger.info(
            "preprocess input: %s (%.2fs, %dch, %dHz, %s)",
            src, src_info.duration, src_info.channels, src_info.sample_rate, src_info.codec,
        )
    except AudioProbeError:
        logger.info("preprocess input: %s (probe failed, proceeding anyway)", src)

    argv = _build_preprocess_argv(
        src, dst, sample_rate=sample_rate, channels=channels, trim_seconds=trim_seconds
    )
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg preprocessing failed:\n{result.stderr[-500:]}"
        )

    try:
        dst_info = probe(dst)
        logger.info(
            "preprocess output: %s (%.2fs, %dch, %dHz, %s)",
            dst, dst_info.duration, dst_info.channels, dst_info.sample_rate, dst_info.codec,
        )
    except AudioProbeError:
        logger.info("preprocess output: %s (probe failed)", dst)
