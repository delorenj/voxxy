"""Per-project ephemeral state for the voxxy CLI.

State file: ``{project_root}/.voxxy.state.json``

NOTE: This file is expected to be gitignored. See T6.5 for the .gitignore
update. The module creates/reads the file without modifying .gitignore so
the gitignore update can land as an independent atomic commit.

Design notes:
- JSON is used instead of TOML because the state is machine-written and the
  field types are simple strings/nulls; there's no human-editing intent. JSON
  round-trips faster and doesn't require a write library beyond the stdlib.
- 0600 permissions for the same reason as config.py: the VOX_ENGINES value
  contains internal Docker network URLs (e.g. http://voxxy-engine-voxcpm:8000)
  that reveal internal topology. Group/other read is undesirable.
- last_engine_change stores an ISO 8601 string rather than a datetime object
  because JSON has no datetime type and we want the state file to be
  round-trippable without a custom encoder. Callers that need a datetime parse
  it themselves.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

STATE_FILENAME = ".voxxy.state.json"


@dataclass(slots=True)
class State:
    """Ephemeral project-level state persisted to .voxxy.state.json.

    vox_engines mirrors the VOX_ENGINES env var format understood by voxxy-core:
    a comma-separated list of ``name=url`` pairs. Persisted here so
    'daemon start' can inject the user's last 'engine use' choice even after a
    host reboot.
    """

    vox_engines: str = ""
    last_engine_change: Optional[str] = None   # ISO 8601 string
    last_engine_change_by: Optional[str] = None  # human-readable action description


def load_state(project_root: Path) -> State:
    """Read {project_root}/.voxxy.state.json if present; return defaults otherwise.

    Missing file is not an error — a fresh project has no state yet. Callers that
    need a non-empty vox_engines should check the field and fall back to the
    compose.yml default.
    """
    path = project_root / STATE_FILENAME
    if not path.is_file():
        return State()

    with open(path, encoding="utf-8") as fh:
        raw = json.loads(fh.read())

    return State(
        vox_engines=raw.get("vox_engines", ""),
        last_engine_change=raw.get("last_engine_change"),
        last_engine_change_by=raw.get("last_engine_change_by"),
    )


def save_state(project_root: Path, state: State) -> None:
    """Write {project_root}/.voxxy.state.json with 0600 permissions.

    Uses os.open with O_CREAT|O_WRONLY|O_TRUNC to set 0600 atomically at file
    creation rather than chmod-after-write (which has a TOCTOU window). The state
    file is always fully rewritten; it's small enough that partial writes are not
    a concern worth adding complexity for.
    """
    path = project_root / STATE_FILENAME
    content = json.dumps(asdict(state), indent=2).encode()

    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
