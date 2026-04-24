"""Tests for voxxy.config: round-trip, file permissions, project discovery."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from voxxy.config import (
    Config,
    ProjectNotFound,
    _is_voxxy_root,
    discover_project_root,
    load_config,
    save_config,
)


# ---------------------------------------------------------------------------
# Config round-trip
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    def test_save_then_load_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Saving and reloading a default Config returns the same values."""
        config_path = tmp_path / "voxxy" / "config.toml"
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", config_path)

        cfg = Config()
        save_config(cfg)

        loaded = load_config()
        assert loaded.default_url == cfg.default_url
        assert loaded.default_voice == cfg.default_voice
        assert loaded.project_root is None

    def test_save_then_load_with_project_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """project_root round-trips correctly."""
        config_path = tmp_path / "voxxy" / "config.toml"
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", config_path)

        project_dir = tmp_path / "my-voxxy"
        project_dir.mkdir()

        cfg = Config(project_root=project_dir, default_url="http://localhost:9999")
        save_config(cfg)

        loaded = load_config()
        assert loaded.default_url == "http://localhost:9999"
        assert loaded.project_root == project_dir

    def test_load_missing_returns_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_config on a non-existent file returns hardcoded defaults."""
        config_path = tmp_path / "does-not-exist" / "config.toml"
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", config_path)

        cfg = load_config()
        assert cfg.default_url == "https://vox.delo.sh"
        assert cfg.default_voice == "rick"
        assert cfg.project_root is None


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


class TestConfigPermissions:
    def test_config_written_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """save_config creates the file with 0600 (owner read/write only)."""
        config_path = tmp_path / "voxxy" / "config.toml"
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", config_path)

        save_config(Config())

        mode = config_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# _is_voxxy_root
# ---------------------------------------------------------------------------


class TestIsVoxxyRoot:
    def test_valid_root(self, fake_voxxy_root: Path) -> None:
        assert _is_voxxy_root(fake_voxxy_root) is True

    def test_missing_engines(self, tmp_path: Path) -> None:
        d = tmp_path / "no-engines"
        d.mkdir()
        (d / "compose.yml").write_text("")
        # no engines/ dir
        assert _is_voxxy_root(d) is False

    def test_missing_compose(self, tmp_path: Path) -> None:
        d = tmp_path / "no-compose"
        d.mkdir()
        (d / "engines").mkdir()
        # no compose.yml
        assert _is_voxxy_root(d) is False

    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert _is_voxxy_root(d) is False


# ---------------------------------------------------------------------------
# discover_project_root
# ---------------------------------------------------------------------------


class TestDiscoverProjectRoot:
    def test_cli_flag_wins(self, fake_voxxy_root: Path) -> None:
        """cli_flag pointing at a valid root returns that root."""
        result = discover_project_root(cli_flag=fake_voxxy_root)
        assert result == fake_voxxy_root.resolve()

    def test_cli_flag_invalid_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cli_flag pointing at a non-voxxy dir still raises ProjectNotFound.

        We also neutralize VOXXY_HOME, config, and cwd so no other resolver can
        rescue the lookup.
        """
        bad = tmp_path / "not-voxxy"
        bad.mkdir()
        monkeypatch.delenv("VOXXY_HOME", raising=False)
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "nope.toml")
        monkeypatch.chdir(tmp_path)  # tmp_path has no compose.yml + engines/
        with pytest.raises(ProjectNotFound):
            discover_project_root(cli_flag=bad)

    def test_env_var_wins(
        self,
        fake_voxxy_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """VOXXY_HOME env var is used when cli_flag is absent."""
        monkeypatch.setenv("VOXXY_HOME", str(fake_voxxy_root))
        # Point config file away so it doesn't interfere.
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "nope.toml")
        result = discover_project_root()
        assert result == fake_voxxy_root.resolve()

    def test_cwd_walk_up_finds_root(
        self,
        fake_voxxy_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Walking up from a subdirectory of the project root finds it."""
        sub = fake_voxxy_root / "sub" / "deep"
        sub.mkdir(parents=True)

        monkeypatch.delenv("VOXXY_HOME", raising=False)
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "nope.toml")
        monkeypatch.chdir(sub)

        result = discover_project_root()
        assert result == fake_voxxy_root.resolve()

    def test_not_found_raises_project_not_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All resolution methods exhausted raises ProjectNotFound."""
        monkeypatch.delenv("VOXXY_HOME", raising=False)
        monkeypatch.setattr("voxxy.config.CONFIG_PATH", tmp_path / "nope.toml")
        # cd to tmp_path which has no compose.yml + engines/
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ProjectNotFound) as exc_info:
            discover_project_root()

        assert "Could not locate a voxxy project root" in str(exc_info.value)
