# Plan: Engine Decoupling

- **Spec:** [engine-decoupling.md](./engine-decoupling.md)
- **Status:** Ready for build
- **Date:** 2026-04-22

---

## Dependency Graph

```
Phase 0 (DB)  ─┐
               ├─→ Phase 1 (contract + client) ─→ Phase 2 (voxcpm extract) ─┐
               │                                                              ├─→ Phase 4 (polish)
               └───────────────────────────────→ Phase 3 (vibevoice engine) ─┘
```

**Slicing principle:** each phase lands the stack in a shippable state. No half-finished dep shape is ever on `main`.

| Phase | End state | Rollback |
|-------|-----------|----------|
| 0 | DB has new columns, code unaware | drop columns |
| 1 | `RemoteEngineClient` works against a loopback fake, in-process engines still active | revert commit |
| 2 | `voxcpm` runs as its own container, core is CPU-only | previous core image + compose.yml |
| 3 | `vibevoice` container joinable via env | stop vibevoice service |
| 4 | mise tasks, logs, README match new topology | non-functional, pure docs/ops |

---

## Phase 0 — Schema prep (S)

### T0.1 — Migration 002: per-engine voice mapping
- **Size:** S
- **Deps:** none
- **Files:** `migrations/002_engine_mapping.sql` (new), `init.sql` (edit)
- **Acceptance:**
  - `vibevoice_ref_path TEXT NULL` added to `voices`
  - `vibevoice_speaker_tag TEXT NULL` added to `voices`
  - `init.sql` matches for greenfield installs
- **Verify:**
  ```bash
  mise run migrate
  psql -h localhost -U "$DEFAULT_USERNAME" -d vox -c "\d voices" | grep vibevoice
  # expect: vibevoice_ref_path, vibevoice_speaker_tag
  ```
- **Rollback:** `ALTER TABLE voices DROP COLUMN vibevoice_ref_path, DROP COLUMN vibevoice_speaker_tag;`

**Phase 0 checkpoint:** smoke test still passes (no code path touches new columns yet).

---

## Phase 1 — Engine contract + remote client (M)

Core still runs in-process. Add the new seam without removing the old. Prove routing works before physically splitting containers.

### T1.1 — Pydantic engine contract module
- **Size:** S
- **Deps:** T0.1
- **Files:** `app/engine_contract.py` (new)
- **Acceptance:**
  - `EngineSynthesizeRequest`, `EngineSynthesizeResponse`, `EngineError`, `EngineHealth`, `EngineCapabilities` pydantic v2 models
  - Matches §4 AC1 + AC2 in spec exactly
  - Zero imports of `voxcpm` / `torch` / `transformers` (pure contract)
- **Verify:**
  ```bash
  uv run python -c "from app.engine_contract import EngineSynthesizeRequest, EngineSynthesizeResponse, EngineHealth; print('ok')"
  ```

