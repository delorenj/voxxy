"""``voxxy license``: show or add the self-hosted edition's license key.

The key (``VS1.…``) comes by email after purchase. The server checks it offline and keeps
it on its own data volume, so these commands only talk to the server; they never store the
key on this machine. Voxxy works without a key; the license covers 12 months of new versions.

Exit codes: 0 licensed, 2 no key / updates ended, 3 server unreachable or key refused, 4 hosted edition
(no license there), 5 the key was refused.
"""

from __future__ import annotations

import json as _json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from voxxy.client import VoxClient, VoxNotFound, VoxUnauthorized, VoxUnreachable, VoxValidationError
from voxxy.config import load_config

console = Console()

_STATE_EXIT = {"valid": 0, "updates_ended": 2, "missing": 2, "invalid": 5}


def register(license_app: typer.Typer) -> None:
    license_app.command("show")(show)
    license_app.command("import")(import_key)


def _date(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "-"


def _client() -> VoxClient:
    return VoxClient(load_config().default_url)


def _render(status: dict, as_json: bool) -> int:
    if as_json:
        typer.echo(_json.dumps(status, indent=2))
    else:
        colour = {"valid": "green", "updates_ended": "yellow", "missing": "yellow"}.get(status["state"], "red")
        console.print(f"[{colour}]{status['state']}[/{colour}]  {status['detail']}")
        if status.get("license_id"):
            console.print(f"license {status['license_id']}  updates until {_date(status.get('updates_until'))}")
    return _STATE_EXIT.get(status["state"], 5)


def _call(fn):
    try:
        return fn()
    except VoxUnreachable as exc:
        typer.secho(f"vox service unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(3)
    except VoxUnauthorized:
        typer.secho("the server refused this machine's API key; run `voxxy setup`", fg=typer.colors.RED, err=True)
        raise typer.Exit(3)
    except VoxNotFound:
        typer.secho("this Voxxy server has no license (the hosted edition does not use one)", err=True)
        raise typer.Exit(4)


def show(as_json: bool = typer.Option(False, "-j", "--json", help="Print the status as JSON.")) -> None:
    """Show the license state: valid, missing, invalid, or valid with updates ended."""
    raise typer.Exit(_render(_call(lambda: _client().license_status()), as_json))


def import_key(
    key: str = typer.Argument(..., help="The license key (VS1.…), a file holding it, or - for stdin."),
    as_json: bool = typer.Option(False, "-j", "--json", help="Print the result as JSON."),
) -> None:
    """Add or replace the license key. The server checks it first and keeps it only if it is valid."""
    if key == "-":
        key = sys.stdin.read()
    elif not key.strip().startswith("VS1.") and Path(key).expanduser().is_file():
        key = Path(key).expanduser().read_text()
    try:
        status = _call(lambda: _client().set_license(key.strip()))
    except VoxValidationError as exc:
        m = re.search(r'"detail"\s*:\s*"([^"]*)"', str(exc))
        typer.secho(m.group(1) if m else str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(5)
    raise typer.Exit(_render(status, as_json))
