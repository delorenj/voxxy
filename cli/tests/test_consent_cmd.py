"""``voxxy consent``: the operator's review queue commands, against a mocked server."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

import voxxy.commands.consent as consent_cmd
from voxxy.app import app
from voxxy.client import VoxClient, env_operator_key

runner = CliRunner()
ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def _review(**kw) -> dict:
    base = dict(id=ID, voice="my-voice", principal="open", sentence="s", code="123456", transcript="t",
                phrase_score=0.9, code_ok=True, speaker_score_reference=None, speaker_score_account=0.52,
                decision="review", reason="We could not tell for sure that the recordings match.",
                has_reference_audio=False, created_at="2026-11-23T20:00:00+00:00")
    base.update(kw)
    return base


class Server:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": {503: "no operator key configured", 403: "this needs the operator key"}[self.status]})
        path = request.url.path
        if path == "/consent/reviews":
            return httpx.Response(200, json=[_review()])
        if path.endswith("/audio"):
            return httpx.Response(200, content=b"RIFFfakewav")
        body = json.loads(request.content)
        return httpx.Response(200, json=_review(decision="accepted" if body["decision"] == "approve" else "rejected",
                                               review_note=body.get("note")))


@pytest.fixture()
def server(monkeypatch):
    srv = Server()
    monkeypatch.setenv("VOX_OPERATOR_KEY", "op-key")
    monkeypatch.setattr(consent_cmd, "load_config", lambda: SimpleNamespace(default_url="https://vox.test"))
    real = VoxClient
    monkeypatch.setattr(consent_cmd, "VoxClient",
                        lambda url, api_key=None: real(url, api_key=api_key, transport=httpx.MockTransport(srv)))
    return srv


def test_operator_key_comes_from_the_env_or_an_op_reference(monkeypatch) -> None:
    monkeypatch.delenv("VOX_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("VOX_OPERATOR_KEY_REF", raising=False)
    assert env_operator_key() is None
    assert env_operator_key({"VOX_OPERATOR_KEY": " direct "}) == "direct"
    import voxxy.client as client_mod
    monkeypatch.setattr(client_mod, "_resolve_ref", lambda ref: f"resolved:{ref}")
    assert env_operator_key({"VOX_OPERATOR_KEY_REF": "op://v/i/f"}) == "resolved:op://v/i/f"
    assert env_operator_key({"VOX_OPERATOR_KEY": "wins", "VOX_OPERATOR_KEY_REF": "op://v/i/f"}) == "wins"


def test_operator_key_is_not_the_api_key(monkeypatch) -> None:
    monkeypatch.setenv("VOX_API_KEY", "ordinary")
    monkeypatch.delenv("VOX_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("VOX_OPERATOR_KEY_REF", raising=False)
    assert env_operator_key() is None


def test_reviews_lists_the_queue_and_sends_the_operator_key(server) -> None:
    result = runner.invoke(app, ["consent", "reviews"])
    assert result.exit_code == 0, result.output
    assert "my-voice" in result.output and "0.52" in result.output
    req = server.seen[0]
    assert req.url.path == "/consent/reviews" and req.url.params["status"] == "review"
    assert req.headers["x-api-key"] == "op-key" and req.headers["authorization"] == "Bearer op-key"


def test_reviews_json_and_all(server) -> None:
    result = runner.invoke(app, ["consent", "reviews", "--all", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output)[0]["id"] == ID
    assert server.seen[0].url.params["status"] == "all"


@pytest.mark.parametrize("verb, decision", [("approve", "approve"), ("reject", "reject")])
def test_approve_and_reject_post_the_decision_with_a_note(server, verb, decision) -> None:
    result = runner.invoke(app, ["consent", verb, ID, "--note", "checked by ear"])
    assert result.exit_code == 0, result.output
    req = server.seen[-1]
    assert req.method == "POST" and req.url.path == f"/consent/reviews/{ID}"
    assert json.loads(req.content) == {"decision": decision, "note": "checked by ear"}


def test_a_short_id_prefix_is_expanded_from_the_records(server) -> None:
    result = runner.invoke(app, ["consent", "approve", ID[:8]])
    assert result.exit_code == 0, result.output
    assert [r.url.path for r in server.seen] == ["/consent/reviews", f"/consent/reviews/{ID}"]
    assert server.seen[0].url.params["status"] == "all"


def test_an_unknown_prefix_fails_without_posting(server) -> None:
    result = runner.invoke(app, ["consent", "approve", "ffff"])
    assert result.exit_code == 4
    assert all(r.method == "GET" for r in server.seen)


def test_audio_writes_a_wav(server, tmp_path) -> None:
    out = tmp_path / "x.wav"
    result = runner.invoke(app, ["consent", "audio", ID, "--reference", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == b"RIFFfakewav"
    assert server.seen[-1].url.params["which"] == "reference"


def test_without_an_operator_key_the_command_says_how_to_set_one(monkeypatch) -> None:
    monkeypatch.delenv("VOX_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("VOX_OPERATOR_KEY_REF", raising=False)
    result = runner.invoke(app, ["consent", "reviews"])
    assert result.exit_code == 2
    assert "VOX_OPERATOR_KEY" in result.output


def test_a_server_without_an_operator_key_is_explained(server) -> None:
    server.status = 503
    result = runner.invoke(app, ["consent", "reviews"])
    assert result.exit_code == 1
    assert "no operator key configured" in result.output or "switched off" in result.output


def test_a_wrong_operator_key_is_explained(server) -> None:
    server.status = 403
    result = runner.invoke(app, ["consent", "reviews"])
    assert result.exit_code == 5
    assert "operator key" in result.output
