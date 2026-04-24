"""Top-level Typer app wiring.

Subcommand groups (`daemon`, `engine`, `voice`) are mounted as sub-apps.
Leaf commands live under :mod:`voxxy.commands` and register themselves
onto either the root or the right sub-app via a ``register`` entrypoint.
Kept thin here so cold-start cost stays low for shell completions.
"""

from __future__ import annotations

import sys

import typer

from voxxy.commands import daemon as daemon_commands
from voxxy.commands import engine as engine_commands
from voxxy.commands import speak as speak_commands
from voxxy.commands import util as util_commands
from voxxy.commands import voice as voice_commands

app = typer.Typer(
    name="voxxy",
    help="Unified CLI for vox-tts. See 'voxxy <command> --help' for details.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

daemon_app = typer.Typer(help="Stack lifecycle: start, stop, status, install.")
engine_app = typer.Typer(help="Engine control: list, use, enable, disable, logs.")
voice_app = typer.Typer(help="Voice management: list, info, add, delete.")

app.add_typer(daemon_app, name="daemon")
app.add_typer(engine_app, name="engine")
app.add_typer(voice_app, name="voice")


@app.callback()
def _root(
    ctx: typer.Context,
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Raise exceptions instead of handling them gracefully.",
        is_eager=False,
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress non-data output (errors still go to stderr).",
        is_eager=False,
    ),
) -> None:
    """Global options that apply to every subcommand."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    ctx.obj["quiet"] = quiet


# Top-level leaves (health, logs, version, speak).
util_commands.register(app)
speak_commands.register(app)

# Engine, voice, and daemon commands.
engine_commands.register(engine_app)
voice_commands.register(voice_app)
daemon_commands.register(daemon_app)


def main() -> None:
    """Entrypoint used by ``[project.scripts] voxxy``."""
    from voxxy.errors import (
        ProjectNotFound,
        VoxUnreachable,
        FfmpegMissing,
        DockerError,
        EXIT_NOT_FOUND,
        EXIT_UNREACHABLE,
        EXIT_GENERIC,
    )

    try:
        app()
    except SystemExit:
        # typer.Exit and SystemExit are intentional control-flow signals;
        # always propagate them. typer.Exit is a subclass of SystemExit.
        raise
    except ProjectNotFound as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(EXIT_NOT_FOUND)
    except VoxUnreachable as exc:
        typer.secho(f"vox service unreachable: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(EXIT_UNREACHABLE)
    except FfmpegMissing as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        sys.exit(EXIT_GENERIC)
    except DockerError as exc:
        typer.secho(f"docker: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(EXIT_GENERIC)
    except Exception as exc:
        typer.secho(f"unexpected error: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(EXIT_GENERIC)


if __name__ == "__main__":
    main()
