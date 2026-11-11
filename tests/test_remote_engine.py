"""RemoteEngineClient: what marks an engine unhealthy, what an error may carry, what it learns from /healthz.

The silent ElevenLabs fallback came from core marking voxcpm unhealthy while it was merely busy. Only
evidence the engine is *down* (refused connection, connect timeout, 5xx, failed probe) may mark it so;
a read timeout (busy) must not.
"""

from __future__ import annotations

import base64
import time

import httpx
import pytest

from app.engines import PermanentEngineError, RemoteEngineClient

pytestmark = pytest.mark.anyio

OK_BODY = {"wav_b64": base64.b64encode(b"RIFF-ok").decode(), "sample_rate": 48_000, "engine": "voxcpm", "bytes": 7}


def _client(handler) -> RemoteEngineClient:
    c = RemoteEngineClient("voxcpm", "http://engine", timeout=5.0, transport=httpx.MockTransport(handler))
    c._health_cache = (time.monotonic(), True)
    return c


async def test_read_timeout_leaves_the_engine_healthy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("still generating", request=request)

    c = _client(handler)
    with pytest.raises(RuntimeError, match="timed out"):
        await c.generate(text="a very long utterance")
    assert c.available() is True


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError])
async def test_down_engine_is_marked_unhealthy(exc_type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("gone", request=request)

    c = _client(handler)
    with pytest.raises(RuntimeError):
        await c.generate(text="hi")
    assert c.available() is False


async def test_5xx_marks_unhealthy_and_keeps_only_the_envelope() -> None:
    c = _client(lambda r: httpx.Response(500, json={"error": {"code": "OOM", "message": "CUDA out of memory"}}))
    with pytest.raises(RuntimeError, match="OOM: CUDA out of memory") as info:
        await c.generate(text="hi")
    assert not isinstance(info.value, PermanentEngineError)
    assert c.available() is False


async def test_4xx_is_permanent_and_never_echoes_the_request() -> None:
    echo = {"detail": [{"type": "missing", "input": {"text": "private caption text"}}]}
    c = _client(lambda r: httpx.Response(422, json=echo))
    with pytest.raises(PermanentEngineError) as info:
        await c.generate(text="private caption text")
    assert "private caption text" not in str(info.value)
    assert "withheld" in str(info.value)
    assert c.available() is True  # bad input says nothing about engine health


async def test_engine_invalid_input_message_is_kept() -> None:
    body = {"error": {"code": "INVALID_INPUT", "message": "reference audio is not readable audio (LibsndfileError)"}}
    c = _client(lambda r: httpx.Response(400, json=body))
    with pytest.raises(PermanentEngineError, match="INVALID_INPUT: reference audio is not readable"):
        await c.generate(text="hi", reference_audio=b"junk", reference_sample_rate=16_000)


async def test_success_returns_the_wav_and_marks_healthy() -> None:
    c = _client(lambda r: httpx.Response(200, json=OK_BODY))
    c._health_cache = (time.monotonic(), False)
    result = await c.generate(text="hi")
    assert result.wav_bytes == b"RIFF-ok" and result.engine == "voxcpm"
    assert c.available() is True


async def test_capabilities_come_from_healthz() -> None:
    health = {
        "engine": "voxcpm", "ready": True, "model_loaded": True,
        "capabilities": {"accepts_reference_audio": False, "output_sample_rate": 24_000},
    }
    c = _client(lambda r: httpx.Response(200, json=health))
    assert c.accepts_reference is True  # contract default before any probe
    assert await c.refresh_health() is True
    assert c.accepts_reference is False
