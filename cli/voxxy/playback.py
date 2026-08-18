"""Local audio playback: turn bytes into sound on whatever machine we are on.

Extracted from ``commands/speak.py`` so ``voxxy listen`` can reuse it verbatim.
Both commands end at the same place -- hand audio to the sound server in front
of the human -- and they must agree on how, or the sink relay would play through
a subtly different path than a direct ``voxxy speak``.

Two wrinkles this module exists to absorb:

- **Not every player reads stdin.** ``paplay``/``pw-play``/``aplay`` do; macOS's
  ``afplay`` takes a path only. The stdin path is preferred where available
  because it avoids a temp file, so we branch on the binary name.
- **Not every player decodes Opus.** ``afplay`` goes through CoreAudio, which has
  no Opus decoder, so the OGG blobs the sink relay hands out are unplayable on a
  Mac as-is. Rather than maintain a per-platform matrix of container support, we
  normalize everything to WAV with ffmpeg first -- ffmpeg is already a hard
  dependency of the voice pipeline, so this costs nothing new.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile

import typer


class PlaybackError(RuntimeError):
    """Raised when audio could not be decoded or played."""


def _is_ssh_session() -> bool:
    """Return True if we appear to be running inside an SSH session."""
    return any(
        var in os.environ
        for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    )


def _pulseaudio_forwarded() -> str | None:
    """Detect a forwarded PulseAudio server common in SSH sessions.

    Returns the ``PULSE_SERVER`` value to use, or *None* if no forwarding
    is detected.  Checks, in order:

    1. ``PULSE_SERVER`` already set in the environment (trust it).
    2. TCP port 4713 open on localhost (the standard PulseAudio TCP port
       often forwarded with ``ssh -R 4713:localhost:4713``).
    """
    if os.environ.get("PULSE_SERVER"):
        return os.environ["PULSE_SERVER"]

    if not _is_ssh_session():
        return None

    try:
        with socket.create_connection(("127.0.0.1", 4713), timeout=0.3):
            return "127.0.0.1:4713"
    except OSError:
        pass

    return None


# Players that accept a raw WAV stream on stdin with no arguments. Anything else
# (afplay on macOS, etc.) is handed a temp file path instead.
_STDIN_WAV_PLAYERS = {"paplay", "pw-play", "aplay"}


def _default_player() -> str:
    """Platform-appropriate default audio player.

    macOS ships ``afplay`` (CoreAudio, needs no sound server); Linux desktops
    have ``paplay`` (PulseAudio/PipeWire). ``$VOX_PLAYER`` overrides either.
    """
    if sys.platform == "darwin":
        return "afplay"
    return "paplay"


def resolve_player(explicit: str | None = None) -> str:
    """Resolve the player binary: explicit flag > $VOX_PLAYER > platform default."""
    return explicit or os.environ.get("VOX_PLAYER") or _default_player()


def _play_wav_via_file(wav_bytes: bytes, player_bin: str) -> None:
    """Play via a temp WAV file, for players that can't read stdin (afplay).

    No PulseAudio env dance — these players talk to the OS audio stack directly.
    """
    tmp = tempfile.NamedTemporaryFile(prefix="voxxy-", suffix=".wav", delete=False)
    try:
        tmp.write(wav_bytes)
        tmp.flush()
        tmp.close()
        proc = subprocess.run([player_bin, tmp.name], check=False)
        if proc.returncode != 0:
            typer.secho(
                f"{player_bin} exited with {proc.returncode}",
                fg=typer.colors.YELLOW, err=True,
            )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _play_wav(wav_bytes: bytes, player_bin: str) -> None:
    if not shutil.which(player_bin):
        typer.secho(
            f"{player_bin} not found; use --raw and pipe the WAV yourself",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=127)

    # File-based players (afplay, ...) can't consume stdin: write a temp WAV and
    # pass its path. The stdin + PulseAudio path below is for paplay and friends.
    if os.path.basename(player_bin) not in _STDIN_WAV_PLAYERS:
        _play_wav_via_file(wav_bytes, player_bin)
        return

    env = os.environ.copy()
    pa_server = _pulseaudio_forwarded()
    if pa_server:
        env["PULSE_SERVER"] = pa_server
    elif env.get("PULSE_SERVER") == "":
        # An empty PULSE_SERVER breaks libpulse ("Invalid server"). Drop it so
        # paplay falls back to its default discovery (X11, autospawn, etc.).
        env.pop("PULSE_SERVER", None)

    proc = subprocess.run(
        [player_bin],
        input=wav_bytes,
        check=False,
        capture_output=False,
        env=env,
    )

    # A non-zero exit while PULSE_SERVER points at a *remote* server (e.g. a
    # stale `export PULSE_SERVER=tcp:host:4713` from a forwarding session whose
    # host is now offline) is almost always "Connection refused". When we're not
    # in an SSH session there's a working local sound server right here, so retry
    # once with PULSE_SERVER stripped to let libpulse find the local socket.
    if (
        proc.returncode != 0
        and not _is_ssh_session()
        and env.get("PULSE_SERVER")
    ):
        local_env = env.copy()
        local_env.pop("PULSE_SERVER", None)
        local_env.pop("PULSE_SINK", None)
        retry = subprocess.run(
            [player_bin],
            input=wav_bytes,
            check=False,
            capture_output=False,
            env=local_env,
        )
        if retry.returncode == 0:
            typer.secho(
                f"note: PULSE_SERVER={env['PULSE_SERVER']} was unreachable; "
                "played on the local sound server instead.",
                fg=typer.colors.YELLOW, err=True,
            )
            return
        proc = retry

    if proc.returncode != 0:
        if _is_ssh_session() and not _pulseaudio_forwarded():
            typer.secho(
                f"{player_bin} failed (exit {proc.returncode}). "
                "This shell is an SSH session, so there are no speakers here.\n"
                "Route the audio to the machine you're actually sitting at:\n"
                "  • On that machine:  voxxy listen --key <name>\n"
                "  • Here:             export VOX_SINK=<name>\n"
                "Or forward a sound server the old way:\n"
                "  • ssh -R 4713:localhost:4713 <host>, then\n"
                "    export PULSE_SERVER=127.0.0.1:4713",
                fg=typer.colors.YELLOW, err=True,
            )
        else:
            typer.secho(
                f"{player_bin} exited with {proc.returncode}",
                fg=typer.colors.YELLOW, err=True,
            )


def decode_to_wav(audio_bytes: bytes) -> bytes:
    """Decode arbitrary encoded audio (OGG/Opus, MP3, ...) to a WAV stream.

    The sink relay ships OGG/Opus because that is what the audio cache already
    stores and what Telegram wants. CoreAudio cannot decode Opus, so on macOS
    ``afplay`` would simply refuse the blob. Normalizing to WAV here keeps one
    playback path for every platform and every source format.
    """
    if not shutil.which("ffmpeg"):
        raise PlaybackError(
            "ffmpeg not found; it is required to decode sink audio "
            "(macOS: brew install ffmpeg)"
        )
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0", "-f", "wav", "pipe:1"],
        input=audio_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise PlaybackError(f"ffmpeg failed to decode audio: {detail}")
    return proc.stdout


def play_encoded(audio_bytes: bytes, player_bin: str) -> None:
    """Decode then play. Entry point for anything that isn't already WAV."""
    _play_wav(decode_to_wav(audio_bytes), player_bin)
