"""Tell the operator when Voxxy is waiting on them.

Today that is the consent review queue: a voice held for a person's check sat "under review" with
nobody told. Each held voice becomes one ``bloodbank.approval.approval_request.created`` event on the
33GOD Bloodbank bus (NATS), which the ntfy router pushes to the operator's phone; the decision becomes
``approval_request.resolved``. Off unless ``VOX_BLOODBANK_NATS_URL`` is set (a self-hosted install has no
bus), and never in the request's way: publishing runs in the background, bounded, and a failure is a log
line, never an error for the person saving a voice.

No client library: one PUB over the NATS text protocol, then PING/PONG so we know the server took it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SERVICE = "voxxy"
KIND = "voice_consent"
CREATED = "bloodbank.approval.approval_request.created"
RESOLVED = "bloodbank.approval.approval_request.resolved"
_NS = uuid.NAMESPACE_URL
TIMEOUT_S = 5.0

_tasks: set[asyncio.Task] = set()  # keep background publishes alive until they finish


def nats_url() -> Optional[str]:
    return os.environ.get("VOX_BLOODBANK_NATS_URL", "").strip() or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def envelope(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """A Bloodbank v3 CloudEvents envelope, shaped like ``bb-emit`` builds it."""
    approval_id = data["approval_id"]
    return {
        "specversion": "1.0",
        "id": str(uuid.uuid4()),
        "source": "urn:33god:voxxy:consent",
        "type": event_type,
        "subject": "bloodbank.evt." + event_type[len("bloodbank."):],
        "time": _now(),
        "datacontenttype": "application/json",
        # one request and its outcome share a correlation id
        "correlationid": str(uuid.uuid5(_NS, f"urn:33god:approval:{SERVICE}:{approval_id}")),
        "causationid": None,
        "producer": SERVICE,
        "service": SERVICE,
        "domain": "approval",
        "schemaref": f"{event_type}.v1",
        "dataschema": f"apicurio://holyfields/{event_type}/versions/1",
        "kind": "event",
        "actor": {"type": "service", "agent_id": SERVICE, "cli": None, "provider": None, "model": None},
        "ordering_key": f"approval:{SERVICE}:{approval_id}",
        "data": data,
    }


async def publish(url: str, subject: str, payload: bytes, timeout: float = TIMEOUT_S) -> None:
    """One NATS PUB, confirmed by PING/PONG. Raises on any failure."""
    u = urlparse(url)
    host, port = u.hostname or "127.0.0.1", u.port or 4222

    async def run() -> None:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            info = await reader.readline()
            if not info.startswith(b"INFO"):
                raise ConnectionError(f"not a NATS server: {info[:60]!r}")
            writer.write(b'CONNECT {"verbose":false,"pedantic":false,"name":"voxxy"}\r\n')
            writer.write(f"PUB {subject} {len(payload)}\r\n".encode() + payload + b"\r\nPING\r\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("NATS closed the connection")
                if line.startswith(b"PONG"):
                    return
                if line.startswith(b"-ERR"):
                    raise ConnectionError(line.decode(errors="replace").strip())
                # +OK / PING / INFO updates: keep reading
                if line.startswith(b"PING"):
                    writer.write(b"PONG\r\n")
        finally:
            writer.close()

    await asyncio.wait_for(run(), timeout)


async def _send(event_type: str, data: dict[str, Any]) -> bool:
    url = nats_url()
    if not url:
        return False
    env = envelope(event_type, data)
    try:
        await publish(url, env["subject"], json.dumps(env).encode())
        logger.info("operator notified type=%s approval=%s", event_type, data["approval_id"])
        return True
    except Exception as exc:  # noqa: BLE001 -- a notice must never fail the caller
        logger.warning("operator notice not sent type=%s approval=%s: %r", event_type, data["approval_id"], exc)
        return False


def _background(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no loop (a sync caller): nothing to attach to, skip
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def review_requested(*, record_id: str, voice: str, display_name: str, reason: str = "") -> None:
    """A voice is held for the operator's check. Fire and forget."""
    if not nats_url():
        return
    data = {
        "approval_id": record_id,
        "service": SERVICE,
        "approval_kind": KIND,
        "title": f"Voxxy: voice \"{display_name}\" is waiting for your review",
        "summary": (f"\"{display_name}\" ({voice}) was recorded and is held until a person checks it. "
                    + (reason + " " if reason else "") + "It cannot speak until you approve it."),
        "decide": f"voxxy consent audio {record_id}, then voxxy consent approve|reject {record_id}",
        "reason": reason or None,
        "requested_at": _now(),
    }
    _background(_send(CREATED, data))


def review_resolved(*, record_id: str, approved: bool, note: Optional[str] = None) -> None:
    """The operator decided. Fire and forget."""
    if not nats_url():
        return
    data = {
        "approval_id": record_id,
        "service": SERVICE,
        "approval_kind": KIND,
        "decision": "approved" if approved else "rejected",
        "note": note,
        "resolved_at": _now(),
    }
    _background(_send(RESOLVED, data))
