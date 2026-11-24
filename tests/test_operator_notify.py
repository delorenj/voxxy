"""Operator notices for the consent review queue: one Bloodbank event per held voice and per decision,
over a real (fake) NATS socket, off without VOX_BLOODBANK_NATS_URL, and never in the request's way."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import operator_notify as on


class FakeNats:
    """Speaks just enough NATS: INFO, then records each PUB and answers PING with PONG."""

    def __init__(self, err: bytes | None = None) -> None:
        self.published: list[tuple[str, dict]] = []
        self.err = err

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b'INFO {"server_id":"fake"}\r\n')
        await writer.drain()
        while line := await reader.readline():
            if line.startswith(b"PUB"):
                _, subject, size = line.split()
                body = await reader.readexactly(int(size) + 2)
                if self.err:
                    writer.write(self.err)
                else:
                    self.published.append((subject.decode(), json.loads(body[:-2])))
            elif line.startswith(b"PING"):
                writer.write(b"PONG\r\n")
            await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = f"nats://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


async def _drain() -> None:
    for _ in range(50):
        if not on._tasks:
            return
        await asyncio.sleep(0.02)


def test_a_held_voice_and_its_decision_each_reach_the_bus(monkeypatch) -> None:
    async def run():
        async with FakeNats() as nats:
            monkeypatch.setenv("VOX_BLOODBANK_NATS_URL", nats.url)
            on.review_requested(record_id="6c15db7c3235", voice="jarad", display_name="Jarad",
                                reason="Every account's first voice is checked by a person.")
            await _drain()
            on.review_resolved(record_id="6c15db7c3235", approved=True, note="mine")
            await _drain()
            return nats.published
    published = asyncio.run(run())
    assert [s for s, _ in published] == ["bloodbank.evt.approval.approval_request.created",
                                         "bloodbank.evt.approval.approval_request.resolved"]
    created, resolved = published[0][1], published[1][1]
    assert created["type"] == on.CREATED and created["domain"] == "approval" and created["kind"] == "event"
    assert created["data"]["approval_id"] == "6c15db7c3235" and created["data"]["service"] == "voxxy"
    assert "Jarad" in created["data"]["title"] and "voxxy consent approve|reject 6c15db7c3235" in created["data"]["decide"]
    assert resolved["data"]["decision"] == "approved"
    assert created["correlationid"] == resolved["correlationid"], "request and outcome are one journey"


def test_the_envelope_passes_the_bloodbank_contract() -> None:
    validate = pytest.importorskip("jsonschema")  # noqa: F841 -- only to know the bloodbank checker can run
    import importlib.util
    import pathlib
    path = pathlib.Path.home() / "code/33GOD/bloodbank/services/agent-hooks/core/validate.py"
    if not path.is_file():
        pytest.skip("bloodbank checkout not present")
    spec = importlib.util.spec_from_file_location("bb_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for t, data in ((on.CREATED, {"approval_id": "a", "service": "voxxy", "approval_kind": on.KIND, "title": "t", "summary": "s"}),
                    (on.RESOLVED, {"approval_id": "a", "service": "voxxy", "approval_kind": on.KIND, "decision": "rejected"})):
        env = on.envelope(t, data)
        mod.assert_contract(env)
        if hasattr(mod, "validate_envelope"):
            mod.validate_envelope(env)


def test_off_without_a_bus_url(monkeypatch) -> None:
    monkeypatch.delenv("VOX_BLOODBANK_NATS_URL", raising=False)
    async def run():
        on.review_requested(record_id="x", voice="v", display_name="V")
        return len(on._tasks)
    assert asyncio.run(run()) == 0


def test_a_refusing_or_absent_bus_is_a_log_line_not_an_error(monkeypatch, caplog) -> None:
    async def run():
        async with FakeNats(err=b"-ERR 'Permissions Violation'\r\n") as nats:
            monkeypatch.setenv("VOX_BLOODBANK_NATS_URL", nats.url)
            assert await on._send(on.CREATED, {"approval_id": "x"}) is False
        monkeypatch.setenv("VOX_BLOODBANK_NATS_URL", "nats://127.0.0.1:1")
        assert await on._send(on.CREATED, {"approval_id": "y"}) is False
    asyncio.run(run())
    assert "operator notice not sent" in caplog.text
