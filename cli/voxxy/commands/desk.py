"""`voxxy sink on|off|toggle`: make this Linux desk a speaker for its sink.

"Speak on this desk" in one command. `on` writes a systemd *user* unit that runs
`voxxy listen --key <sink>` and starts it; `off` stops and disables it (the unit
file stays, so `on` is instant next time); `toggle` flips it. `voxxy sink status
--json` reports the local unit next to the server's listener count.

HeyMa's tray row and `heyma setup` call these commands and never write the unit
themselves (the joining-HeyMa contract: actions are the product's own CLI).

The key the unit listens on is resolved once, at `on` (--key, else this
machine's hostname; never $VOX_SINK, which names where a shell *sends*), and
baked into ExecStart, so the unit, the status read and every caller that sends
to this desk agree on one name.

A user unit, not a system one: playback needs the user's PipeWire/PulseAudio
socket. macOS listeners are HeyMa.app's job (native, no Python), so `on` refuses
there with a pointer.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

UNIT_NAME = "voxxy-listen.service"
_KEY_IN_EXEC = re.compile(r"\s--key\s+(\S+)")

Run = Callable[[list[str]], subprocess.CompletedProcess]


def unit_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / ".config" / "systemd" / "user" / UNIT_NAME


def voxxy_bin() -> str:
    """The stable launcher path (uv tool's ~/.local/bin shim), not a venv's python."""
    local = Path.home() / ".local" / "bin" / "voxxy"
    if local.exists():
        return str(local)
    return shutil.which("voxxy") or str(local)


def unit_text(key: str, url: str, binary: str) -> str:
    return f"""# Written by `voxxy sink on` ({key}). Re-run it to change the key or URL.
# `voxxy sink off` stops and disables this unit; HeyMa's "Speak on this desk" toggles it.
[Unit]
Description=Voxxy speaker for sink {key} (play what callers send to this desk)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
Environment=VOX_URL={url}
ExecStart={binary} listen --key {key}
# The listener reconnects on its own; Restart covers an exit anyway, and RestartSec
# keeps a hard failure from spinning.
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def baked_key(text: str) -> Optional[str]:
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            match = _KEY_IN_EXEC.search(line)
            return match.group(1) if match else None
    return None


def _systemctl(run: Run, *args: str) -> subprocess.CompletedProcess:
    return run(["systemctl", "--user", *args])


def _default_run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


def local_state(run: Run = _default_run, home: Optional[Path] = None) -> dict:
    """{unit, installed, enabled, active, key}: this desk's listener unit, read-only."""
    path = unit_path(home)
    state = {"unit": UNIT_NAME, "installed": path.is_file(), "enabled": False,
             "active": False, "key": None}
    if not state["installed"]:
        return state
    try:
        state["key"] = baked_key(path.read_text())
    except OSError:
        pass
    state["enabled"] = _systemctl(run, "is-enabled", UNIT_NAME).stdout.strip() == "enabled"
    state["active"] = _systemctl(run, "is-active", UNIT_NAME).stdout.strip() == "active"
    return state


class DeskError(RuntimeError):
    pass


def turn_on(key: str, url: str, run: Run = _default_run, home: Optional[Path] = None,
            binary: Optional[str] = None, platform: str = sys.platform) -> dict:
    if not platform.startswith("linux"):
        raise DeskError("`voxxy sink on` manages a systemd user unit (Linux). On a Mac, "
                        "HeyMa.app's Voxxy section is the speaker.")
    path = unit_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = unit_text(key, url, binary or voxxy_bin())
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)
        _check(_systemctl(run, "daemon-reload"), "daemon-reload")
        # A running unit keeps its old ExecStart until restarted.
        _systemctl(run, "try-restart", UNIT_NAME)
    _check(_systemctl(run, "enable", "--now", UNIT_NAME), f"enable --now {UNIT_NAME}")
    return local_state(run, home)


def turn_off(run: Run = _default_run, home: Optional[Path] = None) -> dict:
    if unit_path(home).is_file():
        _check(_systemctl(run, "disable", "--now", UNIT_NAME), f"disable --now {UNIT_NAME}")
    return local_state(run, home)


def _check(done: subprocess.CompletedProcess, what: str) -> None:
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise DeskError(f"systemctl --user {what} failed: {detail[0] if detail else done.returncode}")


def env_url(cfg_url: str, explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("VOX_URL") or cfg_url).rstrip("/")
