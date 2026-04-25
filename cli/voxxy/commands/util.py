"""Top-level leaf commands: health, logs, version.

Intentionally small; each exercises one foundation module. `health` and
`logs` own the "is it up?" and "what's it doing?" muscle.
"""

from __future__ import annotations

import json as _json

import typer
from rich.console import Console
from rich.table import Table

from voxxy import __version__
from voxxy.client import VoxClient, VoxUnreachable
from voxxy.config import load_config
from voxxy.docker import logs_follow

console = Console()


def register(app: typer.Typer) -> None:
    """Register top-level commands onto the passed Typer app."""
    app.command("health")(health)
    app.command("logs")(logs)
    app.command("version")(version)


# version stays cheap; does not hit the server unless --server is passed.
# This preserves a <50ms cold start for the common case.
def version(
    server: bool = typer.Option(False, "-s", "--server", help="Also query /healthz and print server state."),
) -> None:
    """Print the voxxy CLI version (and optionally the server's health)."""
    typer.echo(f"voxxy {__version__}")
    if server:
        cfg = load_config()
        try:
            hc = VoxClient(cfg.default_url).healthz()
        except VoxUnreachable as exc:
            typer.secho(f"server unreachable at {cfg.default_url}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=3)
        typer.echo(f"server {cfg.default_url} status={hc.status} engines={[e.name for e in hc.engines]}")


def health(
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump raw JSON instead of a table."),
) -> None:
    """Hit /healthz and render the per-engine readiness.

    Exit codes: 0 ok, 2 degraded, 3 unreachable.
    """
    cfg = load_config()
    try:
        hc = VoxClient(cfg.default_url).healthz()
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    if as_json:
        typer.echo(_json.dumps(hc.model_dump(), indent=2))
    else:
        table = Table(title=f"vox-tts @ {cfg.default_url}")
        table.add_column("engine", style="cyan")
        table.add_column("ready")
        for e in hc.engines:
            ready_str = "[green]✓[/green]" if e.ready else "[red]✗[/red]"
            table.add_row(e.name, ready_str)
        console.print(table)
        console.print(f"status: [bold]{hc.status}[/bold]")

    if hc.status != "ok":
        raise typer.Exit(code=2)


# logs replaces the current process via os.execvp so Ctrl-C detaches cleanly
# from docker logs -f (no shell-layer buffering, no TTY state mess).
def logs(
    target: str = typer.Argument("core", help="'core' for voxxy-core, or an engine name (voxcpm, vibevoice)."),
) -> None:
    """Tail container logs. 'core' → `vox`, 'voxcpm' → `voxxy-engine-voxcpm`, etc."""
    name = "vox" if target == "core" else f"voxxy-engine-{target}"
    logs_follow(name)
