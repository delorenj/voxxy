"""Engine control commands.

Read-only in Phase 2: `list` and `logs`.
Mutating commands (`use`, `enable`, `disable`) added in Phase 3 (T3.4).

Engine URL resolution uses a hard-coded map for the two known local engines
(voxcpm, vibevoice). ElevenLabs is always appended in-core and is never a
valid target for `use`/`enable`/`disable`. The map is intentionally small and
explicit; a v2 could auto-discover from compose.engines.yml if a third engine
is added.

VOX_ENGINES string format (mirrors compose.yml convention):
  "name1=http://host:port,name2=http://host:port"
Order matters: core tries engines left-to-right, first success wins.

Design notes for reorder logic:
- `use <name>`: moves <name> to position 0; preserves relative order of others.
- `enable <name>`: appends <name> if absent; no-op if already present.
- `disable <name>`: drops <name>; errors if it's the only local engine left
  (without --force) to prevent cost-bearing ElevenLabs-only operation.
- All three save state then recreate core with the new VOX_ENGINES env.
- Polling after recreate checks /healthz; for `use` it also verifies the
  primary engine name matches. For `enable`/`disable` it checks core is up.
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from voxxy.client import VoxClient, VoxNotFound, VoxUnreachable, VoxServerError
from voxxy.config import discover_project_root, load_config
from voxxy.docker import DockerError, compose_up, ensure_op_authed, logs_follow
from voxxy.state import State, load_state, save_state

console = Console()

# Compose-default VOX_ENGINES value (mirrors compose.yml).
DEFAULT_VOX_ENGINES = (
    "voxcpm=http://voxxy-engine-voxcpm:8000,"
    "vibevoice=http://voxxy-engine-vibevoice:8000"
)

# Hard-coded URL map for the two known local engine sidecars.
ENGINE_URLS: dict[str, str] = {
    "voxcpm": "http://voxxy-engine-voxcpm:8000",
    "vibevoice": "http://voxxy-engine-vibevoice:8000",
}

# Names that are NOT valid targets for use/enable/disable (managed by core in-process).
INTERNAL_ENGINE_NAMES = {"elevenlabs"}


def register(engine_app: typer.Typer) -> None:
    """Register leaf commands on the engine sub-app."""
    engine_app.command("list")(list_engines)
    engine_app.command("logs")(engine_logs)
    engine_app.command("use")(engine_use)
    engine_app.command("enable")(engine_enable)
    engine_app.command("disable")(engine_disable)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_vox_engines(engines_str: str) -> list[tuple[str, str]]:
    """Parse a VOX_ENGINES string into a list of (name, url) pairs.

    Ignores blank entries from trailing commas.
    """
    pairs: list[tuple[str, str]] = []
    for entry in engines_str.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            name, _, url = entry.partition("=")
            pairs.append((name.strip(), url.strip()))
        # Entries without '=' are silently skipped (corrupt state guard).
    return pairs


def _render_vox_engines(pairs: list[tuple[str, str]]) -> str:
    """Serialise (name, url) pairs back to the VOX_ENGINES string format."""
    return ",".join(f"{name}={url}" for name, url in pairs)


def _current_engines(project_root) -> list[tuple[str, str]]:
    """Return the current engine chain from state (or compose default)."""
    state = load_state(project_root)
    src = state.vox_engines or DEFAULT_VOX_ENGINES
    return _parse_vox_engines(src)


def _reorder_engines(
    current: list[tuple[str, str]],
    action: str,
    name: str,
    *,
    force: bool = False,
) -> list[tuple[str, str]]:
    """Pure reorder function — no I/O, no side effects.

    Returns the new (name, url) list after applying ``action``.

    Actions:
      ``use``     — move ``name`` to position 0; preserve relative order of
                    others. If ``name`` is absent from ``current`` it is added
                    at position 0 using ENGINE_URLS.
      ``enable``  — append ``name`` if absent; no-op (return same list) if
                    already present.
      ``disable`` — drop ``name``; raises ValueError if it is the only entry
                    and ``force=False``, to prevent an empty local chain.

    Raises:
        ValueError: when ``disable`` would leave an empty chain and
                    ``force=False``, or when the engine URL is unknown.
        KeyError:   when ``name`` is not in ENGINE_URLS and needs a URL
                    (i.e. during ``use`` when absent, or ``enable``).
    """
    if action == "use":
        url = ENGINE_URLS.get(name)
        if url is None and name not in dict(current):
            raise KeyError(f"unknown engine '{name}'")
        resolved_url = url or dict(current)[name]
        new = [(name, resolved_url)]
        for n, u in current:
            if n != name:
                new.append((n, u))
        return new

    if action == "enable":
        current_names = [n for n, _ in current]
        if name in current_names:
            return list(current)
        url = ENGINE_URLS.get(name)
        if url is None:
            raise KeyError(f"unknown engine '{name}'")
        return list(current) + [(name, url)]

    if action == "disable":
        new = [(n, u) for n, u in current if n != name]
        if not new and not force:
            raise ValueError(
                f"Refusing to disable '{name}': it is the only local engine in the "
                "chain. Pass --force to override."
            )
        return new

    raise ValueError(f"unknown action '{action}'")


def _url_for(name: str) -> str:
    """Resolve the internal URL for a named engine.

    Raises typer.Exit(1) if the name is not in the known map, since we can't
    construct a valid URL for an unknown engine.
    """
    if name in ENGINE_URLS:
        return ENGINE_URLS[name]
    known = ", ".join(sorted(ENGINE_URLS))
    typer.secho(
        f"Unknown engine '{name}'. Known local engines: {known}",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(code=1)


def _recreate_core(
    project_root,
    new_engines_str: str,
    action: str,
) -> None:
    """Save state, then recreate the core container with the new VOX_ENGINES.

    Separated from individual commands so state + recreate stay atomic.
    """
    state = load_state(project_root)
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_state = State(
        vox_engines=new_engines_str,
        last_engine_change=now_iso,
        last_engine_change_by=action,
    )
    save_state(project_root, new_state)
    console.print(f"  [dim]State written: {project_root}/.voxxy.state.json[/dim]")

    try:
        ensure_op_authed()
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    console.print("  [dim]Recreating voxxy-core with new VOX_ENGINES...[/dim]")
    try:
        compose_up(
            project_root,
            full=False,   # Only core; engines keep running.
            services=["vox"],
            recreate=True,
            env={"VOX_ENGINES": new_engines_str},
            no_build=False,
        )
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


def _poll_primary(
    client: VoxClient,
    *,
    expected_primary: Optional[str] = None,
    timeout: int = 60,
) -> bool:
    """Poll /healthz until core is up (and optionally until primary matches).

    Returns True on success, False on timeout.
    """
    deadline = time.monotonic() + timeout
    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while time.monotonic() < deadline:
        try:
            hc = client.healthz()
            if expected_primary:
                if hc.engines and hc.engines[0].name == expected_primary:
                    target = next(
                        (e for e in hc.engines if e.name == expected_primary), None
                    )
                    if target and target.ready:
                        print()
                        return True
            else:
                # Just need any response (core is up).
                print()
                return True
        except (VoxUnreachable, VoxServerError, VoxNotFound):
            # 502/503/404 while core is restarting (Traefik may not have re-registered
            # the new container yet); keep polling.
            pass

        ch = spinner_chars[i % len(spinner_chars)]
        print(f"\r  {ch} Waiting for /healthz …", end="", flush=True)
        i += 1
        time.sleep(2)

    print()
    return False


# ---------------------------------------------------------------------------
# Read-only commands (Phase 2)
# ---------------------------------------------------------------------------

def list_engines(
    as_json: bool = typer.Option(False, "-j", "--json", help="Dump raw JSON."),
) -> None:
    """List configured engines with readiness state."""
    cfg = load_config()
    try:
        hc = VoxClient(cfg.default_url).healthz()
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    if as_json:
        typer.echo(_json.dumps(hc.model_dump(), indent=2))
        return

    table = Table(title="Engines")
    table.add_column("name", style="cyan")
    table.add_column("ready")
    for e in hc.engines:
        ready_str = "[green]✓[/green]" if e.ready else "[red]✗[/red]"
        table.add_row(e.name, ready_str)
    console.print(table)


def engine_logs(
    name: str = typer.Argument(..., help="Engine name (voxcpm, vibevoice, ...)."),
) -> None:
    """Tail the named engine's container logs."""
    logs_follow(f"voxxy-engine-{name}")


