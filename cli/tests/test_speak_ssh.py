"""Tests for SSH session detection and PulseAudio forwarding in `voxxy speak`."""

import os
import socket
import sys
from unittest.mock import MagicMock, patch

import pytest

import voxxy.commands.speak as speak_mod
from voxxy.commands.speak import (
    _default_player,
    _is_ssh_session,
    _play_wav,
    _pulseaudio_forwarded,
    _via_is_local,
)


class TestIsSshSession:
    """_is_ssh_session returns True when any SSH env var is present."""

    @pytest.mark.parametrize("var", ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"])
    def test_detects_ssh_via_env_var(self, var: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(var, "some-value")
        assert _is_ssh_session() is True

    def test_no_ssh_when_vars_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            monkeypatch.delenv(var, raising=False)
        assert _is_ssh_session() is False

    def test_detects_ssh_even_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty env vars (e.g. stale tmux/screen sessions) still count."""
        for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("SSH_CONNECTION", "")
        assert _is_ssh_session() is True


class TestPulseaudioForwarded:
    """_pulseaudio_forwarded detects existing or forwarded PulseAudio servers."""

    def test_trusts_existing_pulse_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PULSE_SERVER", "tcp:192.168.1.5:4713")
        assert _pulseaudio_forwarded() == "tcp:192.168.1.5:4713"

    def test_no_ssh_no_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "PULSE_SERVER"):
            monkeypatch.delenv(var, raising=False)
        assert _pulseaudio_forwarded() is None

    def test_detects_localhost_4713_in_ssh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 12345 10.0.0.2 22")
        monkeypatch.delenv("PULSE_SERVER", raising=False)

        # Patch create_connection so it succeeds
        with patch.object(
            socket, "create_connection", return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
        ) as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            result = _pulseaudio_forwarded()
            assert result == "127.0.0.1:4713"
            mock_conn.assert_called_once_with(("127.0.0.1", 4713), timeout=0.3)

    def test_no_forward_when_port_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 12345 10.0.0.2 22")
        monkeypatch.delenv("PULSE_SERVER", raising=False)

        with patch.object(socket, "create_connection", side_effect=OSError("refused")):
            assert _pulseaudio_forwarded() is None


class TestViaIsLocal:
    """_via_is_local guards the VOX_REMOTE_HOST=<self> misconfig so --via to our
    own machine falls through to the direct local path instead of ssh-ing to
    ourselves (which is pointless and breaks --out)."""

    def test_empty_is_not_local(self) -> None:
        assert _via_is_local("") is False

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
    def test_loopback_is_local(self, host: str) -> None:
        assert _via_is_local(host) is True

    def test_own_hostname_is_local(self) -> None:
        assert _via_is_local(socket.gethostname()) is True

    def test_strips_user_prefix(self) -> None:
        assert _via_is_local("someuser@localhost") is True

    def test_remote_ip_is_not_local(self) -> None:
        # 192.0.2.1 is TEST-NET-1 (RFC 5737): never a local interface, and with
        # no network route the probe raises and is swallowed — False either way.
        assert _via_is_local("192.0.2.1") is False


class TestPlayerSelection:
    """Playback is cross-platform: afplay (file) on macOS, paplay (stdin) on Linux."""

    def test_default_player_is_afplay_on_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        assert _default_player() == "afplay"

    def test_default_player_is_paplay_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert _default_player() == "paplay"

    def test_afplay_gets_a_file_arg_not_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """afplay can't read stdin — it must receive a temp file path."""
        monkeypatch.setattr(speak_mod.shutil, "which", lambda b: "/usr/bin/" + b)
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return MagicMock(returncode=0)

        monkeypatch.setattr(speak_mod.subprocess, "run", fake_run)
        _play_wav(b"RIFFxxxxWAVE", "afplay")

        assert len(seen["argv"]) == 2 and seen["argv"][0] == "afplay"
        assert "input" not in seen["kwargs"]  # file arg, never stdin

    def test_paplay_gets_bytes_on_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(speak_mod.shutil, "which", lambda b: "/usr/bin/" + b)
        monkeypatch.delenv("PULSE_SERVER", raising=False)
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return MagicMock(returncode=0)

        monkeypatch.setattr(speak_mod.subprocess, "run", fake_run)
        _play_wav(b"RIFFxxxxWAVE", "paplay")

        assert seen["argv"] == ["paplay"]
        assert seen["kwargs"].get("input") == b"RIFFxxxxWAVE"
