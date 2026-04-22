# Spec: Engine Decoupling (vox-tts → microservice topology)

- **Status:** Approved 2026-04-22
- **Owner:** jarad
- **Date:** 2026-04-22
- **Related:** follow-up ADR in `docs/adrs/` once locked

---

## 1. Objective

Separate synthesis engines from `voxxy-core` so that any local TTS engine (`voxcpm`, `vibevoice`, future N) can be brought up, torn down, or swapped independently of the transport/routing layer. Specifically:

- `voxxy-core` owns: HTTP API, MCP mount, voice repo (postgres), voice reference file storage, audio cache, ffmpeg transcoder, ElevenLabs fallback, and the engine orchestrator.
- Each local engine ships as its own container image with its own deps, GPU allocation, and model weights.
- A stack operator can bring up any subset: `voxcpm` only, `vibevoice` only, both, or neither (ElevenLabs-only remote fallback).

**Non-goals:**

- No change to the public HTTP/MCP contract that agents/clients see.
- No streaming support in v1 (VibeVoice-Realtime is a future ADR).
- No multi-speaker dialog API in v1 (single voice per call, same as today).
- No event-bus emission (RabbitMQ/Bloodbank) in v1; orchestrator is the future emit site.
- No auth between core and engines in v1 (they share the compose network; external access is Traefik-gated to `voxxy-core` only).

## 2. Target Users

- **Primary:** Me, running the stack on `big-chungus`. I want to A/B voices across engines without touching code.
- **Secondary:** Agents consuming `speak` / `speak_url` via MCP. They should not notice the refactor beyond a new `engine` value in responses.
- **Tertiary:** Future contributors adding a third or fourth engine (e.g., Kokoro, Parler, Orpheus). The engine contract must be simple enough that adding one is "implement two HTTP handlers."

## 3. Surfaced Assumptions (correct these now)

1. **Hard cutover, not dual-run.** Once engines are containerized, core only speaks JSON-RPC to them. No in-process `VoxCPMEngine` code path survives. Rollback = previous image tag on core + compose file.
2. **Engine endpoint registry is env-driven.** Core reads `VOX_ENGINES` (ordered list of `name=url` pairs, e.g. `voxcpm=http://voxcpm:8000,vibevoice=http://vibevoice:8000`). No service discovery, no config file, no DB row for engines.
3. **Reference audio travels inline (base64).** Engines are stateless, no shared volume. Keeps the contract clean and lets engines live on other hosts later (tailscale). Cost: extra ~100 KB per request for a 3 s reference clip. Acceptable against 2 s inference.
4. **No cross-engine voice reuse in v1.** A voice row targets one or more engines via explicit per-engine columns. If `vibevoice_ref_path` is NULL, VibeVoice cannot serve that voice (either fail over to next engine or 404, see §8).
5. **`voxxy-core`'s existing Docker image no longer needs CUDA.** Core runs on CPU-only base image. ~3 GB image shrink.
6. **Engines trust the core's input.** No auth token in v1 because Traefik only routes `vox.delo.sh` to core; engines are on an internal docker network with no external publish. If engines move to tailscale later, add bearer-token auth.
7. **`speak` (raw WAV) and `speak_url` (OGG URL) both go through the orchestrator.** Today `speak` uses the engine chain too (see `main.py`); no behavior change.
8. **VibeVoice 1.5B is the v1 target** (not Realtime-0.5B). Streaming is future work.
9. **Reference clips in voices table are the canonical source.** VibeVoice prefers 24 kHz but accepts any; processor resamples internally. Core does not resample on upload; engines handle sample-rate normalization.

## 4. Core Features & Acceptance Criteria

### AC1: Engine RPC contract
- `POST /v1/synthesize` on every engine accepts:
  ```json
  {
    "text": "string, required",
    "reference_audio_b64": "string|null, base64 WAV bytes",
    "reference_sample_rate": "int|null, informational",
    "prompt_text": "string|null, only used by engines that support transcript-guided cloning",
    "voice_id": "string|null, engine-specific id (e.g. ElevenLabs voice id, VibeVoice speaker tag)",
    "cfg": "float, default 2.0",
    "steps": "int, default 10"
  }
  ```
  Responds:
  ```json
  {
    "wav_b64": "string, base64 WAV bytes",
    "sample_rate": "int",
    "engine": "string, must match engine name in GET /healthz",
    "duration_s": "float",
    "bytes": "int"
  }
  ```
  Errors return `{"error": {"code": "string", "message": "string"}}` with HTTP 4xx/5xx. Codes: `INVALID_INPUT`, `MODEL_ERROR`, `OOM`, `TIMEOUT`, `UNAVAILABLE`.

### AC2: Engine health contract
- `GET /healthz` returns:
  ```json
  {
    "engine": "voxcpm",
    "ready": true,
    "model_loaded": true,
    "vram_used_gb": 4.8,
    "capabilities": {
      "accepts_reference_audio": true,
      "needs_transcript": false,
      "max_ref_seconds": 30,
      "output_sample_rate": 16000,
      "multi_speaker": false
    }
  }
  ```
