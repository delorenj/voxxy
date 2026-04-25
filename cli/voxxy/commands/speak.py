"""Synthesis command: ``voxxy speak``.

Replaces the original ``scripts/vox-speak`` bash script. Preserves its flag
surface exactly so existing ssh-pipeline scripts (``ssh host vox-speak
--raw "hi" | paplay``) keep working once the bash file is swapped to a
shim forwarding here.

Behavior modes (mirroring the bash original):

- **play** (default on TTY stdout): fetch WAV bytes, pipe to the local
  player (default ``paplay`` on Linux, overridable via ``VOX_PLAYER`` or
  ``--player``).
- **raw** (default on non-TTY stdout): write WAV bytes to stdout. Supports
  the classic ``voxxy speak --raw "hi" > out.wav`` and
  ``ssh host voxxy speak --raw "hi" | paplay`` patterns.
- **out FILE**: fetch OGG (via ``/synthesize-url``) and save to file.

``--via HOST`` shells out to ``ssh HOST voxxy speak --raw`` with the text
piped on stdin. This preserves the remote-synth + local-play pattern from
the bash original. The remote side can be any shim that accepts
``--raw``, so old ``vox-speak`` installs on remote hosts still work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from voxxy.client import (
    VoxClient,
    VoxError,
    VoxNotFound,
    VoxUnreachable,
    VoxValidationError,
)
from voxxy.config import load_config

console = Console(stderr=True)  # progress/status → stderr, keep stdout clean for --raw


def register(app: typer.Typer) -> None:
    """Register `speak` on the root app."""
    app.command("speak")(speak)


def speak(
    text: Optional[list[str]] = typer.Argument(
        None,
        help="Text to synthesize. If omitted and stdin is a pipe, reads from stdin.",
    ),
    voice: Optional[str] = typer.Option(
        None, "-v", "--voice",
        help="Voice slug. Defaults to $VOX_VOICE or config.default_voice (rick).",
    ),
    url: Optional[str] = typer.Option(
        None, "-u", "--url",
        help="Base URL for the vox service. Defaults to $VOX_URL or config.default_url.",
    ),
    via: Optional[str] = typer.Option(
        None, "--via",
        help="Synthesize on a remote host via ssh; play locally. "
             "Defaults to $VOX_REMOTE_HOST.",
    ),
    raw: bool = typer.Option(
        False, "-r", "--raw",
        help="Write WAV bytes to stdout (no playback).",
    ),
    play: bool = typer.Option(
        False, "-p", "--play",
        help="Force playback mode. Default is auto: play when stdout is TTY, raw otherwise.",
    ),
    out: Optional[Path] = typer.Option(
        None, "-o", "--out",
        help="Write OGG/Opus audio to this file instead of playing or streaming.",
    ),
    player: Optional[str] = typer.Option(
        None, "-P", "--player",
        help="Local audio player binary. Defaults to $VOX_PLAYER or 'paplay'.",
    ),
    cfg_value: float = typer.Option(2.0, "-c", "--cfg", min=1.0, max=5.0),
    steps: int = typer.Option(10, "-S", "--steps", min=1, max=50),
) -> None:
    """Synthesize speech via the vox service.

    Examples:

      voxxy speak "hello world"
      voxxy speak -v rick "hello world"
      echo "hi" | voxxy speak
      voxxy speak --raw "hi" > out.wav
      voxxy speak --via big-chungus "hi"
      voxxy speak --out /tmp/voice.ogg "hi"
    """
    # Resolve configuration with env overrides (env wins over config, flags win
    # over env). Matches the bash original's precedence.
    cfg = load_config()
    voice_name = voice or os.environ.get("VOX_VOICE") or cfg.default_voice
    base_url = url or os.environ.get("VOX_URL") or cfg.default_url
    via_host = via or os.environ.get("VOX_REMOTE_HOST") or None
    player_bin = player or os.environ.get("VOX_PLAYER") or "paplay"

    # Resolve text: args > stdin (non-TTY) > error.
    if text:
        text_str = " ".join(text).strip()
    elif not sys.stdin.isatty():
        text_str = sys.stdin.read().strip()
    else:
        typer.secho(
            "no text (pass as args or pipe via stdin; use --help for help)",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    if not text_str:
        typer.secho("empty text", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Reject conflicting mode flags.
    modes_set = sum([raw, play, bool(out)])
    if modes_set > 1:
        typer.secho("--raw, --play, and --out are mutually exclusive", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # Resolve mode: explicit flag > auto (TTY = play; non-TTY = raw).
    if out:
        mode = "out"
    elif raw:
        mode = "raw"
    elif play:
        mode = "play"
    else:
        mode = "play" if sys.stdout.isatty() else "raw"

    # --via: delegate WAV fetch to the remote host. Text is piped on stdin so
    # quoting quirks stay the remote's problem, same as the bash original.
    if via_host:
        _speak_via_ssh(via_host, text_str, voice_name, base_url, url is not None, mode, player_bin)
        return

    # Local path.
    client = VoxClient(base_url)
    try:
        if mode == "out":
            _speak_to_file(client, text_str, voice_name, cfg_value, steps, out)
        else:
            wav_bytes = _fetch_wav(client, text_str, voice_name, cfg_value, steps)
            if mode == "raw":
                sys.stdout.buffer.write(wav_bytes)
                sys.stdout.buffer.flush()
            else:
                _play_wav(wav_bytes, player_bin)
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)
    except VoxNotFound as exc:
        typer.secho(f"{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    except VoxValidationError as exc:
        typer.secho(f"server rejected request: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except VoxError as exc:
        typer.secho(f"synth failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_wav(
    client: VoxClient, text: str, voice: str | None,
    cfg_value: float, steps: int,
) -> bytes:
    """POST /synthesize (raw WAV inline). Used for play + raw modes.

    /synthesize-url exists for URL-return cases (Telegram etc.) but for local
    playback we want bytes inline to avoid a second round-trip. Matches the
    bash original's behavior (it hit /synthesize directly).
    """
    return client.synthesize_wav(text=text, voice=voice, cfg=cfg_value, steps=steps)


def _speak_to_file(
    client: VoxClient, text: str, voice: str | None,
    cfg_value: float, steps: int, out: Path,
) -> None:
    """Fetch OGG via /synthesize-url + GET audio_url; write to `out`.

    /synthesize-url is preferred here because it produces the Telegram-ready
    OGG/Opus blob already; saves a ffmpeg transcode on the client side.
    """
    resp = client.synthesize_url(text=text, voice=voice, cfg=cfg_value, steps=steps)
    audio = client.fetch_audio(resp.audio_url)
    out.write_bytes(audio)
    console.print(
        f"[green]wrote[/green] {out} ([cyan]{len(audio)}[/cyan] bytes, "
        f"engine=[cyan]{resp.engine}[/cyan])"
    )


def _play_wav(wav_bytes: bytes, player_bin: str) -> None:
    if not shutil.which(player_bin):
        typer.secho(
            f"{player_bin} not found; use --raw and pipe the WAV yourself",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=127)
    proc = subprocess.run(
        [player_bin],
        input=wav_bytes,
        check=False,
        capture_output=False,
    )
    if proc.returncode != 0:
        typer.secho(f"{player_bin} exited with {proc.returncode}", fg=typer.colors.YELLOW, err=True)


def _speak_via_ssh(
    host: str, text: str, voice: str | None, url: str, url_explicit: bool,
    mode: str, player_bin: str,
) -> None:
    """Remote-synth + local-play pattern.

    Runs ``ssh host voxxy speak --raw [-v VOICE] [-u URL]`` with the text
    on stdin. If the remote host still has the old ``vox-speak`` symlink it
    works too because the shim forwards the same flags.
    """
    if not shutil.which("ssh"):
        typer.secho("missing dependency: ssh", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=127)

    # Default to `vox-speak` on the remote so hosts that haven't migrated to
    # voxxy yet still work (the shim we ship keeps the flag surface). Override
    # via VOX_REMOTE_BIN when all your hosts have voxxy installed, e.g.
    # VOX_REMOTE_BIN="voxxy speak".
    remote_bin = os.environ.get("VOX_REMOTE_BIN", "vox-speak")
    # Allow compound values like "voxxy speak" — split on whitespace.
    remote_cmd = [*remote_bin.split(), "--raw"]
    if voice:
        remote_cmd += ["-v", voice]
    if url_explicit:
        remote_cmd += ["-u", url]

    # printf %q-style quoting: use shlex.quote per token so remote shell sees
    # the right argv. ssh joins argv with spaces into a single remote cmdline.
    import shlex
    remote_cmdline = " ".join(shlex.quote(tok) for tok in remote_cmd)

    ssh = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", host, remote_cmdline],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    assert ssh.stdin is not None and ssh.stdout is not None
    ssh.stdin.write(text.encode("utf-8"))
    ssh.stdin.close()

    wav_bytes = ssh.stdout.read()
    rc = ssh.wait()
    if rc != 0:
        typer.secho(f"remote synth failed (ssh exit {rc})", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=rc)

    if mode == "raw":
        sys.stdout.buffer.write(wav_bytes)
        sys.stdout.buffer.flush()
    elif mode == "out":
        typer.secho("--via with --out not supported; use --raw and redirect", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    else:
        _play_wav(wav_bytes, player_bin)
