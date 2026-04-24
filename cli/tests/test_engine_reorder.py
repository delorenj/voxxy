"""Tests for the pure _reorder_engines function in voxxy.commands.engine."""

from __future__ import annotations

import pytest

from voxxy.commands.engine import _reorder_engines, ENGINE_URLS


# Helpers: canonical two-engine starting state.
VOXCPM_URL = ENGINE_URLS["voxcpm"]
VIBEVOICE_URL = ENGINE_URLS["vibevoice"]

DEFAULT_CHAIN: list[tuple[str, str]] = [
    ("voxcpm", VOXCPM_URL),
    ("vibevoice", VIBEVOICE_URL),
]


# ---------------------------------------------------------------------------
# use
# ---------------------------------------------------------------------------


class TestUseAction:
    def test_use_already_primary_is_noop(self) -> None:
        """use on the already-primary engine keeps order unchanged."""
        result = _reorder_engines(DEFAULT_CHAIN, "use", "voxcpm")
        assert result == DEFAULT_CHAIN

    def test_use_secondary_promotes_to_primary(self) -> None:
        """use on the secondary engine swaps order."""
        result = _reorder_engines(DEFAULT_CHAIN, "use", "vibevoice")
        assert result[0] == ("vibevoice", VIBEVOICE_URL)
        assert result[1] == ("voxcpm", VOXCPM_URL)
        assert len(result) == 2

    def test_use_preserves_relative_order_of_remaining(self) -> None:
        """With three engines, use keeps the others in original relative order."""
        chain = [
            ("voxcpm", VOXCPM_URL),
            ("vibevoice", VIBEVOICE_URL),
            ("other", "http://other:8000"),
        ]
        result = _reorder_engines(chain, "use", "other")
        assert result[0][0] == "other"
        assert [n for n, _ in result[1:]] == ["voxcpm", "vibevoice"]

    def test_use_absent_engine_in_known_urls(self) -> None:
        """use on an engine not currently in chain but in ENGINE_URLS adds it at pos 0."""
        chain: list[tuple[str, str]] = [("vibevoice", VIBEVOICE_URL)]
        result = _reorder_engines(chain, "use", "voxcpm")
        assert result[0] == ("voxcpm", VOXCPM_URL)
        assert result[1] == ("vibevoice", VIBEVOICE_URL)

    def test_use_completely_unknown_engine_raises(self) -> None:
        """use on an engine unknown to ENGINE_URLS and not in chain raises KeyError."""
        chain: list[tuple[str, str]] = [("voxcpm", VOXCPM_URL)]
        with pytest.raises(KeyError, match="unknown engine"):
            _reorder_engines(chain, "use", "nonexistent")


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------


class TestEnableAction:
    def test_enable_absent_engine(self) -> None:
        """enable appends the engine to the end."""
        chain: list[tuple[str, str]] = [("voxcpm", VOXCPM_URL)]
        result = _reorder_engines(chain, "enable", "vibevoice")
        assert result == [("voxcpm", VOXCPM_URL), ("vibevoice", VIBEVOICE_URL)]

    def test_enable_already_present_is_noop(self) -> None:
        """enable when already present returns the same chain unchanged."""
        result = _reorder_engines(DEFAULT_CHAIN, "enable", "voxcpm")
        assert result == DEFAULT_CHAIN

    def test_enable_already_present_does_not_duplicate(self) -> None:
        """enable never produces duplicate entries."""
        result = _reorder_engines(DEFAULT_CHAIN, "enable", "vibevoice")
        names = [n for n, _ in result]
        assert names.count("vibevoice") == 1

    def test_enable_unknown_engine_raises(self) -> None:
        """enable on an engine not in ENGINE_URLS raises KeyError."""
        chain: list[tuple[str, str]] = [("voxcpm", VOXCPM_URL)]
        with pytest.raises(KeyError, match="unknown engine"):
            _reorder_engines(chain, "enable", "nonexistent")


# ---------------------------------------------------------------------------
# disable
# ---------------------------------------------------------------------------


class TestDisableAction:
    def test_disable_secondary_removes_it(self) -> None:
        """disable removes the named engine from a two-engine chain."""
        result = _reorder_engines(DEFAULT_CHAIN, "disable", "vibevoice")
        assert result == [("voxcpm", VOXCPM_URL)]

    def test_disable_primary_demotes_it(self) -> None:
        """disable removes primary, leaving secondary as sole engine."""
        result = _reorder_engines(DEFAULT_CHAIN, "disable", "voxcpm")
        assert result == [("vibevoice", VIBEVOICE_URL)]

    def test_disable_last_local_engine_raises_without_force(self) -> None:
        """Disabling the only local engine raises ValueError unless force=True."""
        chain: list[tuple[str, str]] = [("voxcpm", VOXCPM_URL)]
        with pytest.raises(ValueError, match="only local engine"):
            _reorder_engines(chain, "disable", "voxcpm", force=False)

    def test_disable_last_local_engine_allowed_with_force(self) -> None:
        """--force allows disabling the last local engine, leaving an empty chain."""
        chain: list[tuple[str, str]] = [("voxcpm", VOXCPM_URL)]
        result = _reorder_engines(chain, "disable", "voxcpm", force=True)
        assert result == []

    def test_disable_engine_not_in_chain(self) -> None:
        """Disabling an engine that is not in the chain is a no-op (returns same list)."""
        result = _reorder_engines(DEFAULT_CHAIN, "disable", "vibevoice")
        # vibevoice removed; voxcpm remains
        assert ("vibevoice", VIBEVOICE_URL) not in result
        assert ("voxcpm", VOXCPM_URL) in result

    def test_disable_with_three_engines(self) -> None:
        """Disable preserves order of remaining engines."""
        chain = [
            ("voxcpm", VOXCPM_URL),
            ("vibevoice", VIBEVOICE_URL),
            ("other", "http://other:8000"),
        ]
        result = _reorder_engines(chain, "disable", "vibevoice")
        assert [n for n, _ in result] == ["voxcpm", "other"]


# ---------------------------------------------------------------------------
# VOX_ENGINES string round-trip
# ---------------------------------------------------------------------------


class TestVoxEnginesString:
    """Verify _reorder_engines output feeds correctly into _render_vox_engines."""

    def test_use_produces_correct_vox_engines_string(self) -> None:
        from voxxy.commands.engine import _render_vox_engines

        result = _reorder_engines(DEFAULT_CHAIN, "use", "vibevoice")
        rendered = _render_vox_engines(result)
        assert rendered.startswith("vibevoice=")
        assert "voxcpm=" in rendered

    def test_disable_produces_single_entry_string(self) -> None:
        from voxxy.commands.engine import _render_vox_engines

        result = _reorder_engines(DEFAULT_CHAIN, "disable", "vibevoice")
        rendered = _render_vox_engines(result)
        assert rendered == f"voxcpm={VOXCPM_URL}"
        assert "vibevoice" not in rendered
