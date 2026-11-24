"""`voxxy setup`: make this host a Voxxy client in one idempotent step.

The client role, which is what every desk needs (the server role is `voxxy
daemon install`, on a GPU host only). It records, in ~/.config/voxxy/config.toml:

- `default_url`: the Voxxy server (the hosted one, or your own);
- `api_key_ref`: an op:// or file:<path>[#NAME] reference to this host's key (never the value);

then proves the pair works (health, then an authenticated voice list) and, with
`--speak-here`, makes this desk a speaker (`voxxy sink on`).

`heyma setup voxxy` calls this and records the role in host.toml; HeyMa never
writes Voxxy's config itself (the joining-HeyMa contract). Exit 0 when the host
can speak, 2 when it is set up but degraded (server degraded, or no key yet),
3 when the server is unreachable or rejects the key.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import typer
from rich.console import Console

from voxxy.client import VoxClient, VoxError, VoxUnauthorized, VoxUnreachable, env_api_key, is_key_ref
from voxxy.config import load_config, save_config

console = Console(stderr=True)


def register(app: typer.Typer) -> None:
    app.command("setup")(setup)


def setup(
    url: Optional[str] = typer.Option(None, "-u", "--url",
                                      help="Voxxy server. Default: the configured one."),
    key_ref: Optional[str] = typer.Option(None, "--key-ref",
                                          help="op:// or file:<path>[#NAME] reference to this host's API key. Default: keep the configured one."),
    speak_here: Optional[bool] = typer.Option(None, "--speak-here/--no-speak-here",
                                              help="Also make (or stop making) this desk a speaker. Default: leave as is."),
    json_out: bool = typer.Option(False, "--json", help="Emit one JSON report."),
) -> None:
    """Make this host a Voxxy client: server URL, key reference, optional desk speaker."""
    report = run_setup(url, key_ref, speak_here)
    if json_out:
        typer.echo(json.dumps(report))
    else:
        for step in report["steps"]:
            colour = {"ok": "green", "warn": "yellow", "fail": "red"}[step["state"]]
            console.print(f"[{colour}]{step['state']:>4}[/{colour}]  {step['name']}: {step['detail']}")
    raise typer.Exit(code=report["exit"])


def run_setup(url: Optional[str], key_ref: Optional[str], speak_here: Optional[bool],
              client_factory=VoxClient) -> dict:
    steps: list[dict] = []

    def step(name: str, state: str, detail: str) -> None:
        steps.append({"name": name, "state": state, "detail": detail})

    cfg = load_config()
    changed = False
    if url and url.rstrip("/") != cfg.default_url:
        cfg.default_url, changed = url.rstrip("/"), True
    if key_ref and key_ref != cfg.api_key_ref:
        if not is_key_ref(key_ref):
            raise typer.BadParameter(
                "--key-ref takes an op:// or file:<path>[#NAME] reference, never the key itself")
        cfg.api_key_ref, changed = key_ref, True
    if changed:
        save_config(cfg)
    step("config", "ok", f"server {cfg.default_url}; key ref "
         f"{'set' if cfg.api_key_ref else 'not set'}{' (written)' if changed else ''}")

    base = os.environ.get("VOX_URL") or cfg.default_url
    exit_code = 0
    try:
        health = client_factory(base, api_key=None).healthz()
        degraded = getattr(health, "status", "ok") != "ok"
        step("server", "warn" if degraded else "ok", f"{base} {getattr(health, 'status', 'ok')}")
        exit_code = 2 if degraded else 0
    except VoxError as exc:
        step("server", "fail", f"{base} unreachable: {exc}")
        return {"steps": steps, "exit": 3, "url": base, "key": "unknown", "speaker": None}

    key_state = "missing"
    if env_api_key():
        try:
            voices = client_factory(base).list_voices()
            key_state = "ok"
            step("key", "ok", f"accepted ({len(voices)} voices)")
        except VoxUnauthorized:
            key_state = "rejected"
            step("key", "fail", "the server rejected this host's key")
            exit_code = 3
        except VoxUnreachable as exc:
            key_state = "unknown"
            step("key", "fail", f"could not check: {exc}")
            exit_code = 3
        except VoxError as exc:
            # An open server (no VOX_API_KEY) answers without a key too; anything else is real.
            key_state = "unknown"
            step("key", "warn", f"could not check: {exc}")
            exit_code = max(exit_code, 2)
    else:
        step("key", "warn", "no key: set --key-ref op://... (or $VOX_API_KEY); synthesis will be refused")
        exit_code = max(exit_code, 2)

    speaker = None
    from voxxy.commands import desk, listen

    try:
        if speak_here is True:
            speaker = desk.turn_on(listen.default_sink_key(), desk.env_url(cfg.default_url))
        elif speak_here is False:
            speaker = desk.turn_off()
        else:
            speaker = desk.local_state()
        step("speaker", "ok", f"speaking on this desk as {speaker['key']}" if speaker["active"]
             else "this desk is not a speaker (voxxy sink on)")
    except desk.DeskError as exc:
        step("speaker", "warn", str(exc))
        exit_code = max(exit_code, 2)
    return {"steps": steps, "exit": exit_code, "url": base, "key": key_state, "speaker": speaker}
