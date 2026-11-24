"""``voxxy license``: show and import against a mocked server."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

import voxxy.commands.license as license_cmd
from voxxy.app import app
from voxxy.client import VoxClient

runner = CliRunner()
GOOD = "VS1.payload.signature"


class Server:
    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        self.state = "missing"
        self.status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        if request.method == "POST":
            key = json.loads(request.content)["key"]
            if key != GOOD:
                return httpx.Response(422, json={"detail": "This license key is not valid: not a Voxxy license key."},
                                      headers={"X-Vox-Error": "license_invalid"})
            self.state = "valid"
        body = {"state": self.state, "detail": "Licensed." if self.state == "valid" else "No license key has been added.",
                "license_id": "lic-1" if self.state == "valid" else None,
                "updates_until": 1_831_536_000 if self.state == "valid" else None, "issued_at": None}
        return httpx.Response(200, json=body)


@pytest.fixture()
def server(monkeypatch):
    srv = Server()
    monkeypatch.setattr(license_cmd, "load_config", lambda: SimpleNamespace(default_url="http://vox.test"))
    real = VoxClient
    monkeypatch.setattr(license_cmd, "VoxClient", lambda url: real(url, transport=httpx.MockTransport(srv)))
    return srv


def test_show_missing_exits_2(server) -> None:
    r = runner.invoke(app, ["license", "show", "--json"])
    assert r.exit_code == 2 and json.loads(r.stdout)["state"] == "missing"


def test_import_a_key_then_show(server, tmp_path) -> None:
    keyfile = tmp_path / "voxxy-license.txt"
    keyfile.write_text(GOOD + "\n")
    r = runner.invoke(app, ["license", "import", str(keyfile)])
    assert r.exit_code == 0, r.output
    assert json.loads(server.seen[-1].content) == {"key": GOOD}
    r = runner.invoke(app, ["license", "show"])
    assert r.exit_code == 0 and "lic-1" in r.stdout and "2028-01-15" in r.stdout


def test_import_from_stdin(server) -> None:
    r = runner.invoke(app, ["license", "import", "-"], input=GOOD + "\n")
    assert r.exit_code == 0, r.output


def test_refused_key_exits_5_with_the_reason(server) -> None:
    r = runner.invoke(app, ["license", "import", "VS1.bad.key"])
    assert r.exit_code == 5
    assert "not a Voxxy license key" in r.output


@pytest.mark.parametrize("status, code", [(404, 4), (401, 3)])
def test_hosted_and_unauthorized(server, status, code) -> None:
    server.status = status
    assert runner.invoke(app, ["license", "show"]).exit_code == code
