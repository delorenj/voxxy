"""Sink listener: ``voxxy listen`` — be the machine that vox speaks through.

Run this on the machine you are actually sitting at. It holds an SSE connection
to ``GET /sink/{key}/events`` and plays whatever arrives, so anything anywhere
that can reach the vox API can reach your ears:

    # at your desk
    voxxy listen --key delo-macbook

    # in an ssh session, a zellij pane, a cron job, a systemd agent
    export VOX_SINK=delo-macbook
    voxxy speak "build finished"

Why a server-mediated stream instead of an ssh reverse tunnel: a tunnel dies
with the ssh connection, but a zellij session outlives it, so any port- or
``PULSE_SERVER``-based env in that pane is stale the moment you reconnect. A
sink key is a *stable identity* — set once, correct forever, and reachable from
hosts that have no ssh path back to you at all.

The connection is expected to break (laptop sleeps, wifi flaps, the server
redeploys). That is not an error condition, it is the normal life of a
long-lived stream, so this reconnects with backoff and only gives up when the
key or credentials are actually wrong.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Optional

import typer
from rich.console import Console

from voxxy.client import (
    VoxClient,
    VoxError,
    VoxUnauthorized,
    VoxUnreachable,
    VoxValidationError,
)
from voxxy.config import load_config
from voxxy.playback import PlaybackError, play_encoded, resolve_player

console = Console(stderr=True)

# Backoff bounds for reconnecting. The cap matters more than the floor: an
# unattended listener on a laptop that is asleep for hours must not spend that
# time hammering the API.
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
BACKOFF_FACTOR = 2.0


def register(app: typer.Typer) -> None:
    """Register `listen` on the root app."""
    app.command("listen")(listen)


def register_sink(sink_app: typer.Typer) -> None:
    """Register the `sink` subcommand group."""
    sink_app.command("status")(sink_status)


def default_sink_key() -> str:
    """Derive a sink name from the hostname when nothing is configured.

    Sink keys are limited to ``[A-Za-z0-9._-]``, and a hostname can carry
    characters outside that (and, on macOS, a trailing ``.local``), so it is
    sanitized rather than trusted.
    """
    host = socket.gethostname().split(".")[0] or "voxxy"
    cleaned = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in host)
    cleaned = cleaned.lstrip("-._") or "voxxy"
    return cleaned[:64]


def resolve_key(explicit: Optional[str], cfg_sink: Optional[str]) -> str:
    """Sink key precedence: flag > $VOX_SINK > config > hostname."""
    return explicit or os.environ.get("VOX_SINK") or cfg_sink or default_sink_key()


def listen(
    key: Optional[str] = typer.Option(
        None, "-k", "--key",
        help="Sink name to listen on. Defaults to $VOX_SINK, config.default_sink, "
             "then this machine's hostname.",
    ),
    url: Optional[str] = typer.Option(
        None, "-u", "--url",
        help="Base URL for the vox service. Defaults to $VOX_URL or config.default_url.",
    ),
    player: Optional[str] = typer.Option(
        None, "-P", "--player",
        help="Local audio player binary. Defaults to $VOX_PLAYER or the platform default.",
    ),
    print_only: bool = typer.Option(
        False, "--print-only",
        help="Log incoming utterances without playing them (useful for debugging).",
    ),
    once: bool = typer.Option(
        False, "--once",
        help="Play the first utterance received, then exit. Does not reconnect.",
    ),
) -> None:
    """Play audio that other machines send to this machine's sink.

    Examples:

      voxxy listen                        # sink named after this host
      voxxy listen --key delo-macbook
      voxxy listen --print-only           # see what would play, play nothing
    """
    cfg = load_config()
    base_url = url or os.environ.get("VOX_URL") or cfg.default_url
    sink_key = resolve_key(key, cfg.default_sink)
    player_bin = resolve_player(player)

    console.print(
        f"[bold]voxxy listen[/bold] sink=[cyan]{sink_key}[/cyan] "
        f"url=[cyan]{base_url}[/cyan] player=[cyan]{player_bin}[/cyan]"
    )
    console.print(
        f"[dim]On any other host: export VOX_SINK={sink_key}[/dim]"
    )

    client = VoxClient(base_url)
    backoff = BACKOFF_START

    while True:
        try:
            for event in client.stream_sink_events(sink_key):
                # A successful frame proves the connection is healthy; forget
                # any backoff accumulated by earlier failures.
                backoff = BACKOFF_START
                name = event.get("event")
                data = event.get("data") or {}

                if name == "ready":
                    console.print(
                        f"[green]connected[/green] "
                        f"([cyan]{data.get('listeners', 1)}[/cyan] listener(s) on this sink)"
                    )
                    continue
                if name == "error":
                    console.print(f"[red]server refused: {data.get('message')}[/red]")
                    raise typer.Exit(code=1)
                if name != "audio":
                    continue

                _handle_audio(client, data, player_bin, print_only)
                if once:
                    return

        except (KeyboardInterrupt, typer.Exit):
            raise
        except VoxUnauthorized as exc:
            # Retrying will not fix a wrong key, and a silent retry loop would
            # hide the real problem behind "connecting..." forever.
            console.print(f"[red]{exc}[/red] — check VOX_API_KEY")
            raise typer.Exit(code=1)
        except VoxValidationError as exc:
            console.print(f"[red]sink '{sink_key}' rejected: {exc}[/red]")
            raise typer.Exit(code=2)
        except (VoxUnreachable, VoxError) as exc:
            console.print(f"[yellow]disconnected ({exc})[/yellow]")
        else:
            # Clean end of stream (server restart, deploy). Same treatment.
            console.print("[yellow]stream closed by server[/yellow]")

        if once:
            console.print("[yellow]--once: giving up without an utterance[/yellow]")
            raise typer.Exit(code=1)

        console.print(f"[dim]reconnecting in {backoff:.0f}s…[/dim]")
        time.sleep(backoff)
        backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX)


def _handle_audio(
    client: VoxClient, data: dict, player_bin: str, print_only: bool
) -> None:
    """Fetch and play one utterance. Never fatal — one bad blob is not a reason
    to drop the listener and stop hearing everything after it."""
    audio_url = data.get("audio_url")
    said = (data.get("text") or "").strip()
    label = f'"{said}"' if said else "(no text)"
    voice = data.get("voice") or "?"
    console.print(f"[cyan]♪[/cyan] {label} [dim](voice={voice})[/dim]")

    if print_only or not audio_url:
        if not audio_url:
            console.print("[yellow]  no audio_url in event; skipped[/yellow]")
        return

    try:
        audio = client.fetch_audio(audio_url)
    except VoxError as exc:
        console.print(f"[yellow]  fetch failed: {exc}[/yellow]")
        return

    try:
        play_encoded(audio, player_bin)
    except PlaybackError as exc:
        console.print(f"[yellow]  playback failed: {exc}[/yellow]")


def sink_status(
    key: Optional[str] = typer.Argument(
        None, help="Sink name. Defaults to $VOX_SINK, config.default_sink, then hostname."
    ),
    url: Optional[str] = typer.Option(
        None, "-u", "--url",
        help="Base URL for the vox service. Defaults to $VOX_URL or config.default_url.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show how many machines are listening on a sink."""
    import json as _json

    cfg = load_config()
    base_url = url or os.environ.get("VOX_URL") or cfg.default_url
    sink_key = resolve_key(key, cfg.default_sink)

    client = VoxClient(base_url)
    try:
        status = client.sink_status(sink_key)
    except VoxError as exc:
        if json_out:
            typer.echo(_json.dumps({"key": sink_key, "error": str(exc)}))
        else:
            console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=3)

    if json_out:
        typer.echo(_json.dumps(status.model_dump()))
        return

    if status.listeners:
        console.print(
            f"[green]{status.key}[/green]: "
            f"[cyan]{status.listeners}[/cyan] listener(s)"
        )
    else:
        console.print(
            f"[yellow]{status.key}[/yellow]: nobody listening "
            f"([dim]run 'voxxy listen --key {status.key}' where you are[/dim])"
        )