# ---------------------------------------------------------------------------
# Mutating commands (Phase 3 — T3.4)
# ---------------------------------------------------------------------------

def engine_use(
    name: str = typer.Argument(..., help="Engine to promote to primary position."),
) -> None:
    """Reorder the engine chain so <name> is tried first.

    Validates the engine exists in /healthz, updates .voxxy.state.json, and
    recreates voxxy-core to pick up the new VOX_ENGINES order. Polls /healthz
    until the named engine is confirmed as primary (position 0) and ready.
    """
    if name in INTERNAL_ENGINE_NAMES:
        typer.secho(
            f"'{name}' is managed internally by core and cannot be set as primary. "
            "Valid targets: " + ", ".join(sorted(ENGINE_URLS)),
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = load_config()
    client = VoxClient(cfg.default_url)

    # Validate engine exists in /healthz.
    try:
        hc = client.healthz()
    except VoxUnreachable as exc:
        typer.secho(f"unreachable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3)

    available = [e.name for e in hc.engines if e.name not in INTERNAL_ENGINE_NAMES]
    if name not in available:
        typer.secho(
            f"engine '{name}' not found; available: {', '.join(available)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    current_pairs = _current_engines(project_root)
    target_url = _url_for(name)

    # Build new chain: <name>=URL first, then the others in original order.
    new_pairs = [(name, target_url)]
    for n, u in current_pairs:
        if n != name:
            new_pairs.append((n, u))

    # If <name> wasn't in the chain before, it's now at position 0; that's fine.
    old_primary = current_pairs[0][0] if current_pairs else "(none)"
    new_engines_str = _render_vox_engines(new_pairs)

    console.print(
        f"[bold]Reordering engine chain:[/bold] "
        f"[cyan]{old_primary}[/cyan] → [cyan]{name}[/cyan] (primary)"
    )

    _recreate_core(project_root, new_engines_str, f"engine use {name}")

    console.print(f"  [dim]Polling until {name} is primary and ready (timeout 60s)...[/dim]")
    ok = _poll_primary(client, expected_primary=name, timeout=60)

    if ok:
        console.print(
            f"[green bold]Primary engine: {old_primary} → {name}[/green bold]"
        )
    else:
        typer.secho(
            f"Timeout: {name} did not become primary within 60s. "
            "Check `voxxy health` and `docker logs vox`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=3)


def engine_enable(
    name: str = typer.Argument(..., help="Engine to add to the synthesis chain."),
) -> None:
    """Add <name> to the engine chain if not already present.

    Appends to the end (before the implicit ElevenLabs fallback). If the engine
    is already enabled, this is a no-op with a friendly message.
    """
    if name in INTERNAL_ENGINE_NAMES:
        typer.secho(
            f"'{name}' is managed internally by core and cannot be enabled this way.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    # Validate the engine name is known (we need a URL for it).
    _url_for(name)  # Raises Exit(1) if unknown.

    cfg = load_config()
    client = VoxClient(cfg.default_url)

    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    current_pairs = _current_engines(project_root)
    current_names = [n for n, _ in current_pairs]

    if name in current_names:
        console.print(
            f"[yellow]Engine '{name}' is already enabled.[/yellow] "
            f"Current chain: {', '.join(current_names)}"
        )
        return

    # Append to end.
    new_pairs = current_pairs + [(name, ENGINE_URLS[name])]
    new_engines_str = _render_vox_engines(new_pairs)

    console.print(
        f"[bold]Enabling engine:[/bold] appending [cyan]{name}[/cyan] to chain. "
        f"New chain: {', '.join(n for n, _ in new_pairs)}"
    )

    _recreate_core(project_root, new_engines_str, f"engine enable {name}")

    console.print("  [dim]Polling until core is back up (timeout 60s)...[/dim]")
    ok = _poll_primary(client, expected_primary=None, timeout=60)

    if ok:
        console.print(f"[green bold]Engine '{name}' enabled.[/green bold]")
    else:
        typer.secho(
            "Timeout: core did not come back within 60s. "
            "Check `voxxy health` and `docker logs vox`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=3)


def engine_disable(
    name: str = typer.Argument(..., help="Engine to remove from the synthesis chain."),
    force: bool = typer.Option(
        False,
        "-f", "--force",
        help="Allow disabling the last local engine (ElevenLabs-only mode).",
    ),
) -> None:
    """Remove <name> from the engine chain.

    Refuses to drop the last remaining local engine unless --force is passed,
    because that would leave only the cost-bearing ElevenLabs API in the chain.
    If the engine is not in the chain, this is a no-op with a friendly message.
    """
    if name in INTERNAL_ENGINE_NAMES:
        typer.secho(
            f"'{name}' is managed internally by core and cannot be disabled this way.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = load_config()
    client = VoxClient(cfg.default_url)

    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    current_pairs = _current_engines(project_root)
    current_names = [n for n, _ in current_pairs]

    if name not in current_names:
        console.print(
            f"[yellow]Engine '{name}' is already not in the chain.[/yellow] "
            f"Current chain: {', '.join(current_names)}"
        )
        return

    new_pairs = [(n, u) for n, u in current_pairs if n != name]

    # Guard: don't leave an empty local engine chain unless forced.
    if not new_pairs and not force:
        typer.secho(
            f"Refusing to disable '{name}': it is the only local engine in the chain. "
            "Removing it would route all synthesis through ElevenLabs (cost-bearing). "
            "Pass --force to override.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    new_engines_str = _render_vox_engines(new_pairs) if new_pairs else ""

    console.print(
        f"[bold]Disabling engine:[/bold] removing [cyan]{name}[/cyan] from chain. "
        f"New chain: {', '.join(n for n, _ in new_pairs) or '(empty — ElevenLabs only)'}"
    )

    _recreate_core(project_root, new_engines_str, f"engine disable {name}")

    console.print("  [dim]Polling until core is back up (timeout 60s)...[/dim]")
    ok = _poll_primary(client, expected_primary=None, timeout=60)

    if ok:
        console.print(f"[green bold]Engine '{name}' disabled.[/green bold]")
    else:
        typer.secho(
            "Timeout: core did not come back within 60s. "
            "Check `voxxy health` and `docker logs vox`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=3)
