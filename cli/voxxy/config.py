"""User config + project-root discovery for the voxxy CLI.

Design notes:
- Config lives at ~/.config/voxxy/config.toml (XDG-ish; not full XDG_CONFIG_HOME
  because the project has a fixed audience and keeps the path predictable).
- 0600 permissions on config.toml prevent other local users reading the project_root
  (which implies the voxxy repo location on disk). We write via os.open so the
  permissions are set atomically at file creation rather than chmod-after-write.
- save_config uses tomli_w (not tomllib which is read-only stdlib) because tomllib
  was added to the stdlib as read-only by design; writing requires a third-party lib.
- discover_project_root intentionally does NOT fall back silently: the caller must
  know *why* discovery succeeded so error messages are accurate, and callers that
  don't need a project_root (e.g. 'voxxy health --url ...') skip discovery entirely.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import tomli_w

CONFIG_PATH = Path.home() / ".config" / "voxxy" / "config.toml"


class ProjectNotFound(Exception):
    """Raised when no valid voxxy project root can be resolved.

    The ``tried`` attribute lists the paths / resolution methods attempted in
    order, so the error message can point the user at what to fix.
    """

    def __init__(self, tried: list[str]) -> None:
        self.tried = tried

    def __str__(self) -> str:
        lines = ["Could not locate a voxxy project root. Tried:"]
        for t in self.tried:
            lines.append(f"  - {t}")
        lines.append(
            "\nFix: pass --project /path/to/repo, set VOXXY_HOME, "
            "add project_root to ~/.config/voxxy/config.toml, "
            "or run from inside the voxxy repository."
        )
        return "\n".join(lines)


@dataclass(slots=True)
class Config:
    """User-level preferences stored in ~/.config/voxxy/config.toml.

    Defaults are chosen to work against the production stack out of the box so
    a user who hasn't run 'daemon install' can still 'voxxy health'.
    """

    project_root: Optional[Path] = None
    default_url: str = "https://vox.delo.sh"
    default_voice: str = "rick"


def load_config() -> Config:
    """Read CONFIG_PATH if it exists; return defaults otherwise.

    Uses stdlib tomllib (3.11+). Does not raise on a missing file because the
    CLI must work before 'daemon install' creates the config.
    """
    if not CONFIG_PATH.is_file():
        return Config()

    with open(CONFIG_PATH, "rb") as fh:
        raw = tomllib.load(fh)

    project_root: Optional[Path] = None
    if "project_root" in raw:
        project_root = Path(raw["project_root"]).expanduser()

    return Config(
        project_root=project_root,
        default_url=raw.get("default_url", Config.default_url),
        default_voice=raw.get("default_voice", Config.default_voice),
    )


def save_config(cfg: Config) -> None:
    """Write CONFIG_PATH with 0600 permissions.

    0600 (owner read/write only) is used because the file may contain the local
    filesystem path to the voxxy repo, which doubles as information about what
    the user is running on this machine. Group/other read is undesirable.

    We use os.open with O_CREAT|O_WRONLY|O_TRUNC to set permissions atomically
    at creation time; chmod-after-write has a TOCTOU window where other processes
    could read the file before permissions are tightened.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {
        "default_url": cfg.default_url,
        "default_voice": cfg.default_voice,
    }
    if cfg.project_root is not None:
        data["project_root"] = str(cfg.project_root)

    content = tomli_w.dumps(data).encode()

    fd = os.open(str(CONFIG_PATH), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def _is_voxxy_root(path: Path) -> bool:
    """Return True if path looks like the root of a voxxy project.

    A directory qualifies when it contains BOTH compose.yml AND an engines/
    subdirectory. This pair is unique enough that accidental false positives
    are extremely unlikely.
    """
    return (path / "compose.yml").is_file() and (path / "engines").is_dir()


def discover_project_root(cli_flag: Optional[Path] = None) -> Path:
    """Resolve the voxxy project root following the AC6 search order.

    Search order (first match wins):
      1. cli_flag, if provided
      2. VOXXY_HOME environment variable
      3. project_root from ~/.config/voxxy/config.toml
      4. Walk up from cwd; first dir containing compose.yml + engines/ wins

    Raises ProjectNotFound listing every path / method tried if all fail.
    """
    tried: list[str] = []

    # 1. CLI flag
    if cli_flag is not None:
        if _is_voxxy_root(cli_flag):
            return cli_flag.resolve()
        tried.append(f"--project flag: {cli_flag} (not a voxxy project)")

    # 2. Environment variable
    env_home = os.environ.get("VOXXY_HOME")
    if env_home:
        p = Path(env_home).expanduser()
        if _is_voxxy_root(p):
            return p.resolve()
        tried.append(f"$VOXXY_HOME={env_home} (not a voxxy project)")
    else:
        tried.append("$VOXXY_HOME (not set)")

    # 3. Config file
    cfg = load_config()
    if cfg.project_root is not None:
        p = cfg.project_root
        if _is_voxxy_root(p):
            return p.resolve()
        tried.append(
            f"config.toml project_root={cfg.project_root} (not a voxxy project)"
        )
    else:
        tried.append("config.toml project_root (not set)")

    # 4. Walk up from cwd
    current = Path.cwd().resolve()
    walked: list[str] = []
    while True:
        if _is_voxxy_root(current):
            return current
        walked.append(str(current))
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            break
        current = parent

    tried.append(
        f"cwd walk-up from {Path.cwd()}: checked {len(walked)} director"
        f"{'y' if len(walked) == 1 else 'ies'}, none contained compose.yml + engines/"
    )

    raise ProjectNotFound(tried)
