"""Tests for voxxy.state: round-trip, file permissions, defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from voxxy.state import State, STATE_FILENAME, load_state, save_state


class TestStateRoundTrip:
    def test_save_then_load(self, fake_voxxy_root: Path) -> None:
        """Saving and reloading State returns the same values."""
        state = State(
            vox_engines="voxcpm=http://voxxy-engine-voxcpm:8000",
            last_engine_change="2026-04-24T12:00:00Z",
            last_engine_change_by="engine use voxcpm",
        )
        save_state(fake_voxxy_root, state)
        loaded = load_state(fake_voxxy_root)

        assert loaded.vox_engines == state.vox_engines
        assert loaded.last_engine_change == state.last_engine_change
        assert loaded.last_engine_change_by == state.last_engine_change_by

    def test_save_then_load_empty_fields(self, fake_voxxy_root: Path) -> None:
        """Optional fields survive round-trip as None."""
        state = State(vox_engines="")
        save_state(fake_voxxy_root, state)
        loaded = load_state(fake_voxxy_root)

        assert loaded.vox_engines == ""
        assert loaded.last_engine_change is None
        assert loaded.last_engine_change_by is None

    def test_load_missing_returns_defaults(self, fake_voxxy_root: Path) -> None:
        """load_state with no file returns a default State."""
        # Ensure there is no state file.
        state_file = fake_voxxy_root / STATE_FILENAME
        assert not state_file.exists()

        loaded = load_state(fake_voxxy_root)
        assert loaded.vox_engines == ""
        assert loaded.last_engine_change is None
        assert loaded.last_engine_change_by is None

    def test_multi_engine_string(self, fake_voxxy_root: Path) -> None:
        """Multiple engines in vox_engines round-trip correctly."""
        engines = (
            "voxcpm=http://voxxy-engine-voxcpm:8000,"
            "vibevoice=http://voxxy-engine-vibevoice:8000"
        )
        save_state(fake_voxxy_root, State(vox_engines=engines))
        loaded = load_state(fake_voxxy_root)
        assert loaded.vox_engines == engines


class TestStatePermissions:
    def test_state_written_0600(self, fake_voxxy_root: Path) -> None:
        """save_state creates the file with 0600 (owner read/write only)."""
        save_state(fake_voxxy_root, State(vox_engines="voxcpm=http://fake:8000"))
        state_file = fake_voxxy_root / STATE_FILENAME
        mode = state_file.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    def test_state_file_created_in_project_root(self, fake_voxxy_root: Path) -> None:
        """The state file lands at {project_root}/.voxxy.state.json."""
        save_state(fake_voxxy_root, State())
        assert (fake_voxxy_root / STATE_FILENAME).is_file()
