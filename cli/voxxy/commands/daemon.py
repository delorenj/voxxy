"""Daemon lifecycle commands: start, stop, restart, status, reset, install.

These commands manage the docker compose stack that backs voxxy-core and its
engine sidecars. All stack mutations go through 1Password via compose_up /
compose_down; the CLI never reads secrets directly.

Design notes:
- Health polling uses a simple time.sleep(2) loop rather than asyncio because
  these are one-shot CLI invocations, not long-running services. The polling
  window is at most 180s; complexity of async is not justified.
- compose_up stdout/stderr stream directly to the terminal (capture_output=False
  in docker.py) so the user sees docker's pull/build progress.
- `daemon status` inspects containers via docker inspect (not compose ps) so it
  works even when compose context isn't available (e.g. running from a different
  directory after daemon install).
- `daemon reset` uses shutil.rmtree + os.makedirs rather than iterating and
  unlinking because the audio-cache directory may contain thousands of files;
  the atomic rm+recreate is measurably faster.
- `daemon install` is the T6.1 bootstrap flow: prereq check → config write →
  `uv tool install` → optional shell completions → optional systemd unit.
  Every step reports to stderr so a pipeline caller sees progress; exits fast
  with a non-zero code on the first unrecoverable failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from voxxy.client import VoxClient, VoxNotFound, VoxUnreachable, VoxServerError
from voxxy.config import CONFIG_PATH, Config, discover_project_root, load_config, save_config
from voxxy.docker import (
    DockerError,
    compose_down,
    compose_up,
    container_status,
    ensure_op_authed,
    image_for,
)
from voxxy.state import load_state

console = Console()

# The compose default when no state has been written yet (mirrors compose.yml).
DEFAULT_VOX_ENGINES = (
    "voxcpm=http://voxxy-engine-voxcpm:8000,"
    "vibevoice=http://voxxy-engine-vibevoice:8000"
)

# Containers that are part of the full stack (used for status display).
STACK_CONTAINERS = ["vox", "voxxy-engine-voxcpm", "voxxy-engine-vibevoice"]

# Engines that are NOT ElevenLabs (i.e., local engines that must be ready
# before `daemon start` declares success).
LOCAL_ENGINE_NAMES = {"voxcpm", "vibevoice"}


def register(daemon_app: typer.Typer) -> None:
    """Register all daemon leaf commands on the daemon sub-app."""
    daemon_app.command("start")(daemon_start)
    daemon_app.command("stop")(daemon_stop)
    daemon_app.command("restart")(daemon_restart)
    daemon_app.command("status")(daemon_status)
    daemon_app.command("reset")(daemon_reset)
    daemon_app.command("install")(daemon_install)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_client(project_root: Optional[Path] = None) -> VoxClient:
    """Build a VoxClient from config (or default URL)."""
    cfg = load_config()
    return VoxClient(cfg.default_url)


def _poll_until_healthy(
    client: VoxClient,
    *,
    timeout: int,
    require_engine: Optional[str] = None,
    message: str = "Waiting for stack to become healthy",
) -> bool:
    """Poll /healthz until all local engines are ready (or timeout).

    Args:
        client: VoxClient to use for polling.
        timeout: Maximum seconds to wait.
        require_engine: If set, also assert this engine is first in the list
                        and ready (used by ``engine use``).
        message: Spinner label shown during wait.

    Returns:
        True if healthy within timeout; False on timeout.
    """
    deadline = time.monotonic() + timeout
    spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while time.monotonic() < deadline:
        try:
            hc = client.healthz()
            local_engines = [e for e in hc.engines if e.name in LOCAL_ENGINE_NAMES]
            all_local_ready = all(e.ready for e in local_engines)

            if require_engine:
                # Check that the named engine is first and ready.
                if hc.engines and hc.engines[0].name == require_engine:
                    target = next((e for e in hc.engines if e.name == require_engine), None)
                    if target and target.ready:
                        # Clear spinner line.
                        console.print(f"\r  {message}: [green]healthy[/green]" + " " * 20)
                        return True
                # Not yet in position; keep polling.
            elif all_local_ready:
                console.print(f"\r  {message}: [green]healthy[/green]" + " " * 20)
                return True

        except (VoxUnreachable, VoxServerError, VoxNotFound):
            pass  # 502/503/404 while restarting (Traefik re-registering); keep polling.

        ch = spinner_chars[i % len(spinner_chars)]
        # Use print with \r and end="" for spinner-in-place; Rich print adds newline.
        print(f"\r  {ch} {message} …", end="", flush=True)
        i += 1
        time.sleep(2)

    print()  # Final newline after spinner.
    return False


def _engines_from_state(project_root: Path) -> str:
    """Return the VOX_ENGINES string to inject, or the compose default."""
    state = load_state(project_root)
    return state.vox_engines or DEFAULT_VOX_ENGINES


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def daemon_start(
    core_only: bool = typer.Option(
        False, "-C", "--core-only", help="Start only voxxy-core; skip engine sidecars."
    ),
    engines_only: bool = typer.Option(
        False, "-E", "--engines-only", help="Start only the engine sidecars."
    ),
    force_recreate: bool = typer.Option(
        False, "-f", "--force-recreate", help="Force-recreate containers even if up to date."
    ),
    no_rebuild: bool = typer.Option(
        False, "-N", "--no-rebuild", help="Skip docker build; use cached images."
    ),
) -> None:
    """Bring up the voxxy stack (core + engine sidecars by default)."""
    if core_only and engines_only:
        typer.secho(
            "Error: --core-only and --engines-only are mutually exclusive.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        ensure_op_authed()
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    vox_engines = _engines_from_state(project_root)
    engine_env: dict[str, str] = {"VOX_ENGINES": vox_engines}

    if engines_only:
        console.print("[bold]Starting engine sidecars only...[/bold]")
        try:
            compose_up(
                project_root,
                full=True,
                services=["voxxy-engine-voxcpm", "voxxy-engine-vibevoice"],
                recreate=force_recreate,
                env=engine_env,
                no_build=no_rebuild,
            )
        except DockerError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        console.print("[green]Engine sidecars started.[/green]")
        return

    console.print("[bold]Starting voxxy stack...[/bold]")
    try:
        compose_up(
            project_root,
            full=not core_only,
            recreate=force_recreate,
            env=engine_env,
            no_build=no_rebuild,
        )
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    console.print("[bold]Polling /healthz (timeout 180s)...[/bold]")
    client = _get_client(project_root)
    ok = _poll_until_healthy(client, timeout=180, message="Waiting for local engines")

    if ok:
        console.print("[green bold]Stack is healthy.[/green bold]")
    else:
        typer.secho(
            "Timeout: stack did not become healthy within 180s. "
            "Check `voxxy daemon status` and `docker logs vox`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=3)


def daemon_stop() -> None:
    """Bring down the entire voxxy stack."""
    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        ensure_op_authed()
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    console.print("[bold]Stopping voxxy stack...[/bold]")
    try:
        compose_down(project_root)
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    console.print("[green]Stack stopped.[/green]")


def daemon_restart() -> None:
    """Recreate only voxxy-core (faster than full restart)."""
    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        ensure_op_authed()
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    vox_engines = _engines_from_state(project_root)
    engine_env: dict[str, str] = {"VOX_ENGINES": vox_engines}

    console.print("[bold]Recreating voxxy-core (vox)...[/bold]")
    try:
        compose_up(
            project_root,
            full=False,   # Core-only compose; engines keep running.
            services=["vox"],
            recreate=True,
            env=engine_env,
            no_build=False,
        )
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    console.print("[bold]Polling /healthz (timeout 60s)...[/bold]")
    client = _get_client(project_root)
    ok = _poll_until_healthy(client, timeout=60, message="Waiting for core")

    if ok:
        console.print("[green bold]Core restarted and healthy.[/green bold]")
    else:
        typer.secho(
            "Timeout: core did not come back within 60s. "
            "Check `docker logs vox`.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=3)


def daemon_status(
    wait_healthy: bool = typer.Option(
        False, "-w", "--wait-healthy", help="Block until all containers are healthy."
    ),
    timeout: int = typer.Option(
        60, "-t", "--timeout", help="Seconds to wait when --wait-healthy is set."
    ),
    json_output: bool = typer.Option(
        False, "-j", "--json", help="Dump a JSON blob instead of the Rich table."
    ),
) -> None:
    """Show container states and per-engine health from /healthz."""
    # --- Gather container state via docker inspect ---
    container_states: dict[str, str] = {}
    container_images: dict[str, str] = {}
    for name in STACK_CONTAINERS:
        container_states[name] = container_status(name)
        img = image_for(name)
        container_images[name] = img or "—"

    # --- Gather engine health from /healthz (best-effort; may be unreachable) ---
    cfg = load_config()
    client = VoxClient(cfg.default_url)
    engine_health: dict[str, bool] = {}
    overall_reachable = True
    try:
        hc = client.healthz()
        for eng in hc.engines:
            engine_health[eng.name] = eng.ready
    except (VoxUnreachable, VoxServerError, VoxNotFound):
        overall_reachable = False

    if wait_healthy:
        deadline = time.monotonic() + timeout
        spinner_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        while time.monotonic() < deadline:
            all_running = all(
                container_states[n] == "running" for n in STACK_CONTAINERS
            )
            if all_running and overall_reachable and all(
                engine_health.get(e, False) for e in LOCAL_ENGINE_NAMES
            ):
                break

            time.sleep(2)
            for name in STACK_CONTAINERS:
                container_states[name] = container_status(name)
                img = image_for(name)
                container_images[name] = img or "—"
            try:
                hc = client.healthz()
                overall_reachable = True
                for eng in hc.engines:
                    engine_health[eng.name] = eng.ready
            except (VoxUnreachable, VoxServerError, VoxNotFound):
                overall_reachable = False

            ch = spinner_chars[i % len(spinner_chars)]
            print(f"\r  {ch} Waiting for healthy stack …", end="", flush=True)
            i += 1
        else:
            print()
            typer.secho(
                f"Timeout: stack not healthy within {timeout}s.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=2)
        print()

    # --- Derive overall status ---
    all_running = all(container_states[n] == "running" for n in STACK_CONTAINERS)
    if not overall_reachable:
        overall_status = "unreachable"
    elif all_running and all(
        engine_health.get(e, False) for e in LOCAL_ENGINE_NAMES
    ):
        overall_status = "ok"
    else:
        overall_status = "degraded"

    if json_output:
        # Machine-readable shape documented in T6.0 bonus / T7.2.
        containers_payload = [
            {
                "name": name,
                "state": container_states[name],
                "image": container_images[name] if container_images[name] != "—" else None,
            }
            for name in STACK_CONTAINERS
        ]
        engines_payload = [
            {"name": n, "ready": r} for n, r in sorted(engine_health.items())
        ]
        print(json.dumps(
            {
                "status": overall_status,
                "containers": containers_payload,
                "engines": engines_payload,
                "reachable": overall_reachable,
            },
            indent=2,
        ))
    else:
        # --- Render table ---
        table = Table(title="Voxxy Stack Status")
        table.add_column("Container", style="cyan")
        table.add_column("State")
        table.add_column("Health (API)")
        table.add_column("Image")

        for name in STACK_CONTAINERS:
            state = container_states[name]
            if state == "running":
                state_str = "[green]running[/green]"
            elif state == "missing":
                state_str = "[red]missing[/red]"
            else:
                state_str = f"[yellow]{state}[/yellow]"

            # Derive engine name from container name (vox → core, voxxy-engine-X → X).
            if name == "vox":
                eng_name = "core"
                health_str = "[green]ok[/green]" if overall_reachable else "[red]unreachable[/red]"
            else:
                eng_name = name.replace("voxxy-engine-", "")
                if not overall_reachable:
                    health_str = "[dim]unreachable[/dim]"
                elif eng_name in engine_health:
                    health_str = "[green]ready[/green]" if engine_health[eng_name] else "[red]not ready[/red]"
                else:
                    health_str = "[dim]—[/dim]"

            table.add_row(name, state_str, health_str, container_images[name])

        console.print(table)

    # Determine exit code.
    if not overall_reachable:
        raise typer.Exit(code=3)
    if not all_running:
        raise typer.Exit(code=2)


def daemon_reset(
    yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip confirmation prompt."
    ),
) -> None:
    """Stop all containers and wipe the audio cache. Voices are preserved."""
    try:
        project_root = discover_project_root()
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not yes:
        if sys.stdin.isatty():
            confirm = typer.prompt(
                "This will stop all containers and wipe audio-cache. "
                "Voices are preserved. Continue? [y/N]",
                default="N",
            )
            if confirm.strip().lower() not in ("y", "yes"):
                typer.secho("Aborted.", fg=typer.colors.YELLOW)
                raise typer.Exit(code=0)
        else:
            # Non-TTY stdin without --yes: refuse to proceed destructively.
            typer.secho(
                "Refusing non-interactive reset without --yes. "
                "Pass --yes to confirm.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

    # Stop all containers first.
    try:
        ensure_op_authed()
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    console.print("[bold]Stopping stack...[/bold]")
    try:
        compose_down(project_root)
    except DockerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    console.print("[green]Stack stopped.[/green]")

    # Wipe audio cache (not the directory itself, not voices/).
    audio_cache = project_root / "audio-cache"
    removed_count = 0
    if audio_cache.is_dir():
        for child in audio_cache.iterdir():
            removed_count += 1
        shutil.rmtree(audio_cache)
        audio_cache.mkdir(exist_ok=True)
        console.print(
            f"[green]Audio cache cleared:[/green] removed {removed_count} "
            f"file{'s' if removed_count != 1 else ''} from {audio_cache}"
        )
    else:
        console.print(f"[dim]Audio cache directory not found at {audio_cache}; nothing to clear.[/dim]")

    console.print("[green bold]Reset complete.[/green bold] Voices and DB untouched.")
    console.print("Run [cyan]voxxy daemon start[/cyan] to bring the stack back up.")


# ---------------------------------------------------------------------------
# daemon install helpers (T6.1)
# ---------------------------------------------------------------------------

# Prereqs for a functional voxxy host. Each tuple is
# (executable, version-probe-argv, install-hint). version-probe-argv is run and
# its first line of stdout used as the "installed" proof. If the binary is
# absent OR the probe fails, we collect an install hint.
_PREREQS: list[tuple[str, list[str], str]] = [
    ("docker",        ["docker", "--version"],        "install: https://docs.docker.com/engine/install/"),
    ("docker compose", ["docker", "compose", "version"], "install: docker compose is bundled with docker >= 20.10; upgrade docker."),
    ("op",            ["op", "--version"],            "install: brew install 1password-cli  OR  apt install 1password-cli"),
    ("ffmpeg",        ["ffmpeg", "-version"],         "install: apt install ffmpeg"),
    ("ffprobe",       ["ffprobe", "-version"],        "install: apt install ffmpeg (ffprobe ships with ffmpeg)"),
    ("psql",          ["psql", "--version"],          "install: apt install postgresql-client"),
]


def _stderr(msg: str) -> None:
    """Write a plain-text line to stderr. Used by daemon_install steps."""
    print(msg, file=sys.stderr, flush=True)


def _check_prereq(name: str, argv: list[str]) -> Optional[str]:
    """Probe one prereq. Returns the version string on success, or None.

    ``name`` may be a multi-word label (e.g. "docker compose"); the argv's first
    element must still be an executable name that ``shutil.which`` can resolve.
    """
    exe = argv[0]
    if shutil.which(exe) is None:
        return None
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # First line, stripped. Covers "docker version 28.1.1" and the multi-line
    # output of "docker compose version" alike.
    out = (result.stdout or result.stderr).strip().splitlines()
    if not out:
        return None
    return out[0].strip()


def _check_nvidia_runtime() -> bool:
    """Return True if docker info reports nvidia as the default runtime."""
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    # docker info prints a line like "Default Runtime: nvidia".
    for line in result.stdout.splitlines():
        if "default runtime" in line.lower() and "nvidia" in line.lower():
            return True
    return False


def _check_restart_policies(project_root: Path) -> list[str]:
    """Return list of services missing ``restart: unless-stopped``.

    Uses ``docker compose ... config`` to get the fully resolved compose spec,
    then walks the services and checks each one's restart field. Services that
    are missing the field entirely or whose field differs from unless-stopped
    are returned. Empty list means everything is correctly configured.
    """
    argv = [
        "docker", "compose",
        "-f", "compose.yml",
        "-f", "compose.engines.yml",
        "config",
    ]
    try:
        # ``docker compose config`` expands env vars; unset required vars would
        # fail. We don't care about values — only the restart key — so silence
        # interpolation warnings by passing dummy env if needed.
        env = os.environ.copy()
        env.setdefault("ELEVENLABS_API_KEY", "dummy-for-config-parse")
        env.setdefault("POSTGRES_DSN", "dummy-for-config-parse")
        result = subprocess.run(
            argv, cwd=project_root, capture_output=True, text=True, timeout=15, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []  # Can't tell; warn-only check, assume OK.
    if result.returncode != 0:
        return []

    # Parse YAML-ish output minimally: look for "services:" block, then each
    # second-level key is a service. For each service, grep for its restart:
    # line before the next second-level key. This avoids adding a yaml dep.
    try:
        import yaml  # Compose ships with PyYAML in most installs; optional.
        doc = yaml.safe_load(result.stdout) or {}
        services = doc.get("services", {}) or {}
        missing: list[str] = []
        for svc_name, svc_body in services.items():
            if not isinstance(svc_body, dict):
                continue
            policy = svc_body.get("restart")
            if policy != "unless-stopped":
                missing.append(svc_name)
        return missing
    except ImportError:
        # PyYAML unavailable — skip check.
        return []


def _detect_shell() -> Optional[str]:
    """Return 'bash', 'zsh', 'fish', or None based on $SHELL."""
    shell = os.environ.get("SHELL", "")
    base = os.path.basename(shell).lower()
    if base in ("bash", "zsh", "fish"):
        return base
    return None


def _completion_install_path(shell: str) -> Path:
    """Return the install path for completions for the given shell."""
    home = Path.home()
    if shell == "bash":
        return home / ".local" / "share" / "bash-completion" / "completions" / "voxxy"
    if shell == "zsh":
        return home / ".local" / "share" / "zsh" / "site-functions" / "_voxxy"
    if shell == "fish":
        return home / ".config" / "fish" / "completions" / "voxxy.fish"
    raise ValueError(f"unsupported shell: {shell}")


def _generate_completions(shell: str, voxxy_path: str) -> Optional[str]:
    """Call `voxxy --show-completion` with the shell env set; return stdout."""
    env = os.environ.copy()
    env["_VOXXY_COMPLETE"] = shell + "_source"  # Click/Typer env knob.
    env["SHELL"] = f"/bin/{shell}"
    try:
        result = subprocess.run(
            [voxxy_path, "--show-completion"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _install_completions(shell: str, voxxy_path: str) -> Optional[Path]:
    """Generate + write completion script for `shell`. Returns path on success."""
    script = _generate_completions(shell, voxxy_path)
    if not script or not script.strip():
        return None
    dst = _completion_install_path(shell)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(script)
    return dst


_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=voxxy boot-time stack reconciliation
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={voxxy_path} daemon status --wait-healthy --timeout 120

[Install]
WantedBy=default.target
"""