- Core caches health for 10 s, skips engines where `ready=false` or the call times out (2 s).

### AC3: Core aggregates engine health
- `GET /healthz` on core returns per-engine status plus `elevenlabs` and `overall` (true if ≥1 engine ready).
- `overall=false` → 503. Otherwise 200.

### AC4: Voice schema additions (additive migration)
- `migrations/002_engine_mapping.sql`:
  - `ALTER TABLE voices ADD COLUMN vibevoice_ref_path TEXT NULL;` (falls back to `wav_path` if NULL and engine accepts it)
  - `ALTER TABLE voices ADD COLUMN vibevoice_speaker_tag TEXT NULL;` (future multi-speaker, unused v1)
  - No change to `wav_path`, `prompt_text`, `prompt_wav_path`, `elevenlabs_voice_id`.
- `init.sql` updated to match for fresh installs.

### AC5: Compose topology
- Three services (engines optional):
  - `voxxy-core`: CPU, ports 8000 via Traefik, depends_on engines with healthcheck gates.
  - `voxxy-engine-voxcpm`: GPU (nvidia runtime), no external port, on `proxy` network for DNS, HF cache bind mount, voices NOT bind-mounted (comes in via RPC).
  - `voxxy-engine-vibevoice`: GPU, same shape as voxcpm, separate HF cache subdir to avoid collision.
- `VOX_ENGINES` env on core drives which engines are expected.
- Existing `compose.yml` becomes `compose.yml` (core + elevenlabs-only default) with optional `compose.engines.yml` overlay for local engines. `mise run up` composes both; `mise run up:core-only` brings up just core.

### AC6: Orchestrator fallback semantics
- Unchanged from today: try engines in `VOX_ENGINES` order, first success wins, log fallbacks at WARNING. ElevenLabs is always last (appended after env-declared engines if `ELEVENLABS_API_KEY` is set).
- New: if the requested voice has no reference mapping for an engine (e.g. `vibevoice_ref_path` NULL), that engine is skipped (not failed) for this call. Logged at INFO with `voice_not_mapped`.

### AC7: MCP and HTTP surface unchanged
- `speak`, `speak_url`, `list_voices_tool`, `/voices`, `/audio/<id>.ogg`, `/synthesize`, `/synthesize-url` all behave identically from the caller's perspective. `engine` field in responses now includes `"vibevoice"` as a possible value.

