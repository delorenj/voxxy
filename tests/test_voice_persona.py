"""voices.persona: repo SQL contract + PUT /voices/{name}/persona + MCP/GET exposure.

The endpoint tests swap ``app.main._repo`` for an in-memory fake and drive the app with a bare
TestClient (no lifespan, so no postgres/engines are needed).
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from app import main
from app.voices import PERSONA_NONE, PERSONA_RE, Voice, VoiceRepo, _row_to_voice


def _voice(name: str = "peter", persona: Optional[str] = None) -> Voice:
    return Voice(name=name, display_name=name.title(), wav_path=f"{name}.wav", duration_s=3.0,
                 prompt_text=None, tags=[], persona=persona)


class FakeRepo:
    def __init__(self, *voices: Voice) -> None:
        self.voices = {v.name: v for v in voices}

    async def get(self, name: str) -> Optional[Voice]:
        return self.voices.get(name)

    async def list(self) -> list[Voice]:
        return sorted(self.voices.values(), key=lambda v: v.name)

    async def set_persona(self, name: str, persona: Optional[str]) -> Optional[Voice]:
        v = self.voices.get(name)
        if v is not None:
            v.persona = persona
        return v


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("VOX_API_KEY", raising=False)
    monkeypatch.setattr(main, "_repo", FakeRepo(_voice("peter"), _voice("rick", persona="george-carlin")))
    return TestClient(main.app)


# ---------------------------------------------------------------- endpoint

def test_set_persona_slug(client: TestClient) -> None:
    r = client.put("/voices/peter/persona", json={"persona": "peter-griffin"})
    assert r.status_code == 200
    assert r.json()["persona"] == "peter-griffin"
    assert client.get("/voices/peter").json()["persona"] == "peter-griffin"


@pytest.mark.parametrize("spelling", ["None", "NONE", " none "])
def test_set_persona_none_is_case_normalized(client: TestClient, spelling: str) -> None:
    r = client.put("/voices/peter/persona", json={"persona": spelling})
    assert r.status_code == 200 and r.json()["persona"] == "none"


def test_set_persona_none_literal_is_stored(client: TestClient) -> None:
    r = client.put("/voices/rick/persona", json={"persona": PERSONA_NONE})
    assert r.status_code == 200 and r.json()["persona"] == "none"


def test_clear_persona_with_null(client: TestClient) -> None:
    assert client.get("/voices/rick").json()["persona"] == "george-carlin"
    r = client.put("/voices/rick/persona", json={"persona": None})
    assert r.status_code == 200 and r.json()["persona"] is None
    # An empty body means "no persona field" which is the same as null.
    assert client.put("/voices/rick/persona", json={}).json()["persona"] is None


def test_unknown_voice_404(client: TestClient) -> None:
    assert client.put("/voices/nobody/persona", json={"persona": "peter-griffin"}).status_code == 404


@pytest.mark.parametrize("bad", ["", "  ", "../etc/passwd", "a/b", "-lead", ".dot", "has space", "é"])
def test_bad_slug_422(client: TestClient, bad: str) -> None:
    r = client.put("/voices/peter/persona", json={"persona": bad})
    assert r.status_code == 422
    assert client.get("/voices/peter").json()["persona"] is None


def test_non_string_persona_422(client: TestClient) -> None:
    assert client.put("/voices/peter/persona", json={"persona": 7}).status_code == 422


def test_persona_in_list_and_get(client: TestClient) -> None:
    by_name = {v["name"]: v for v in client.get("/voices").json()}
    assert by_name["rick"]["persona"] == "george-carlin"
    assert by_name["peter"]["persona"] is None


def test_put_requires_api_key_when_configured(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("VOX_API_KEY", "secret")
    assert client.put("/voices/peter/persona", json={"persona": "x"}).status_code == 401
    ok = client.put("/voices/peter/persona", json={"persona": "x"}, headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_mcp_list_voices_includes_persona(client: TestClient) -> None:
    tool = main.list_voices_tool
    fn = getattr(tool, "fn", tool)
    out = asyncio.run(fn())
    assert {v["name"]: v["persona"] for v in out} == {"peter": None, "rick": "george-carlin"}


# ---------------------------------------------------------------- repo SQL

class RecordingPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.row = {
            "name": "peter", "display_name": "Peter", "wav_path": "peter.wav", "duration_s": 3.0,
            "prompt_text": None, "tags": [], "elevenlabs_voice_id": None, "vibevoice_ref_path": None,
            "vibevoice_speaker_tag": None, "persona": "peter-griffin",
        }

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        return self.row

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return [self.row]


def test_upsert_preserves_persona_on_conflict() -> None:
    pool = RecordingPool()
    repo = VoiceRepo(pool)  # type: ignore[arg-type]
    v = asyncio.run(repo.upsert(name="peter", display_name="Peter", wav_path="peter.wav", duration_s=3.0))
    sql = pool.calls[0][0]
    update_set = sql.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]
    assert "persona" not in update_set
    assert "persona" in sql.split("RETURNING", 1)[1]
    assert v.persona == "peter-griffin"


def test_get_list_select_persona_and_set_persona_sql() -> None:
    pool = RecordingPool()
    repo = VoiceRepo(pool)  # type: ignore[arg-type]
    assert asyncio.run(repo.get("peter")).persona == "peter-griffin"
    assert asyncio.run(repo.list())[0].persona == "peter-griffin"
    assert asyncio.run(repo.set_persona("peter", None)).persona == "peter-griffin"
    for sql, _ in pool.calls[:2]:
        assert "persona" in sql
    sql, args = pool.calls[2]
    assert sql.startswith("UPDATE voices SET persona = $2") and args == ("peter", None)
    pool.row = None
    assert asyncio.run(repo.set_persona("ghost", "x")) is None


def test_row_without_persona_defaults_none() -> None:
    row = RecordingPool().row
    row["persona"] = None
    assert _row_to_voice(row).persona is None


def test_persona_regex_matches_plugin_name_re() -> None:
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "plugins" / "voice-persona" / "__init__.py"
    spec = importlib.util.spec_from_file_location("vp_regex_check", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vp_regex_check"] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    assert mod._NAME_RE.pattern == PERSONA_RE.pattern
