"""Tests for the sink relay: registry semantics + a real SSE round-trip.

The registry tests pin the two behaviors the CLI depends on for correctness:
``publish`` reporting a delivery count (so ``speak`` knows whether to fall back
to local playback) and drop-oldest overflow (so a wedged listener degrades to
"missed some" rather than "grew the server").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time

import pytest
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from app import sink as sink_relay
from app.auth import is_public_request


# ---------------------------------------------------------------- key shape

@pytest.mark.parametrize("key", ["a", "delo-macbook", "MBP.local", "a_b-c.d", "x" * 64])
def test_valid_keys_accepted(key: str) -> None:
    assert sink_relay.validate_key(key) == key


@pytest.mark.parametrize("key", ["", "-leading", ".dot", "has space", "sla/sh", "x" * 65, "é"])
def test_invalid_keys_rejected(key: str) -> None:
    with pytest.raises(sink_relay.SinkKeyError):
        sink_relay.validate_key(key)


# ---------------------------------------------------------------- registry

@pytest.mark.anyio
async def test_publish_with_no_listener_reports_zero() -> None:
    reg = sink_relay.SinkRegistry()
    assert reg.publish("nobody", {"audio_url": "x"}) == 0


@pytest.mark.anyio
async def test_publish_fans_out_to_every_listener() -> None:
    reg = sink_relay.SinkRegistry()
    async with reg.subscribe("k") as q1, reg.subscribe("k") as q2:
        assert reg.listener_count("k") == 2
        assert reg.publish("k", {"audio_url": "u"}) == 2
        assert q1.get_nowait() == {"audio_url": "u"}
        assert q2.get_nowait() == {"audio_url": "u"}


@pytest.mark.anyio
async def test_listener_is_deregistered_on_exit() -> None:
    reg = sink_relay.SinkRegistry()
    async with reg.subscribe("k"):
        assert reg.listener_count("k") == 1
    assert reg.listener_count("k") == 0
    assert reg.keys() == []  # empty key must not hold a slot forever


@pytest.mark.anyio
async def test_full_queue_evicts_oldest_not_newest() -> None:
    """Stale speech is worthless; the freshest utterance must survive."""
    reg = sink_relay.SinkRegistry()
    async with reg.subscribe("k") as q:
        for i in range(sink_relay.QUEUE_MAXSIZE + 5):
            assert reg.publish("k", {"n": i}) == 1
        drained = [q.get_nowait()["n"] for _ in range(q.qsize())]
    assert len(drained) == sink_relay.QUEUE_MAXSIZE
    assert drained[-1] == sink_relay.QUEUE_MAXSIZE + 4  # newest survived
    assert drained[0] == 5  # the first five were evicted


@pytest.mark.anyio
async def test_listener_cap_is_enforced() -> None:
    reg = sink_relay.SinkRegistry()
    ctxs = [reg.subscribe("k") for _ in range(sink_relay.MAX_LISTENERS_PER_KEY)]
    for c in ctxs:
        await c.__aenter__()
    try:
        with pytest.raises(sink_relay.SinkCapacityError):
            async with reg.subscribe("k"):
                pass
    finally:
        for c in ctxs:
            await c.__aexit__(None, None, None)


# ---------------------------------------------------------------- SSE frames

def test_sse_frame_shape() -> None:
    frame = sink_relay.sse_frame("audio", {"audio_url": "u"}).decode()
    assert frame.startswith("event: audio\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {"audio_url": "u"}


def test_sse_comment_is_a_comment() -> None:
    assert sink_relay.sse_comment().decode() == ": ping\n\n"


# ---------------------------------------------------------------- auth posture

def test_sink_routes_are_not_public() -> None:
    """The relay reaches the human's speakers; it must sit behind VOX_API_KEY."""
    assert not is_public_request("GET", "/sink/delo-macbook/events")
    assert not is_public_request("GET", "/sink/delo-macbook")
    assert not is_public_request("POST", "/sink/delo-macbook/play")


