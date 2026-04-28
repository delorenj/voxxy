"""Tests for SSH session detection and PulseAudio forwarding in `voxxy speak`."""

import os
import socket
from unittest.mock import MagicMock, patch

import pytest

from voxxy.commands.speak import _is_ssh_session, _pulseaudio_forwarded


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
