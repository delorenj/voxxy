"""Audio transcoding helpers.

Telegram voice notes must be OGG/Opus. VoxCPM2 and ElevenLabs both hand us
something else (WAV PCM and MP3 respectively), so everything converges on a
single ``wav_like_to_ogg_opus`` entry point that feeds ffmpeg on stdin and
reads the transcoded stream from stdout.

ffmpeg flags:
  -c:a libopus     Telegram-compatible codec
  -b:a 32k         Voice-grade bitrate; plenty for speech, keeps payload small
  -ar 48000        Opus native sample rate; avoids resample warnings
  -ac 1            Mono; Telegram voice notes are always mono
  -application voip  Opus preset tuned for speech
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


class FfmpegMissingError(RuntimeError):
    """Raised when ffmpeg is not on PATH. The container Dockerfile installs it."""


def _ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FfmpegMissingError("ffmpeg not found on PATH")
    return path


def to_ogg_opus(audio_bytes: bytes, *, input_format: str | None = None) -> bytes:
    """Transcode arbitrary ffmpeg-readable audio bytes to OGG/Opus mono 48 kHz.

    ``input_format`` hints ffmpeg's demuxer (``wav``, ``mp3``, ``ogg``). Leaving
    it None lets ffmpeg probe, which is fine for well-formed inputs.
    """
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += [
        "-i", "pipe:0",
        "-c:a", "libopus",
        "-b:a", "32k",
        "-ar", "48000",
        "-ac", "1",
        "-application", "voip",
        "-f", "ogg",
        "pipe:1",
    ]
    proc = subprocess.run(
        cmd, input=audio_bytes, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): {stderr}")
    return proc.stdout


# OpenAI `response_format` -> (ffmpeg output args, Content-Type). ``wav`` is what the engines already
# produce, so it only goes through ffmpeg when a speed change is asked for.
OPENAI_FORMATS: dict[str, tuple[list[str], str]] = {
    "wav": (["-c:a", "pcm_s16le", "-f", "wav"], "audio/wav"),
    "mp3": (["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"], "audio/mpeg"),
    "opus": (["-c:a", "libopus", "-b:a", "64k", "-f", "ogg"], "audio/opus"),
    "flac": (["-c:a", "flac", "-f", "flac"], "audio/flac"),
    "aac": (["-c:a", "aac", "-b:a", "128k", "-f", "adts"], "audio/aac"),
    # OpenAI's pcm is raw signed 16-bit little-endian, 24 kHz, mono.
    "pcm": (["-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "s16le"], "audio/pcm"),
}


def atempo_filter(speed: float) -> str:
    """An ffmpeg ``-af`` chain for ``speed`` (OpenAI allows 0.25-4.0; one atempo covers 0.5-2.0)."""
    parts: list[str] = []
    remaining = speed
    while remaining < 0.5:
        parts.append("atempo=0.5")
        remaining /= 0.5
    while remaining > 2.0:
        parts.append("atempo=2.0")
        remaining /= 2.0
    parts.append(f"atempo={remaining:.4f}")
    return ",".join(parts)


def transcode_openai(
    audio_bytes: bytes, response_format: str, *, speed: float = 1.0, input_format: str | None = "wav",
) -> bytes:
    """Re-encode engine audio to an OpenAI ``response_format`` (and optionally change its speed)."""
    out_args, _ = OPENAI_FORMATS[response_format]
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error"]
    if input_format:
        cmd += ["-f", input_format]
    cmd += ["-i", "pipe:0"]
    if speed != 1.0:
        cmd += ["-af", atempo_filter(speed)]
    cmd += out_args + ["pipe:1"]
    proc = subprocess.run(cmd, input=audio_bytes, capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): {stderr}")
    return proc.stdout