### T1.2 — `RemoteEngineClient` implementing `SynthEngine` Protocol
- **Size:** M
- **Deps:** T1.1
- **Files:** `app/engines.py` (edit: add `RemoteEngineClient`, keep `VoxCPMEngine` and `ElevenLabsEngine` intact)
- **Acceptance:**
  - Constructor: `RemoteEngineClient(name: str, base_url: str, timeout: float = 30.0)`
  - `available()` returns cached `/healthz` result (10 s TTL), logs warnings on failures
  - `generate()` posts to `<base_url>/v1/synthesize`, deserializes response, decodes `wav_b64` to bytes, returns `SynthResult`
  - Translates `EngineError` payloads into `RuntimeError` with code prefix so orchestrator logs capture it
  - Error classes: timeout → fallback; 5xx → fallback; 4xx → re-raise as permanent (don't fall through on bad input)
- **Verify:** covered by T1.3.

### T1.3 — Loopback contract test
- **Size:** S
- **Deps:** T1.2
- **Files:** `scripts/verify-engine-contract.sh` (new), `tests/test_remote_engine.py` (new, optional pytest)
- **Acceptance:**
  - Shell script spins up a minimal FastAPI fake engine on `:18001` that returns a hardcoded WAV, then runs `curl` against contract endpoints and validates shape with `jq`
  - Pytest (if adopted): starts an in-process fake, instantiates `RemoteEngineClient`, asserts orchestrator returns fake bytes with `engine="fake"`
- **Verify:**
  ```bash
  bash scripts/verify-engine-contract.sh
  # expect: "contract ok"
  ```

**Phase 1 checkpoint:** orchestrator routes through `RemoteEngineClient` successfully in-repo. `VoxCPMEngine` still wired in `main.py`. Nothing user-visible changed.

---

## Phase 2 — Extract voxcpm into its own container (L)

Physical split. After this phase, core no longer imports `voxcpm` or `torch`.

### T2.1 — Scaffold `engines/voxcpm/`
- **Size:** S
- **Deps:** T1.3
- **Files:** `engines/voxcpm/pyproject.toml`, `engines/voxcpm/uv.lock` (generated), `engines/voxcpm/.python-version`
- **Acceptance:**
  - Isolated pyproject with: `fastapi`, `uvicorn[standard]`, `voxcpm`, `torch` from `pytorch-cu124` index, `soundfile`, `numpy`, `pydantic>=2`
  - `tool.uv.sources` pins pytorch index (copy from root `pyproject.toml`)
  - Python 3.12
- **Verify:**
  ```bash
  cd engines/voxcpm && uv sync --frozen
  # expect: lock resolves, venv created
  ```

### T2.2 — Vendor `synth.py` into the engine
- **Size:** S
- **Deps:** T2.1
- **Files:** `engines/voxcpm/engine/__init__.py`, `engines/voxcpm/engine/synth.py`
- **Acceptance:**
  - Copy of `app/synth.py` verbatim (memory containment, trim logic, VOX_OPTIMIZE gate all preserved)
  - No imports from `app.*` (decoupled)
- **Verify:** diff shows only import-path edits, no behavior changes.

### T2.3 — Engine HTTP server
- **Size:** M
- **Deps:** T2.2, T1.1
- **Files:** `engines/voxcpm/engine/main.py`, `engines/voxcpm/engine/contract.py` (vendor-copy of `app/engine_contract.py`)
- **Acceptance:**
  - `POST /v1/synthesize` implements contract: decodes `reference_audio_b64` to tempfile, calls `Synth.generate(...)`, returns base64-encoded WAV + sample rate
  - `GET /healthz` returns `EngineHealth` with `capabilities.needs_transcript=false`, `accepts_reference_audio=true`, `max_ref_seconds=VOX_REF_AUDIO_MAX_SECONDS`, `output_sample_rate=16000`
  - Model loads in FastAPI `lifespan`; first request after boot blocks until ready
  - Temp-file cleanup on every request (finally block)
- **Verify:**
  ```bash
  cd engines/voxcpm && uv run uvicorn engine.main:app --port 18002 &
  # once healthz is 200:
  curl -fsS http://localhost:18002/healthz | jq .
  # spot-check: ready=true, capabilities.output_sample_rate=16000
  ```

### T2.4 — Engine Dockerfile
- **Size:** S
- **Deps:** T2.3
- **Files:** `engines/voxcpm/Dockerfile`
- **Acceptance:**
  - Base: `nvidia/cuda:12.4.1-runtime-ubuntu22.04` (same as current root)
  - Runs `uvicorn engine.main:app --host 0.0.0.0 --port 8000`
  - HF cache bind mount target: `/cache/huggingface` (same path as today)
  - Healthcheck: `curl /healthz`
- **Verify:**
  ```bash
  docker build -t voxxy-engine-voxcpm engines/voxcpm
  # expect: clean build
  ```

### T2.5 — Slim core Dockerfile to CPU-only
- **Size:** S
- **Deps:** T2.4
- **Files:** `Dockerfile` (edit), `pyproject.toml` (edit)
- **Acceptance:**
  - Base flips to `python:3.12-slim`
  - Drops `voxcpm`, `torch`, `torchaudio` from `[project.dependencies]`
  - Drops `gcc g++` from apt (no native builds needed for core anymore)
  - Keeps `ffmpeg`, `libsndfile1`, `curl`
  - `uv sync --frozen` resolves without CUDA wheels
- **Verify:**
  ```bash
  docker build -t voxxy-core .
  docker run --rm voxxy-core python -c "import app.main; print('ok')"
  # expect: "ok"; no CUDA in the image
  docker image inspect voxxy-core --format '{{.Size}}'
  # expect: ~500 MB vs current ~6 GB
  ```

### T2.6 — Rewire core lifespan to remote engine
- **Size:** M
- **Deps:** T2.5
- **Files:** `app/main.py` (edit), `app/engines.py` (edit: delete `VoxCPMEngine`, keep `ElevenLabsEngine`, `RemoteEngineClient`, `EngineOrchestrator`), `app/synth.py` (delete), `app/engines.py` moves `REF_AUDIO_MAX_SECONDS` reference out
- **Acceptance:**
  - `lifespan` parses `VOX_ENGINES` env (format: `name=url,name=url`)
  - Builds engines list: `[RemoteEngineClient(n, u) for n,u in parsed] + [ElevenLabsEngine()]` (ElevenLabs appended if API key set, else omitted)
  - `/synthesize` (legacy raw WAV) still works: calls orchestrator, returns `result.wav_bytes` directly
  - `/synthesize-url` unchanged behaviorally
  - `_synth` global removed; `_maybe_trim_reference` on uploads uses inline trimming via `sf.read` (already done in `create_voice`)
  - `_resolve_voice` reads WAV bytes from disk, base64 encodes, passes through the contract (shape: add `reference_audio_b64` alongside or instead of `reference_wav_path` at the engine seam)
- **Verify:**
  ```bash
  # compose up both services
  mise run up
  mise run health   # core healthz lists remote voxcpm as ready
  mise run smoke    # end-to-end synthesize returns engine="voxcpm"
  ```

### T2.7 — Compose topology for Phase 2
- **Size:** S
- **Deps:** T2.6
- **Files:** `compose.yml` (edit), `compose.engines.yml` (new)
- **Acceptance:**
  - `compose.yml`: `voxxy-core` service loses GPU reservation, loses voices/cache volumes stay, adds `VOX_ENGINES=voxcpm=http://voxxy-engine-voxcpm:8000`
  - `compose.engines.yml`: `voxxy-engine-voxcpm` service on `proxy` network, GPU reservation, HF cache bind, healthcheck, no Traefik labels (internal only)
  - Both services rename `vox` → `voxxy-core` for clarity; keep `container_name: vox` on core as alias for existing monitoring
  - `mise run up` uses `docker compose -f compose.yml -f compose.engines.yml up -d --build`
- **Verify:**
  ```bash
  mise run up
  docker ps | grep voxxy
  # expect: voxxy-core + voxxy-engine-voxcpm both running
  mise run smoke   # green
  ```

**Phase 2 checkpoint: LOAD-BEARING.** Full cutover. `git log` at this point is the rollback target. Tag it `v0.3.0-engine-split`.

---

## Phase 3 — VibeVoice engine (L)

Net-new engine. Parallel structure to `engines/voxcpm/` but wrapping `transformers`.

### T3.1 — Scaffold `engines/vibevoice/`
- **Size:** S
- **Deps:** T2.7
- **Files:** `engines/vibevoice/pyproject.toml`, `engines/vibevoice/uv.lock`
- **Acceptance:**
  - Deps: `fastapi`, `uvicorn[standard]`, `transformers>=4.51.3,<5.0`, `torch` from `pytorch-cu124`, `soundfile`, `numpy`, `librosa` (for resampling to 24 kHz), `accelerate`, `pydantic>=2`
- **Verify:** `cd engines/vibevoice && uv sync --frozen` resolves.

### T3.2 — VibeVoice `Synth` wrapper
- **Size:** M
- **Deps:** T3.1
- **Files:** `engines/vibevoice/engine/synth.py`, `engines/vibevoice/engine/contract.py` (vendor-copy)
- **Acceptance:**
  - Loads `microsoft/VibeVoice-1.5B` via `AutoModelForCausalLM.from_pretrained(..., torch_dtype=torch.bfloat16, device_map="auto", trust_remote_code=True)`
  - Loads `AutoProcessor` for same model
  - `generate(text, reference_wav_path, ...)`:
    - Reads reference via `librosa.load(path, sr=24000, mono=True)`
    - Calls processor with `text=...` and `reference_audio=...` (exact kwarg TBD during impl — see spec §11 open items; fall back to grepping `processor_config.json`)
    - Calls `.generate(**inputs, cfg_scale, inference_steps)` with bfloat16 autocast
    - Decodes tokens via acoustic decoder (single additional model loaded in same lifespan)
    - Returns `(wav_np, 24000)` matching voxcpm shape
  - Same memory-containment hygiene as voxcpm: `gc.collect()` + `torch.cuda.empty_cache()` after every call
- **Verify:**
  ```bash
  # inside a GPU host, once HF cache has weights:
  cd engines/vibevoice && uv run python -c "
  from engine.synth import VibeVoiceSynth
  s = VibeVoiceSynth(); s.load()
  wav, sr = s.generate(text='Hello world.', reference_wav_path='../../../voices/rick.wav')
  print(len(wav), sr)"
  # expect: non-zero samples at 24000
  ```

### T3.3 — VibeVoice engine HTTP server + Dockerfile
- **Size:** S
- **Deps:** T3.2
- **Files:** `engines/vibevoice/engine/main.py`, `engines/vibevoice/Dockerfile`
- **Acceptance:**
  - Same contract as voxcpm (T2.3), `engine: "vibevoice"`, `output_sample_rate: 24000`, `needs_transcript: false`
  - Dockerfile mirrors voxcpm but adds `libsndfile1-dev` if librosa needs it
  - Separate HF cache subdir envvar option (`HF_HOME=/cache/huggingface/vibevoice`) to avoid any fetch races with voxcpm on first boot
- **Verify:**
  ```bash
  docker build -t voxxy-engine-vibevoice engines/vibevoice
  ```

### T3.4 — Add vibevoice to compose + enable in core
- **Size:** S
- **Deps:** T3.3
- **Files:** `compose.engines.yml` (edit), `compose.yml` (edit core's `VOX_ENGINES`)
- **Acceptance:**
  - `voxxy-engine-vibevoice` service with GPU reservation, healthcheck
  - Core env updated: `VOX_ENGINES=voxcpm=http://voxxy-engine-voxcpm:8000,vibevoice=http://voxxy-engine-vibevoice:8000` (voxcpm primary)
- **Verify:**
  ```bash
  mise run up
  mise run health
  # expect both engines ready
  ```

### T3.5 — Auto-populate `vibevoice_ref_path` on voice upload
- **Size:** S
- **Deps:** T3.4
- **Files:** `app/main.py` (edit `create_voice`), `app/voices.py` (edit `upsert` + `Voice` dataclass)
- **Acceptance:**
  - `Voice` dataclass gains `vibevoice_ref_path: Optional[str]` and `vibevoice_speaker_tag: Optional[str]`
  - `create_voice` sets `vibevoice_ref_path = wav_path` by default (i.e. same clip for both engines)
  - `_resolve_voice` accepts an optional `engine_name` arg, returns the engine-appropriate ref path (falls back to `wav_path` if engine-specific is NULL)
  - Orchestrator passes `engine_name` down into voice resolution so each attempt gets the right reference
- **Verify:**
  ```bash
  # re-upload rick through the API, confirm both ref paths populate
  curl -fsS -X POST -F 'name=rick' -F 'display_name=Rick' -F audio=@voices/rick.wav \
    https://vox.delo.sh/voices | jq .
  psql -h localhost -U "$DEFAULT_USERNAME" -d vox \
    -c "SELECT name, wav_path, vibevoice_ref_path FROM voices;"
  # expect vibevoice_ref_path = rick.wav

  # synthesis with vibevoice as primary:
  VOX_ENGINES=vibevoice=http://voxxy-engine-vibevoice:8000,voxcpm=http://voxxy-engine-voxcpm:8000 \
    mise run restart
  mise run smoke
  # expect: engine="vibevoice" in response
  ```

**Phase 3 checkpoint:** full three-engine topology live.

---

## Phase 4 — Polish (M total)

All tasks in this phase are independent and parallelizable.

### T4.1 — mise tasks for new topology
- **Size:** S
- **Files:** `mise.toml` (edit)
- **Acceptance:**
  - `up` → both core + engines
  - `up:core-only` → just core (expects external engines)
  - `up:engines` → just engines (useful for partial restart)
  - `build:voxcpm`, `build:vibevoice` → targeted rebuilds
  - `logs:<name>` → per-service log tail
- **Verify:** `mise tasks` lists them; `mise run up:core-only` works.

### T4.2 — Structured logging + `X-Vox-Engine` header
- **Size:** S
- **Files:** `app/main.py` (edit), `app/engines.py` (edit)
- **Acceptance:**
  - Every synth emits: `synth.completed engine=X voice=Y duration_s=Z text_len=N fallback_from=[...]`
  - `/synthesize-url` response carries `X-Vox-Engine: <name>` header
  - Engine containers log `synth.served text_len=N ref_seconds=M gen_seconds=S vram_peak_gb=V`
- **Verify:**
  ```bash
  mise run smoke 2>&1 | grep -q "X-Vox-Engine: voxcpm"   # add -i to curl in smoke task
  docker logs voxxy-engine-voxcpm | grep synth.served
  ```

### T4.3 — README + CLAUDE.md updates
- **Size:** S
- **Files:** `README.md`, `CLAUDE.md`
- **Acceptance:**
  - New architecture section + mermaid diagram of core + engines
  - Updated "Common commands" listing `up:core-only`, `build:<engine>`, etc.
  - Note on watermarks for VibeVoice output
  - Updated "When things go wrong" with per-engine failure modes
- **Verify:** `grep -c voxxy-engine README.md` > 0; `mermaid` block is valid per the GitHub renderer preview.

### T4.4 — End-to-end contract verifier
- **Size:** S
- **Files:** `scripts/verify-engine-contract.sh` (from T1.3, now exhaustive)
- **Acceptance:**
  - Iterates every entry in `VOX_ENGINES` + ElevenLabs
  - For each: curls `/healthz`, validates pydantic-shaped JSON, curls `/v1/synthesize` with a 1-second silence WAV, validates response
  - Part of `mise run smoke`
- **Verify:** `mise run smoke` runs it and stays green.

### T4.5 — Kill the legacy `VoxCPMEngine` leftovers
- **Size:** S
- **Files:** `app/engines.py` (final cleanup)
- **Acceptance:**
  - `VoxCPMEngine` class removed entirely (was left in for Phase 1 safety)
  - No imports of `app.synth` remain in core
  - `pyproject.toml` root deps have no torch/voxcpm
- **Verify:**
  ```bash
  grep -r 'VoxCPMEngine\|from app.synth\|import voxcpm' app/ && echo FAIL || echo OK
  ```

---

## Task Summary Table

| ID | Task | Size | Deps | Phase checkpoint |
|----|------|------|------|-------------------|
| T0.1 | Migration 002 + init.sql | S | — | P0 ✔ |
| T1.1 | Engine contract module | S | T0.1 | — |
| T1.2 | RemoteEngineClient | M | T1.1 | — |
| T1.3 | Loopback contract test | S | T1.2 | P1 ✔ |
| T2.1 | Scaffold engines/voxcpm | S | T1.3 | — |
| T2.2 | Vendor synth.py | S | T2.1 | — |
| T2.3 | voxcpm HTTP server | M | T2.2, T1.1 | — |
| T2.4 | voxcpm Dockerfile | S | T2.3 | — |
| T2.5 | Slim core Dockerfile | S | T2.4 | — |
| T2.6 | Rewire core lifespan | M | T2.5 | — |
| T2.7 | Compose topology | S | T2.6 | P2 ✔ (tag `v0.3.0-engine-split`) |
| T3.1 | Scaffold engines/vibevoice | S | T2.7 | — |
| T3.2 | VibeVoice Synth wrapper | M | T3.1 | — |
| T3.3 | vibevoice HTTP server + Dockerfile | S | T3.2 | — |
| T3.4 | vibevoice in compose | S | T3.3 | — |
| T3.5 | Auto-populate ref path | S | T3.4 | P3 ✔ |
| T4.1 | mise tasks refactor | S | P3 | — |
| T4.2 | Structured logging + header | S | P3 | — |
| T4.3 | README + CLAUDE.md | S | P3 | — |
| T4.4 | Contract verifier | S | P3 | — |
| T4.5 | Remove legacy VoxCPMEngine | S | T4.4 | P4 ✔ |

**Total:** 20 tasks. Distribution: 15×S, 5×M, 0×L/XL. Nothing here is L individually; the phase composition is what makes this feel like an L-shaped effort.

---

## Parallelization opportunities

- **Phase 3 independent of Phase 2 once T1.3 is done** — could be built in parallel if you want two worktrees. But the smoke path benefits from voxcpm being containerized first, so serial is cleaner.
- **Phase 4 is fully parallel** — T4.1–T4.5 touch disjoint files.

## Risks and open items (carry-forward from spec §11)

- VibeVoice processor kwarg name for reference audio is TBD until T3.2 implementation. Not blocking; it's a 15-min source dive inside that task.
- If VibeVoice requires flash-attn or other native kernels (spec research says no, but unverified on actual container build), T3.1 balloons to M. Mitigation: allocate a spike-check inside T3.1 that runs a bare `transformers` load before finalizing the image.
- If the `transformers` pin forces an incompatible `torch` vs our `pytorch-cu124` index, T3.1 needs a pin negotiation. Mitigation: start T3.1 with a dry uv resolve before any code is written.

## Sign-off

- [ ] Plan reviewed, dependency order accepted
- [ ] Risks in §11 acknowledged
- [ ] Ready to start T0.1
