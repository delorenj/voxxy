"""Shared pytest fixtures for the voxxy CLI test suite.

All fixtures here are CI-safe: no network calls, no docker subprocesses,
no real filesystem paths outside of tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def fake_voxxy_root(tmp_path: Path) -> Path:
    """Create a minimal fake voxxy project root in tmp_path.

    A directory qualifies as a voxxy root when it contains BOTH ``compose.yml``
    AND an ``engines/`` subdirectory — mirroring the ``_is_voxxy_root`` check
    in config.py.
    """
    root = tmp_path / "voxxy-project"
    root.mkdir()
    (root / "compose.yml").write_text("# fake compose\n")
    (root / "engines").mkdir()
    return root
