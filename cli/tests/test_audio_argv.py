"""Tests for the ffmpeg argv builder in voxxy.audio.

These tests exercise _build_preprocess_argv as a pure function — no ffmpeg
subprocess is spawned, no tmp files are touched. CI-safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voxxy.audio import _build_preprocess_argv


SRC = Path("/data/voices/rick.wav")
DST = Path("/tmp/rick_proc.wav")


class TestBuildPreprocessArgv:
    def test_basic_structure(self) -> None:
        """argv starts with ffmpeg and contains src/dst in the right positions."""
        argv = _build_preprocess_argv(SRC, DST)
        assert argv[0] == "ffmpeg"
        assert str(SRC) in argv
        assert str(DST) in argv

    def test_overwrite_flag(self) -> None:
        """-y (overwrite) is always present."""
        argv = _build_preprocess_argv(SRC, DST)
        assert "-y" in argv

    def test_default_sample_rate(self) -> None:
        """Default sample rate is 24000 Hz."""
        argv = _build_preprocess_argv(SRC, DST)
        idx = argv.index("-ar")
        assert argv[idx + 1] == "24000"

    def test_custom_sample_rate(self) -> None:
        argv = _build_preprocess_argv(SRC, DST, sample_rate=16000)
        idx = argv.index("-ar")
        assert argv[idx + 1] == "16000"

    def test_default_channels(self) -> None:
        """Default channel count is 1 (mono)."""
        argv = _build_preprocess_argv(SRC, DST)
        idx = argv.index("-ac")
        assert argv[idx + 1] == "1"

    def test_stereo_channels(self) -> None:
        argv = _build_preprocess_argv(SRC, DST, channels=2)
        idx = argv.index("-ac")
        assert argv[idx + 1] == "2"

    def test_default_trim_seconds(self) -> None:
        """Default trim is 8.0 seconds."""
        argv = _build_preprocess_argv(SRC, DST)
        idx = argv.index("-t")
        assert float(argv[idx + 1]) == pytest.approx(8.0)

    def test_custom_trim_seconds(self) -> None:
        argv = _build_preprocess_argv(SRC, DST, trim_seconds=30.0)
        idx = argv.index("-t")
        assert float(argv[idx + 1]) == pytest.approx(30.0)

    def test_input_flag_precedes_src(self) -> None:
        """-i immediately precedes the src path."""
        argv = _build_preprocess_argv(SRC, DST)
        idx = argv.index("-i")
        assert argv[idx + 1] == str(SRC)

    def test_dst_is_last_argument(self) -> None:
        """Output path is the final argument (ffmpeg convention)."""
        argv = _build_preprocess_argv(SRC, DST)
        assert argv[-1] == str(DST)

    def test_returns_list_of_strings(self) -> None:
        """Return type must be list[str] for subprocess compatibility."""
        argv = _build_preprocess_argv(SRC, DST)
        assert isinstance(argv, list)
        assert all(isinstance(a, str) for a in argv)

    def test_different_src_and_dst_paths(self) -> None:
        """Both src and dst paths appear exactly once each."""
        src = Path("/voices/custom.mp3")
        dst = Path("/tmp/out.wav")
        argv = _build_preprocess_argv(src, dst)
        assert argv.count(str(src)) == 1
        assert argv.count(str(dst)) == 1
