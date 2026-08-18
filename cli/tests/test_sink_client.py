"""Tests for the CLI half of the sink relay: SSE decoding + speak's routing rule.

The routing tests exist because the failure mode here is silent and expensive:
if `speak` picks the wrong branch you either hear nothing (routed to a sink with
no listener) or you synthesize the same sentence twice.
"""

from __future__ import annotations

import httpx
import pytest

from voxxy.client import SinkPlayResult, SinkStatusResponse, VoxClient
import voxxy.commands.speak as speak_mod
from voxxy.commands.listen import default_sink_key, resolve_key


def _sse(*frames: str) -> bytes:
    return "".join(frames).encode()


def _client_returning(body: bytes, *, status: int = 200) -> VoxClient:
    """A VoxClient whose transport replays a canned SSE body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, content=body, headers={"content-type": "text/event-stream"}
        )

    return VoxClient("http://test", transport=httpx.MockTransport(handler), api_key=None)


class TestSseDecoding:
    def test_yields_events_with_parsed_data(self) -> None:
        client = _client_returning(_sse(
            'event: ready\ndata: {"key": "desk", "listeners": 1}\n\n',
            'event: audio\ndata: {"audio_url": "http://x/a.ogg", "text": "hi"}\n\n',
        ))
        events = list(client.stream_sink_events("desk"))
        assert [e["event"] for e in events] == ["ready", "audio"]
        assert events[1]["data"]["audio_url"] == "http://x/a.ogg"

    def test_heartbeat_comments_are_swallowed(self) -> None:
        """Comment frames keep the stream alive; they must never surface as events."""
        client = _client_returning(_sse(
            ": ping\n\n",
            ": ping\n\n",
            'event: audio\ndata: {"audio_url": "u"}\n\n',
        ))
        events = list(client.stream_sink_events("desk"))
        assert len(events) == 1
        assert events[0]["event"] == "audio"

    def test_event_name_resets_between_frames(self) -> None:
        """A frame without an explicit event: line must not inherit the previous one."""
        client = _client_returning(_sse(
            'event: audio\ndata: {"n": 1}\n\n',
            'data: {"n": 2}\n\n',
        ))
        events = list(client.stream_sink_events("desk"))
        assert [e["event"] for e in events] == ["audio", "message"]

    def test_crlf_line_endings_are_tolerated(self) -> None:
        client = _client_returning(b'event: audio\r\ndata: {"n": 1}\r\n\r\n')
        events = list(client.stream_sink_events("desk"))
        assert events == [{"event": "audio", "data": {"n": 1}}]

    def test_unauthorized_is_typed(self) -> None:
        from voxxy.client import VoxUnauthorized
        client = _client_returning(b"", status=401)
        with pytest.raises(VoxUnauthorized):
            list(client.stream_sink_events("desk"))


class TestSinkKeyResolution:
    def test_flag_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOX_SINK", "from-env")
        assert resolve_key("from-flag", "from-config") == "from-flag"

    def test_env_beats_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOX_SINK", "from-env")
        assert resolve_key(None, "from-config") == "from-env"

    def test_config_beats_hostname(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOX_SINK", raising=False)
        assert resolve_key(None, "from-config") == "from-config"

    def test_hostname_fallback_is_a_legal_sink_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VOX_SINK", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "Delo's MBP.local")
        key = default_sink_key()
        assert key == "Delo-s-MBP"
        assert resolve_key(None, None) == key


class _FakeClient:
    """Records which server calls speak made, so we can assert on the path taken."""

    def __init__(self, listeners: int, delivered: int | None = None) -> None:
        self._listeners = listeners
        self._delivered = listeners if delivered is None else delivered
        self.calls: list[str] = []

    def sink_status(self, key: str) -> SinkStatusResponse:
        self.calls.append("sink_status")
        return SinkStatusResponse(key=key, listeners=self._listeners)

    def synthesize_url(self, **kwargs):
        self.calls.append("synthesize_url")
        from voxxy.client import SynthUrlResponse
        return SynthUrlResponse(
            audio_url="http://x/a.ogg", engine="voxcpm", duration_s=1.0, bytes=10
        )

    def sink_play(self, key: str, audio_url: str, **kwargs) -> SinkPlayResult:
        self.calls.append("sink_play")
        return SinkPlayResult(key=key, delivered=self._delivered, audio_url=audio_url)

    def fetch_audio(self, url: str) -> bytes:
        self.calls.append("fetch_audio")
        return b"OggS-fake"


class TestSpeakSinkRouting:
    def test_routes_to_sink_when_someone_is_listening(self) -> None:
        client = _FakeClient(listeners=1)
        handled = speak_mod._speak_to_sink(client, "desk", "hi", "rick", 2.0, 10, "paplay")
        assert handled is True
        assert client.calls == ["sink_status", "synthesize_url", "sink_play"]

    def test_falls_back_to_local_when_nobody_listens(self) -> None:
        """No listener must cost nothing: probe, then hand back to the local path."""
        client = _FakeClient(listeners=0)
        handled = speak_mod._speak_to_sink(client, "desk", "hi", "rick", 2.0, 10, "paplay")
        assert handled is False
        assert client.calls == ["sink_status"]  # no synthesis wasted

    def test_lost_race_plays_here_without_resynthesizing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Listener vanishes between probe and publish: reuse the audio we have."""
        played: list[bytes] = []
        monkeypatch.setattr(speak_mod, "play_encoded", lambda b, p: played.append(b))

        client = _FakeClient(listeners=1, delivered=0)
        handled = speak_mod._speak_to_sink(client, "desk", "hi", "rick", 2.0, 10, "paplay")
        assert handled is True
        assert client.calls == ["sink_status", "synthesize_url", "sink_play", "fetch_audio"]
        assert played == [b"OggS-fake"]

    def test_unreachable_sink_never_silences_speech(self) -> None:
        """A broken relay degrades to 'wrong machine', never to 'nothing happened'."""
        from voxxy.client import VoxUnreachable

        class Dead(_FakeClient):
            def sink_status(self, key: str):
                self.calls.append("sink_status")
                raise VoxUnreachable("connection refused")

        client = Dead(listeners=0)
        assert speak_mod._speak_to_sink(client, "desk", "hi", "rick", 2.0, 10, "paplay") is False
