"""Docker + 1Password subprocess helpers for the voxxy CLI.

Design notes:
- All stack mutations go through `op run --env-file .env.template -- docker compose ...`
  so secrets (ElevenLabs key, postgres DSN) never touch the CLI process's env
  directly. This mirrors the pattern in mise.toml and keeps the CLI honest about
  not reading secrets itself.
- ensure_op_authed() is NOT cached. Op auth can expire between commands in a long
  session, and a false "already checked" assumption would produce a confusing
  "docker compose: env var missing" error rather than "op not authenticated". The
  check is a fast `op account list` call so the cost is negligible.
- logs_follow() uses os.execvp to *replace* the current process with docker logs -f.
  This is the correct approach for interactive tail commands because:
  1. Ctrl-C goes directly to docker, not to Python, so there's no risk of the
     Python cleanup code swallowing the signal.
  2. No Python process remains in memory while the user watches logs.
  3. The terminal is fully handed over; colours, line buffering, and TTY detection
     all work as if the user had typed `docker logs -f` directly.
  Note: this function DOES NOT RETURN. Any code placed after the call is dead code.
- compose_up passes `env` by merging into os.environ.copy() rather than using
  subprocess's env= kwarg with a minimal dict. This ensures the subprocess inherits
  the full user environment (PATH, HOME, etc.) needed by `op` and `docker`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


class DockerError(RuntimeError):
    """Raised when a docker or op subprocess exits with a non-zero code.

    The error message includes captured stderr so callers can surface the root
    cause without needing to re-run the failing command.
    """


def ensure_op_authed() -> None:
    """Verify that the 1Password CLI is authenticated.

    Raises DockerError if `op account list` fails (unauthenticated or op missing).
    Called before any compose_up/compose_down so the user gets a clear message
    rather than a cryptic compose failure about missing env vars.

    NOT cached — see module docstring for rationale.
    """
    result = subprocess.run(
        ["op", "account", "list"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DockerError(
            "op is not authenticated or not installed. "
            "Run `op signin` and try again.\n"
            f"stderr: {result.stderr.strip()}"
        )


def _base_compose_argv(project_root: Path, *, full: bool) -> list[str]:
    """Build the common prefix for all compose commands."""
    argv = [
        "op", "run",
        "--env-file", str(project_root / ".env.template"),
        "--",
        "docker", "compose",
        "-f", "compose.yml",
    ]
    if full:
        argv += ["-f", "compose.engines.yml"]
    return argv


def compose_up(
    project_root: Path,
    *,
    full: bool = True,
    services: Optional[list[str]] = None,
    recreate: bool = False,
    env: Optional[dict[str, str]] = None,
    no_build: bool = False,
) -> None:
    """Bring up the stack via docker compose.

    Args:
        project_root: Root of the voxxy repo (cwd for the subprocess).
        full: If True, include compose.engines.yml (GPU sidecars). Set False
              for core-only restarts.
        services: Specific services to start; if None, starts all.
        recreate: Pass --force-recreate so containers pick up env changes.
        env: Extra env vars to inject (e.g. VOX_ENGINES override). Merged
             on top of os.environ so PATH/HOME/etc. are preserved.
        no_build: If True, skip ``--build`` (use cached images). Useful for
                  fast restarts where the image is known-good.
    """
    argv = _base_compose_argv(project_root, full=full)
    argv += ["up", "-d"]
    if not no_build:
        argv.append("--build")
    if recreate:
        argv.append("--force-recreate")
    if services:
        argv.extend(services)

    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    result = subprocess.run(argv, cwd=project_root, env=proc_env, capture_output=False)
    if result.returncode != 0:
        raise DockerError(
            f"docker compose up failed (exit {result.returncode}). "
            "Check output above for details."
        )


def compose_down(project_root: Path, *, full: bool = True) -> None:
    """Bring down the stack via docker compose."""
    argv = _base_compose_argv(project_root, full=full) + ["down"]

    result = subprocess.run(argv, cwd=project_root, capture_output=False)
    if result.returncode != 0:
        raise DockerError(
            f"docker compose down failed (exit {result.returncode}). "
            "Check output above for details."
        )


def compose_build(
    project_root: Path,
    services: Optional[list[str]] = None,
    *,
    no_cache: bool = False,
) -> None:
    """Build images via docker compose build."""
    argv = _base_compose_argv(project_root, full=True) + ["build"]
    if no_cache:
        argv.append("--no-cache")
    if services:
        argv.extend(services)

    result = subprocess.run(argv, cwd=project_root, capture_output=False)
    if result.returncode != 0:
        raise DockerError(
            f"docker compose build failed (exit {result.returncode}). "
            "Check output above for details."
        )


def container_status(name: str) -> str:
    """Return the current state of a named container.

    Returns one of: "running", "exited", "restarting", "missing", "paused",
    "dead", "created". Returns "missing" if the container does not exist
    (docker inspect exits non-zero) rather than raising, so callers can
    treat missing and stopped as the same non-running condition.
    """
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "missing"
    return result.stdout.strip()


def logs_follow(container_name: str) -> None:
    """Replace the current process with `docker logs -f <container>`.

    os.execvp replaces the current Python process so:
    - Ctrl-C reaches docker directly (no Python signal interception)
    - No Python cleanup code runs after the user presses Ctrl-C (desired)
    - No residual Python process holds memory while logs stream

    This function DOES NOT RETURN. Any code after this call is unreachable.
    """
    os.execvp("docker", ["docker", "logs", "-f", container_name])


def image_for(container_name: str) -> Optional[str]:
    """Return the image tag backing a running (or stopped) container.

    Uses ``docker inspect -f '{{.Config.Image}}'`` so it works for any
    container state, not just running. Returns None if the container does
    not exist (inspect exits non-zero).

    This is a pure inspection call — it never modifies container state.
    """
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.Config.Image}}", container_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
