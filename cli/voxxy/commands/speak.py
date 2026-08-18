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
- **sink**: in play mode, when a sink is configured *and* something is
  listening on it, the audio is delivered to that machine instead of this
  one's sound card. This is the answer to "I'm ssh'd into the server and I
  want to hear this at my desk": set ``VOX_SINK=<name>`` once in the remote
  shell's rc and every ``voxxy speak`` there follows you home. ``--raw`` and
  ``--out`` produce bytes for the caller, so they ignore the sink entirely.

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
# Re-exported (underscore names included) so `from voxxy.commands.speak import
# _play_wav` keeps working for callers and tests written before the split.
from voxxy.playback import (  # noqa: F401
    _STDIN_WAV_PLAYERS,
    _default_player,
    _is_ssh_session,
    _play_wav,
    _play_wav_via_file,
    _pulseaudio_forwarded,
    PlaybackError,
    play_encoded,
    resolve_player,
)

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
    sink: Optional[str] = typer.Option(
        None, "-s", "--sink",
        help="Play on the machine listening on this sink instead of here. "
             "Defaults to $VOX_SINK or config.default_sink.",
    ),
    no_sink: bool = typer.Option(
        False, "--no-sink",
        help="Ignore any configured sink and play on this machine's speakers.",
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
      voxxy speak --sink delo-macbook "hi"   # play on the machine at my desk
    """
    # Resolve configuration with env overrides (env wins over config, flags win
    # over env). Matches the bash original's precedence.
    cfg = load_config()
    voice_name = voice or os.environ.get("VOX_VOICE") or cfg.default_voice
    base_url = url or os.environ.get("VOX_URL") or cfg.default_url
    via_host = via or os.environ.get("VOX_REMOTE_HOST") or None
    player_bin = resolve_player(player)
    sink_key = None if no_sink else (
        sink or os.environ.get("VOX_SINK") or cfg.default_sink or None
    )

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
        elif mode == "play" and sink_key and _speak_to_sink(
            client, sink_key, text_str, voice_name, cfg_value, steps, player_bin
        ):
            pass  # delivered to (or played on behalf of) the sink
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


def _speak_to_sink(
    client: VoxClient, sink_key: str, text: str, voice: str | None,
    cfg_value: float, steps: int, player_bin: str,
) -> bool:
    """Try to deliver this utterance to `sink_key`. Return False to play locally.

    Probing listener count *before* synthesizing is what keeps this cheap: with
    a listener we go down the OGG-to-cache path (the sink fetches the URL); with
    none we return False untouched and the caller takes the normal inline-WAV
    path. Either way the text is synthesized exactly once.

    A sink that goes away between the probe and the publish is the one case that
    still costs us: we already have the OGG, so we fetch and play it here rather
    than re-synthesizing.

    Never fatal. A broken sink must degrade to "you heard it on the wrong
    machine", never to "you didn't hear it at all".
    """
    try:
        status = client.sink_status(sink_key)
    except VoxError as exc:
        console.print(f"[yellow]sink {sink_key} unreachable ({exc}); playing here[/yellow]")
        return False

    if status.listeners == 0:
        console.print(
            f"[yellow]nothing listening on sink '{sink_key}'; playing here[/yellow]"
        )
        return False

    resp = client.synthesize_url(text=text, voice=voice, cfg=cfg_value, steps=steps)
    result = client.sink_play(
        sink_key, resp.audio_url,
        text=text, voice=voice, engine=resp.engine, duration_s=resp.duration_s,
    )

    if result.delivered > 0:
        where = f"{result.delivered} listeners" if result.delivered > 1 else "listener"
        console.print(
            f"[green]→[/green] {sink_key} ({where}, engine=[cyan]{resp.engine}[/cyan])"
        )
        return True

    # Lost the race with a disconnecting listener. Don't waste the synthesis.
    console.print(
        f"[yellow]sink '{sink_key}' dropped before delivery; playing here[/yellow]"
    )
    try:
        play_encoded(client.fetch_audio(resp.audio_url), player_bin)
    except PlaybackError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    return True


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