### AC8: Observability
- Every synthesis logs a structured line: `synth.completed engine=X voice=Y duration_s=Z text_len=N fallback_from=[...]`.
- `X-Vox-Engine` response header on `/synthesize-url` (was there implicitly via JSON body; now also header for callers that don't parse JSON).

## 5. Architecture (high-level)

```
                   ┌─────────────────────────────────────┐
                   │           voxxy-core (CPU)          │
                   │  FastAPI + FastMCP + VoiceRepo      │
                   │  AudioCache + ffmpeg transcoder     │
                   │  EngineOrchestrator                 │
                   └────┬──────────┬──────────┬──────────┘
                        │          │          │
            RPC POST /v1/synthesize│          │ httpx → ElevenLabs API
                        │          │          │
                ┌───────▼──┐  ┌───▼────────┐  └──→ (remote, optional)
                │ voxcpm   │  │ vibevoice  │
                │ engine   │  │ engine     │
                │ (GPU)    │  │ (GPU)      │
                └──────────┘  └────────────┘
```

**State transitions for a single `speak_url` call:**

1. Client → core: `POST /synthesize-url { text, voice, ... }`
2. Core: resolve voice row from postgres, read reference WAV from disk, base64 encode.
3. Core → orchestrator: iterate `VOX_ENGINES`:
   - Check cached `/healthz` → skip if unready.
   - Check voice row has mapping for this engine → skip if not.
   - POST `/v1/synthesize` with payload.
   - On 2xx: break with result.
   - On error or timeout: log, try next.
4. Core: decode WAV, transcode WAV → OGG/Opus, write to cache, return `{audio_url, engine, duration_s, bytes}`.

## 6. Tech Stack

Same as today unless noted:

- **Core:** Python 3.12, FastAPI, FastMCP, asyncpg, httpx, ffmpeg-python (already present). Base image: `python:3.12-slim` (new, was CUDA base).
- **Engines:** Python 3.12, FastAPI, uvicorn. Each engine has its own `pyproject.toml` under `engines/<name>/`.
- **`voxcpm` engine:** keeps current `voxcpm` package + pytorch-cu124 pins. Reuses `app/synth.py` memory containment logic (either vendored or imported from a shared path).
- **`vibevoice` engine:** `transformers>=4.51.3,<5.0`, pytorch-cu124, `microsoft/VibeVoice-1.5B` weights via HF cache bind mount.
- **Shared contract:** a small `vox-engine-contract` pydantic v2 module (under `app/engine_contract.py` or a sibling package) used by both core's client and each engine's server. Single source of truth for request/response shapes.

## 7. Project Structure (post-refactor)

```
voxxy/
├── app/                         # voxxy-core
│   ├── main.py                  # FastAPI + MCP mount (unchanged surface)
│   ├── engines.py               # now has RemoteEngineClient + orchestrator only
│   ├── engine_contract.py       # pydantic models shared with engines
│   ├── voices.py                # VoiceRepo; new columns exposed in Voice dataclass
│   ├── cache.py                 # unchanged
│   ├── audio.py                 # unchanged
│   └── ... (existing)
├── engines/
│   ├── voxcpm/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   ├── uv.lock
│   │   └── engine/
│   │       ├── main.py          # FastAPI: /v1/synthesize, /healthz
│   │       └── synth.py         # moved from app/synth.py
│   └── vibevoice/
│       ├── Dockerfile
│       ├── pyproject.toml
│       ├── uv.lock
│       └── engine/
│           ├── main.py
│           └── synth.py         # new, wraps transformers VibeVoice pipeline
├── compose.yml                  # core only (Traefik-facing)
├── compose.engines.yml          # overlay: voxcpm + vibevoice GPU services
├── migrations/
│   ├── 001_elevenlabs_mapping.sql   # existing
│   └── 002_engine_mapping.sql       # new
├── init.sql                         # updated
├── mise.toml                        # new tasks: up, up:core-only, up:engines, build:voxcpm, build:vibevoice
└── docs/
    └── specs/
        └── engine-decoupling.md     # this file
```

## 8. Boundaries

### Always do
- Preserve the public HTTP/MCP contract. Any agent connecting to `vox.delo.sh/mcp/` today must keep working after this lands.
- Log every engine fallback at WARNING with engine name and exception repr.
- Keep ElevenLabs as terminal fallback if `ELEVENLABS_API_KEY` is set.
- Version the RPC contract with `/v1/` prefix so future breaking changes don't trap us.
- Structured logging on every synth (fields: engine, voice, duration_s, text_len, fallback_from).

### Ask first
- Any change to the `voices` table beyond the documented additive migration.
- Removing `VOX_OPTIMIZE` / `VOX_REF_AUDIO_MAX_SECONDS` env vars (they move to the voxcpm engine container, don't disappear).
- Switching RPC format from JSON/HTTP to anything else (protobuf, gRPC, websocket).
- Introducing auth between core and engines in v1.
- Vendoring/copying `app/synth.py` into the voxcpm engine vs importing it from a shared package.

### Never do
- Do not make core CUDA-dependent after the refactor. Core must run on a CPU-only box.
- Do not share a bind mount between core and engines for reference audio in v1. Audio travels over the wire.
- Do not silently retry the same engine twice; on failure, fall through to the next.
- Do not 200 when all engines fail; return 503 with `{"error": "all engines failed"}`.
- Do not commit real voice WAVs other than `rick.wav` (existing convention).
- Do not bypass the engine contract module; both core and engines import the same pydantic models.

## 9. Testing Strategy

No formal test suite exists in repo today. This spec does not introduce one wholesale, but adds targeted verification:

- **Contract test:** a single `scripts/verify-engine-contract.sh` that `curl`s `POST /v1/synthesize` and `GET /healthz` against each configured engine endpoint and validates response shape with `jq`. Run as part of `mise run smoke`.
- **Smoke test extension:** `mise run smoke` already hits `/synthesize-url`, fetches the OGG, and runs `ffprobe`. Extend to also assert the `engine` field in the response matches the expected primary when both engines are up.
- **Engine unit tests (per engine):** minimal pytest invoking the engine's `Synth` wrapper with a tiny text + tiny reference clip. Skipped on CI without GPU, gated by `VOX_TEST_GPU=1`.
- **Orchestrator fallback test:** pytest against core with two fake engine endpoints (one returning 500, one returning WAV). Asserts fallback engaged and result comes from the second.

## 10. Rollback Plan

- Migration `002_engine_mapping.sql` is additive-only, safe to leave in place on rollback.
- Tag core and engine images with git SHA. Rollback = `docker compose pull voxxy-core:<prev-sha>` and revert `compose.yml`.
- `VOX_ENGINES=elevenlabs` short-circuits local engines if both are broken; core still serves via ElevenLabs.

## 11. Resolved Decisions

1. **`app/synth.py` location:** **Vendor-copy** into `engines/voxcpm/engine/synth.py`. Engines stay dep-independent.
2. **ElevenLabs position:** **Stays in core.** It's already a remote call, no benefit to containerizing.
3. **Voice upload path:** On upload, core **auto-populates `vibevoice_ref_path` from `wav_path`** so new voices work on both engines by default. Admin can override per-voice for engine-specific reference clips.

---

## Sign-off checklist (before moving to `plan`)

- [ ] Assumptions in §3 correct (or corrected)
- [ ] AC list in §4 complete for v1 (nothing else needed before shipping)
- [ ] Project structure in §7 acceptable
- [ ] Boundaries in §8 are the right constraints
- [ ] Open questions in §11 resolved