# ---------------------------------------------------------------- round-trip
#
# These run against a real uvicorn on a real socket rather than an in-process
# ASGI transport. Two reasons: the event stream is an infinite generator, which
# an in-process transport has no way to tear down (the test simply hangs), and
# the cleanup path we most need to prove -- a listener being deregistered when
# its client vanishes -- only exists because the server cancels the generator on
# a real disconnect.


def _relay_app(reg: sink_relay.SinkRegistry) -> FastAPI:
    """Mount the relay routes exactly as app/main.py does, minus the DB-backed ones."""
    app = FastAPI()

    @app.get("/sink/{key}")
    async def status(key: str) -> dict:
        return {"key": key, "listeners": reg.listener_count(key)}

    @app.post("/sink/{key}/play")
    async def play(key: str, body: dict) -> dict:
        try:
            sink_relay.validate_key(key)
        except sink_relay.SinkKeyError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "key": key,
            "delivered": reg.publish(key, body),
            "audio_url": body.get("audio_url"),
        }

    @app.get("/sink/{key}/events")
    async def events(key: str) -> StreamingResponse:
        async def stream():
            async with reg.subscribe(key) as q:
                async for frame in sink_relay.event_stream(reg, key, q):
                    yield frame

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers=sink_relay.SSE_HEADERS
        )

    return app


@contextlib.contextmanager
def _running(app: FastAPI):
    """Serve `app` on an ephemeral port for the duration of the block."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("uvicorn died during startup")
        time.sleep(0.02)
    assert server.started, "uvicorn did not start in time"

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def _next_frame(lines) -> tuple[str, dict]:
    """Read one event/data SSE frame, skipping heartbeat comments."""
    event, data = None, None
    for raw in lines:
        line = raw.rstrip("\r")
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data = json.loads(line.split(":", 1)[1].strip())
        elif line == "" and event is not None:
            return event, (data or {})
    raise AssertionError("stream ended before a complete frame arrived")


def test_publish_reaches_a_live_sse_subscriber() -> None:
    """End-to-end over a real socket: subscribe, publish, read the frame back."""
    reg = sink_relay.SinkRegistry()
    with _running(_relay_app(reg)) as base:
        with httpx.Client(base_url=base, timeout=10) as client:
            with client.stream("GET", "/sink/desk/events") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                assert resp.headers["x-accel-buffering"] == "no"
                assert "no-cache" in resp.headers["cache-control"]

                lines = resp.iter_lines()
                event, data = _next_frame(lines)
                assert event == "ready"
                assert data["key"] == "desk"

                # A second connection publishes while the first is still open.
                with httpx.Client(base_url=base, timeout=10) as publisher:
                    posted = publisher.post(
                        "/sink/desk/play",
                        json={
                            "audio_url": "https://vox.delo.sh/audio/abc.ogg",
                            "text": "hi",
                        },
                    ).json()
                assert posted["delivered"] == 1

                event, data = _next_frame(lines)
                assert event == "audio"
                assert data["audio_url"] == "https://vox.delo.sh/audio/abc.ogg"
                assert data["text"] == "hi"

            # Client hung up: the server must drop the listener, or an abandoned
            # sink would report phantom listeners forever and `speak` would keep
            # routing audio into the void.
            deadline = time.monotonic() + 5
            while reg.listener_count("desk") and time.monotonic() < deadline:
                time.sleep(0.05)
            assert reg.listener_count("desk") == 0


def test_status_reports_zero_when_nobody_listens() -> None:
    reg = sink_relay.SinkRegistry()
    with _running(_relay_app(reg)) as base:
        resp = httpx.get(f"{base}/sink/desk", timeout=10)
    assert resp.json() == {"key": "desk", "listeners": 0}


def test_play_rejects_a_malformed_key() -> None:
    reg = sink_relay.SinkRegistry()
    with _running(_relay_app(reg)) as base:
        resp = httpx.post(f"{base}/sink/bad key/play", json={"audio_url": "u"}, timeout=10)
    assert resp.status_code == 422