def _install_systemd_unit(voxxy_path: str) -> Path:
    """Write + enable the voxxy-boot systemd user unit. Returns the path."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / "voxxy-boot.service"
    unit_path.write_text(_SYSTEMD_UNIT_TEMPLATE.format(voxxy_path=voxxy_path))

    # Best-effort daemon-reload + enable. A failure here is surfaced to the
    # user but doesn't unwind the install — the unit file is still on disk and
    # can be enabled manually.
    for argv in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "voxxy-boot.service"],
    ):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, OSError) as exc:
            _stderr(f"  [warn] {' '.join(argv)} failed: {exc}")
            continue
        if result.returncode != 0:
            _stderr(f"  [warn] {' '.join(argv)} exited {result.returncode}: "
                    f"{result.stderr.strip()}")
    return unit_path


def daemon_install(
    project: Optional[Path] = typer.Option(
        None, "-p", "--project", help="Project root (default: discover)."
    ),
    completions: bool = typer.Option(
        False, "-c", "--completions",
        help="Install shell completions for the detected shell."
    ),
    systemd: bool = typer.Option(
        False, "-s", "--systemd",
        help="Install a user systemd unit for boot-time reconciliation (optional)."
    ),
    skip_prereq_check: bool = typer.Option(
        False, "-k", "--skip-prereq-check",
        help="Skip the docker/op/ffmpeg/psql prereq probe."
    ),
    force: bool = typer.Option(
        False, "-f", "--force",
        help="Reinstall even if already installed (rewrites config, re-runs uv tool install)."
    ),
) -> None:
    """Bootstrap this host for voxxy (check prereqs, write config, install CLI).

    Idempotent by default: running without ``--force`` is safe on an already
    installed host. Each step reports a line to stderr; the first unrecoverable
    failure exits non-zero with an actionable message.
    """
    # --- Step 1: Resolve project root ---
    try:
        project_root = discover_project_root(project)
    except Exception as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4)
    _stderr(f"✓ project root: {project_root}")

    # --- Step 2: Prereq check ---
    missing: list[tuple[str, str]] = []
    if skip_prereq_check:
        _stderr("→ skipping prereq check (--skip-prereq-check)")
    else:
        _stderr("→ checking prereqs...")
        for label, argv, hint in _PREREQS:
            version = _check_prereq(label, argv)
            if version is None:
                missing.append((label, hint))
                _stderr(f"  ✗ {label} (missing)   {hint}")
            else:
                _stderr(f"  ✓ {version}")

        if missing:
            typer.secho(
                f"\nMissing prereqs: {', '.join(name for name, _ in missing)}. "
                "Install them and re-run `voxxy daemon install` (or pass "
                "--skip-prereq-check if you know what you're doing).",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)

    # --- Step 3: nvidia runtime check (warn-only) ---
    if _check_nvidia_runtime():
        _stderr("✓ nvidia is the default docker runtime")
    else:
        typer.secho(
            "  [warn] nvidia runtime is not default; engine containers may fail. "
            "See README for runtime configuration.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    # --- Step 4: Compose restart-policy check (warn-only) ---
    policy_missing = _check_restart_policies(project_root)
    if policy_missing:
        typer.secho(
            f"  [warn] services missing `restart: unless-stopped`: "
            f"{', '.join(policy_missing)}. Boot persistence may not survive reboot.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    else:
        _stderr("✓ compose restart policies look good")

    # --- Step 5: Config bootstrap ---
    config_existed = CONFIG_PATH.is_file()
    if config_existed and not force:
        _stderr(f"✓ config already at {CONFIG_PATH} (use --force to rewrite)")
    else:
        cfg = Config(
            project_root=project_root,
            default_url="https://vox.delo.sh",
            default_voice="rick",
        )
        save_config(cfg)
        _stderr(f"✓ wrote {CONFIG_PATH}")

    # --- Step 6: CLI install via uv tool ---
    voxxy_on_path = shutil.which("voxxy")
    if voxxy_on_path and not force:
        _stderr(f"✓ voxxy already on PATH at {voxxy_on_path} (use --force to reinstall)")
    else:
        if shutil.which("uv") is None:
            typer.secho(
                "✗ uv is required to install the voxxy CLI. "
                "install: https://docs.astral.sh/uv/getting-started/installation/",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        _stderr("→ uv tool install (streaming output)...")
        try:
            result = subprocess.run(
                ["uv", "tool", "install", "--force", str(project_root / "cli")],
                capture_output=False,
            )
        except OSError as exc:
            typer.secho(f"✗ uv tool install failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        if result.returncode != 0:
            typer.secho(
                f"✗ uv tool install exited {result.returncode}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        voxxy_on_path = shutil.which("voxxy")
        _stderr(f"✓ installed voxxy ({voxxy_on_path or 'not on PATH yet; check ~/.local/bin'})")

    # Resolved path used for completion + systemd steps (even if already
    # installed we need an absolute path for the unit ExecStart).
    voxxy_bin = voxxy_on_path or str(Path.home() / ".local" / "bin" / "voxxy")

    # --- Step 7: Shell completions ---
    completion_path: Optional[Path] = None
    if completions:
        shell = _detect_shell()
        if not shell:
            typer.secho(
                "  [warn] could not detect $SHELL; skipping completion install. "
                "Run `voxxy --show-completion` and pipe it into your shell's "
                "completion dir manually.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        else:
            try:
                completion_path = _install_completions(shell, voxxy_bin)
            except Exception as exc:
                typer.secho(
                    f"  [warn] completion install failed: {exc}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
            if completion_path:
                _stderr(f"✓ installed {shell} completions to {completion_path}")
            else:
                typer.secho(
                    f"  [warn] `voxxy --show-completion` produced no output for {shell}. "
                    "Skipping install; you can retry manually.",
                    fg=typer.colors.YELLOW,
                    err=True,
                )

    # --- Step 8: Systemd unit ---
    systemd_unit_path: Optional[Path] = None
    if systemd:
        systemd_unit_path = _install_systemd_unit(voxxy_bin)
        _stderr(f"✓ installed systemd unit at {systemd_unit_path}")

    # --- Final report ---
    lines = [
        f"[bold green]voxxy is installed.[/bold green]",
        f"  project:  {project_root}",
        f"  config:   {CONFIG_PATH}",
        f"  binary:   {voxxy_bin}",
    ]
    if completion_path:
        lines.append(f"  completions: {completion_path}")
    if systemd_unit_path:
        lines.append(f"  systemd:  {systemd_unit_path} (enabled)")
    lines.append("")
    lines.append("Next:")
    lines.append("  [cyan]voxxy daemon start[/cyan]   # bring up the stack")
    lines.append("  [cyan]voxxy health[/cyan]         # probe /healthz")
    console.print(Panel.fit("\n".join(lines), title="voxxy daemon install", border_style="green"))
