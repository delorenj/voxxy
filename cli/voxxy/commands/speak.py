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

If ``VOX_API_KEY`` is set, the underlying HTTP client automatically sends both
Bearer and ``X-API-Key`` headers so secured Voxxy deployments keep working
without extra speak-specific flags.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
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
    player_bin = player or os.environ.get("VOX_PLAYER") or _default_player()

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

    # A --via target that resolves to *this* machine would ssh into ourselves —
    # the common VOX_REMOTE_HOST=<this box's own name/IP> misconfig (e.g. a shell
    # rc exporting the LAN IP that happens to be local). That's pointless and it
    # breaks --out. Fall through to the direct local path instead.
    if via_host and _via_is_local(via_host):
        via_host = None

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

def _via_is_local(host: str) -> bool:
    """True when a ``--via`` target refers to this machine.

    ``VOX_REMOTE_HOST`` is meant to point at the box that runs the vox stack, for
    use *from another machine*. On the stack host itself the shell rc may still
    export it (e.g. ``VOX_REMOTE_HOST=<this box's LAN IP>``), which would make
    ``--via`` ssh into ourselves. We short-circuit that to the direct local path.
    """
    if not host:
        return False
    target = host.strip()
    if "@" in target:  # strip an ssh-style user@host prefix
        target = target.split("@", 1)[1]
    if target.lower() in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        if target.lower() in {socket.gethostname().lower(), socket.getfqdn().lower()}:
            return True
    except OSError:
        pass
    # Resolve the target, then ask the kernel which source address it would use
    # to reach each candidate: for a local address the source equals the address
    # itself. UDP connect() only sets the route — no packets leave the host.
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(target, None)}
    except OSError:
        return False
    for addr in addrs:
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        try:
            probe = socket.socket(family, socket.SOCK_DGRAM)
            try:
                probe.connect((addr, 9))
                if probe.getsockname()[0] == addr:
                    return True
            finally:
                probe.close()
        except OSError:
            continue
    return False


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
                "Audio playback inside an SSH session requires PulseAudio forwarding.\n"
                "Quick fixes:\n"
                "  • ssh -X <host>                     # X11 forwarding often carries audio\n"
                "  • ssh -R 4713:localhost:4713 <host>  # forward PA TCP, then set:\n"
                "    export PULSE_SERVER=127.0.0.1:4713\n"
                "  • From your local machine instead:\n"
                "    ssh <host> voxxy speak --raw 'text' | paplay\n"
                "    voxxy speak --via <host> 'text'",
                fg=typer.colors.YELLOW, err=True,
            )
        else:
            typer.secho(
                f"{player_bin} exited with {proc.returncode}",
                fg=typer.colors.YELLOW, err=True,
            )


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
