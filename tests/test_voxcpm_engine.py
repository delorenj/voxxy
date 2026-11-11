"""voxcpm engine (engines/voxcpm/engine/main.py): synthesis runs off the event loop behind one GPU lock.

The engine image has torch and voxcpm; this suite does not. ``torch`` and ``engine.synth`` are swapped for
stand-ins before ``engine.main`` is imported, so the HTTP layer and its concurrency are tested on any box.
``SlowSynth.generate`` blocks its thread with ``time.sleep``, which is what a CUDA-bound generate does to
whichever thread calls it: if it ran on the event loop, /healthz would wait behind it.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import os
import sys
import threading
import time
import types
from pathlib import Path

import httpx
import numpy as np
import pytest

pytestmark = pytest.mark.anyio

ENGINE_ROOT = Path(__file__).resolve().parents[1] / "engines" / "voxcpm"


class FakeOOM(RuntimeError):
    pass


class SlowSynth:
    """Stand-in for engine.synth.Synth: records calls, blocks its thread, tracks overlap."""

    def __init__(self, delay: float = 0.6, fail: Exception | None = None) -> None:
        self._model = object()
        self.delay = delay
        self.fail = fail
        self.calls: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.seen_files: list[tuple[bool, bool]] = []
        self._mu = threading.Lock()

    def generate(self, **kwargs):
        with self._mu:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(kwargs)
        try:
            ref, prompt = kwargs.get("reference_wav_path"), kwargs.get("prompt_wav_path")
            self.seen_files.append((bool(ref and os.path.exists(ref)), bool(prompt and os.path.exists(prompt))))
            time.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
            return np.zeros(4800, dtype=np.float32), 48_000
        finally:
            with self._mu:
                self.active -= 1


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch):
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(OutOfMemoryError=FakeOOM, is_available=lambda: False)
    fake_synth = types.ModuleType("engine.synth")
    fake_synth.REF_AUDIO_MAX_SECONDS = 30.0
    fake_synth.Synth = SlowSynth

    monkeypatch.syspath_prepend(str(ENGINE_ROOT))
    for name in ("engine", "engine.main", "engine.contract", "engine.synth"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    importlib.import_module("engine")  # the real (empty) package from engines/voxcpm
    monkeypatch.setitem(sys.modules, "engine.synth", fake_synth)
    mod = importlib.import_module("engine.main")
    yield mod
    for name in ("engine", "engine.main", "engine.contract"):
        sys.modules.pop(name, None)


def _client(mod) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mod.app), base_url="http://engine")


async def _until(cond, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def test_healthz_answers_while_a_long_synthesis_runs(engine) -> None:
    synth = SlowSynth(delay=1.0)
    engine._synth = synth
    async with _client(engine) as client:
        job = asyncio.create_task(client.post("/v1/synthesize", json={"text": "a long utterance"}))
        await _until(lambda: synth.active == 1)
        started = time.monotonic()
        health = await client.get("/healthz")
        waited = time.monotonic() - started
        assert synth.active == 1, "health must have answered mid-synthesis"
        result = await job
    assert health.status_code == 200 and health.json()["ready"] is True
    assert waited < 0.5  # with generate on the loop this waited out the whole second
    assert result.status_code == 200
    assert result.json()["engine"] == "voxcpm"


async def test_gpu_lock_runs_one_generate_at_a_time(engine) -> None:
    synth = SlowSynth(delay=0.15)
    engine._synth = synth
    async with _client(engine) as client:
        replies = await asyncio.gather(*(client.post("/v1/synthesize", json={"text": f"line {i}"}) for i in range(4)))
    assert [r.status_code for r in replies] == [200] * 4
    assert synth.max_active == 1


async def test_seed_and_settings_reach_the_model(engine) -> None:
    synth = SlowSynth(delay=0.0)
    engine._synth = synth
    async with _client(engine) as client:
        r = await client.post("/v1/synthesize", json={"text": "hi", "seed": 1234, "cfg": 2.5, "steps": 6})
    assert r.status_code == 200
    call = synth.calls[0]
    assert call["seed"] == 1234 and call["cfg_value"] == 2.5 and call["inference_timesteps"] == 6


async def test_reference_and_prompt_temp_files_exist_only_during_the_call(engine, make_wav) -> None:
    synth = SlowSynth(delay=0.0)
    engine._synth = synth
    body = {"text": "hola", "reference_audio_b64": base64.b64encode(make_wav()).decode(), "prompt_text": "hello"}
    async with _client(engine) as client:
        r = await client.post("/v1/synthesize", json=body)
    assert r.status_code == 200
    call = synth.calls[0]
    assert synth.seen_files == [(True, True)]
    assert call["prompt_text"] == "hello"
    assert not os.path.exists(call["reference_wav_path"])
    assert not os.path.exists(call["prompt_wav_path"])


async def test_unreadable_reference_is_400_not_500(engine) -> None:
    synth = SlowSynth(delay=0.0)
    engine._synth = synth
    body = {"text": "hi", "reference_audio_b64": base64.b64encode(b"not audio" * 20).decode()}
    async with _client(engine) as client:
        r = await client.post("/v1/synthesize", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_INPUT"
    assert synth.calls == []


@pytest.mark.parametrize(
    ("fail", "status", "code"),
    [(ValueError("text must be a non-empty string"), 400, "INVALID_INPUT"),
     (FakeOOM("CUDA out of memory"), 500, "OOM"),
     (RuntimeError("kernel died"), 500, "MODEL_ERROR")],
)
async def test_errors_keep_the_envelope(engine, fail, status: int, code: str) -> None:
    engine._synth = SlowSynth(delay=0.0, fail=fail)
    async with _client(engine) as client:
        r = await client.post("/v1/synthesize", json={"text": "hi"})
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


async def test_unloaded_model_is_503_unavailable(engine) -> None:
    engine._synth = None
    async with _client(engine) as client:
        r = await client.post("/v1/synthesize", json={"text": "hi"})
        health = await client.get("/healthz")
    assert r.status_code == 503 and r.json()["error"]["code"] == "UNAVAILABLE"
    assert health.json()["ready"] is False
