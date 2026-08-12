from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from voxxy.client import (
    SynthUrlResponse,
    VoxClient,
    VoxError,
    VoxNotFound,
    VoxServerError,
    VoxUnauthorized,
    VoxUnreachable,
    VoxValidationError,
)

DEFAULT_URL = "https://vox.delo.sh"
SUPPORTED_FORMATS = frozenset({"wav", "ogg", "opus", "mp3", "flac"})
OGGISH_FORMATS = frozenset({"ogg", "opus"})
TRANSCODE_FORMATS = frozenset({"mp3", "flac"})


class MissingFfmpegError(RuntimeError):
    """Raised when ffmpeg is required but unavailable."""


class TranscodeError(RuntimeError):
    """Raised when ffmpeg cannot transcode Voxxy's WAV output."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxxy-http-tts",
        description="Automation-friendly Voxxy HTTP wrapper.",
        exit_on_error=False,
    )
    parser.add_argument("--text", help="Text to synthesize. If omitted, reads --text-file or stdin.")
    parser.add_argument("--text-file", type=Path, help="Read synthesis text from a UTF-8 file.")
    parser.add_argument("--voice", help="Voice slug. Defaults to VOX_VOICE.")
    parser.add_argument("--url", help=f"Voxxy base URL. Defaults to VOX_URL or {DEFAULT_URL}.")
    parser.add_argument("--api-key", help="API key. Defaults to VOX_API_KEY.")
    parser.add_argument("--format", choices=sorted(SUPPORTED_FORMATS), help="Output format. Defaults to --out suffix or wav.")
    parser.add_argument("--cfg", type=float, default=2.0, help="CFG value for synthesis (default: 2.0).")
    parser.add_argument("--steps", type=int, default=10, help="Diffusion steps for synthesis (default: 10).")
    parser.add_argument("--out", type=Path, required=True, help="Output file path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable metadata JSON to stdout.")
    return parser


def _read_text(*, text: str | None, text_file: Path | None, stdin: Any) -> str:
    if text is not None and text_file is not None:
        raise ValueError("--text and --text-file are mutually exclusive")

    if text is not None:
        payload = text.strip()
    elif text_file is not None:
        payload = text_file.read_text(encoding="utf-8").strip()
    elif not stdin.isatty():
        payload = stdin.read().strip()
    else:
        raise ValueError("no text provided (use --text, --text-file, or stdin)")

    if not payload:
        raise ValueError("text is empty")
    return payload


def _normalize_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported format: {value}")
    return normalized


def _resolve_output_format(requested: str | None, out: Path) -> str:
    if requested:
        return _normalize_format(requested)

    suffix = out.suffix.lower().lstrip(".")
    if suffix in SUPPORTED_FORMATS:
        return suffix
    return "wav"


def _write_output(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _require_ffmpeg_binary() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise MissingFfmpegError(
            "ffmpeg not found on PATH. Install ffmpeg or request wav/ogg/opus output instead."
        )
    return binary


def _transcode_wav_bytes(wav_bytes: bytes, output_format: str) -> bytes:
    ffmpeg_binary = _require_ffmpeg_binary()
    if output_format == "mp3":
        cmd = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "wav",
            "-i",
            "pipe:0",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-f",
            "mp3",
            "pipe:1",
        ]
    elif output_format == "flac":
        cmd = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "wav",
            "-i",
            "pipe:0",
            "-c:a",
            "flac",
            "-f",
            "flac",
            "pipe:1",
        ]
    else:
        raise ValueError(f"unsupported transcode format: {output_format}")

    proc = subprocess.run(cmd, input=wav_bytes, capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise TranscodeError(stderr or f"ffmpeg exited with status {proc.returncode}")
    return proc.stdout


def _metadata(
    *,
    out: Path,
    output_format: str,
    audio_bytes: bytes,
    voice: str | None,
    mode: str,
    synth: SynthUrlResponse | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(out),
        "format": output_format,
        "voice": voice,
        "bytes": len(audio_bytes),
        "mode": mode,
    }
    if synth is not None:
        payload["engine"] = synth.engine
        payload["audio_url"] = synth.audio_url
        if synth.duration_s is not None:
            payload["duration_s"] = synth.duration_s
    return payload


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except argparse.ArgumentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not 1.0 <= args.cfg <= 5.0:
        print("error: --cfg must be between 1.0 and 5.0", file=sys.stderr)
        return 2
    if not 1 <= args.steps <= 50:
        print("error: --steps must be between 1 and 50", file=sys.stderr)
        return 2

    try:
        text = _read_text(text=args.text, text_file=args.text_file, stdin=sys.stdin)
        output_format = _resolve_output_format(args.format, args.out)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    base_url = (args.url or os.environ.get("VOX_URL") or DEFAULT_URL).strip().rstrip("/")
    voice = args.voice or os.environ.get("VOX_VOICE") or None
    api_key = args.api_key if args.api_key is not None else os.environ.get("VOX_API_KEY")

    client = VoxClient(base_url, api_key=api_key)
    try:
        if output_format == "wav":
            audio_bytes = client.synthesize_wav(text=text, voice=voice, cfg=args.cfg, steps=args.steps)
            _write_output(args.out, audio_bytes)
            result = _metadata(
                out=args.out,
                output_format=output_format,
                audio_bytes=audio_bytes,
                voice=voice,
                mode="synthesize-wav",
            )
        elif output_format in OGGISH_FORMATS:
            synth = client.synthesize_url(text=text, voice=voice, cfg=args.cfg, steps=args.steps)
            audio_bytes = client.fetch_audio(synth.audio_url)
            _write_output(args.out, audio_bytes)
            result = _metadata(
                out=args.out,
                output_format=output_format,
                audio_bytes=audio_bytes,
                voice=voice,
                mode="synthesize-url-fetch",
                synth=synth,
            )
        elif output_format in TRANSCODE_FORMATS:
            wav_bytes = client.synthesize_wav(text=text, voice=voice, cfg=args.cfg, steps=args.steps)
            audio_bytes = _transcode_wav_bytes(wav_bytes, output_format)
            _write_output(args.out, audio_bytes)
            result = _metadata(
                out=args.out,
                output_format=output_format,
                audio_bytes=audio_bytes,
                voice=voice,
                mode="synthesize-wav-transcode",
            )
        else:
            print(f"error: unsupported format: {output_format}", file=sys.stderr)
            return 2
    except VoxUnauthorized as exc:
        print(f"authentication failed: {exc}. Set VOX_API_KEY or pass --api-key.", file=sys.stderr)
        return 1
    except VoxUnreachable as exc:
        print(f"unreachable: {exc}", file=sys.stderr)
        return 3
    except (MissingFfmpegError, TranscodeError) as exc:
        print(f"audio output failed: {exc}", file=sys.stderr)
        return 1
    except (VoxNotFound, VoxValidationError, VoxServerError, VoxError) as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
