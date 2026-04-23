# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session bootstrap (do this first)

1. **Hindsight memory** — before any work, recall context for this repo:
   ```bash
   hindsight memory recall voxxy "<what you're about to work on>"
   ```
   The repo dir is `voxxy` but the service/product name is `vox`. Use `voxxy` as the hindsight bank (matches `basename $(git rev-parse --show-toplevel)`). Retain findings to the same bank.

2. **Agent skills** — the `agent-skills` collection is vendored at `.agents/skills/google-agent-skills/`. Discovery flowchart lives in its `using-agent-skills` skill. For non-trivial tasks, route through the appropriate phase skill (`spec`, `plan`, `build`, `test`, `review`, `ship`) rather than ad-hoc edits.

## What this is

`vox-tts` — a universal TTS service that unifies multiple local TTS engines behind one HTTP + MCP surface. Core is a CPU-only transport/routing layer; each engine runs as its own GPU-bound sidecar container. ElevenLabs stays in-core as a terminal remote fallback.

**Three-container topology** (see `docs/specs/engine-decoupling.md` for depth):

- `voxxy-core` (container name `vox`) — FastAPI + FastMCP + voice repo + audio cache + ffmpeg transcoder + engine orchestrator. CPU-only, ~1 GB image, no torch/voxcpm/transformers deps.
- `voxxy-engine-voxcpm` — GPU container wrapping `openbmb/VoxCPM2` behind `POST /v1/synthesize` + `GET /healthz`.
- `voxxy-engine-vibevoice` — GPU container wrapping `microsoft/VibeVoice-1.5B` behind the same contract.

Transports exposed by core:

- **HTTP** (`POST /synthesize` → WAV bytes, `POST /synthesize-url` → OGG/Opus URL, `/voices` CRUD, `/audio/<id>.ogg` cache, `/healthz` with per-engine rollup)
- **MCP** at `/mcp/` (FastMCP streamable HTTP) for Hermes / OpenClaw / Claude Code agents — tools: `speak`, `speak_url`, `list_voices_tool`
- **Node-RED** via the sibling `node-red-contrib-vox/` package (thin HTTP wrapper)

Voice profile metadata lives in a host postgres DB (`vox`); reference WAV bytes live on disk under `./voices/` (bind-mounted to core at `/data/voices`, read by core and shipped inline as base64 to engines — engines are stateless, no shared volume); transcoded OGG/Opus delivery blobs live under `./audio-cache/` (bind-mounted to `/data/audio-cache`, TTL-swept).

## Common commands

Build + run (primary workflow). Prefer the `mise` tasks; they wrap `docker compose -f compose.yml -f compose.engines.yml` with `op run` so ElevenLabs / postgres secrets stay in 1password:

```bash
mise run up                # full stack: core + voxcpm + vibevoice
mise run up:core-only      # just voxxy-core (engines managed externally or disabled)
mise run up:engines        # just the engine sidecars (useful for partial restart)
mise run down              # stop + remove all
mise run restart           # recreate voxxy-core after app/ edits
mise run logs              # docker logs -f vox (core)
mise run logs:voxcpm       # docker logs -f voxxy-engine-voxcpm
mise run health            # /healthz with per-engine roll-up
mise run smoke             # speak_url → fetch → ffprobe + X-Vox-Engine header check
mise run build             # rebuild all images
mise run build:core        # rebuild just voxxy-core (fast, no CUDA)
mise run build:voxcpm      # rebuild just the voxcpm engine image
mise run build:vibevoice   # rebuild just the vibevoice engine image
mise run migrate           # apply SQL migrations against host postgres
mise tasks                 # everything, self-documenting
```

Raw compose still works if 1password is unavailable:
```bash
docker compose -f compose.yml -f compose.engines.yml up -d --build
# requires DEFAULT_USERNAME, DEFAULT_PASSWORD, ELEVENLABS_API_KEY in shell env or .env
docker logs -f vox
curl http://localhost:8000/healthz   # or https://vox.delo.sh/healthz via Traefik
```

Local Python work on **core** (CPU-only, iterates without a GPU):
```bash
uv sync                               # resolves deps from uv.lock
uv run uvicorn app.main:app --reload  # core has no torch dep; runs anywhere
# Point it at a fake engine for loopback testing:
VOX_ENGINES=fake=http://localhost:18001 uv run uvicorn app.main:app --reload
```

Local Python work on an **engine** (needs GPU for full model load, but import/lint works anywhere):
```bash
cd engines/voxcpm      # or engines/vibevoice
uv sync
uv run uvicorn engine.main:app --port 18002 --reload
```

Dependency changes:
```bash
# Core:
uv add <pkg>                         # updates root pyproject.toml + uv.lock
mise run build:core                  # rebuild

# Engine:
cd engines/<name> && uv add <pkg>    # updates that engine's lock only
mise run build:<name>                # rebuild just that engine
```

DB schema (applied once against the host postgres). Fresh install:
```bash
psql -h localhost -U "$DEFAULT_USERNAME" -d vox -f init.sql
```

Incremental migrations against an existing DB:
```bash
mise run migrate      # applies every file under migrations/ in order
```

There is no test suite, linter config, or CI in-repo yet. Don't invent commands for those.

## Architecture (the parts that span files)

**Three-container sidecar topology.** `app/main.py` owns the FastAPI app on core and mounts a FastMCP sub-app at `/mcp`. Core is CPU-only — it never loads a TTS model. Synthesis is RPC'd to one of the engine sidecars over the compose network, decoded, optionally transcoded, and cached. Two module-level singletons are populated in the FastAPI `lifespan`:

- `_repo: VoiceRepo` (app/voices.py) — asyncpg pool (1–4 conns) over the `voices` table. Schema in `init.sql`; additive changes live in `migrations/` numbered by ordinal. Stores metadata + `wav_path` + optional `elevenlabs_voice_id` + `vibevoice_ref_path` + `vibevoice_speaker_tag` (migration 002); reference bytes live on disk under `VOICES_DIR`.
- `_engine: EngineOrchestrator` (app/engines.py) — pluggable `SynthEngine` chain. Populated from `VOX_ENGINES` env as `[RemoteEngineClient(name, url) for name,url in parsed]` with `ElevenLabsEngine` appended when `ELEVENLABS_API_KEY` is set. Tries each in order, first success wins, every response carries `engine: "voxcpm"|"vibevoice"|"elevenlabs"` so callers can detect fallback. Per-engine health is cached for 10 s; unready engines are skipped without logging fallbacks at WARNING-level per attempt.

**Engine RPC contract** (see `app/engine_contract.py` — pydantic v2 models shared verbatim with each engine):

- `POST /v1/synthesize` → `{text, reference_audio_b64, reference_sample_rate, prompt_text, voice_id, cfg, steps}` returns `{wav_b64, sample_rate, engine, duration_s, bytes}`. Errors are `{"error": {"code": "INVALID_INPUT"|"MODEL_ERROR"|"OOM"|"TIMEOUT"|"UNAVAILABLE", "message": "..."}}`.
- `GET /healthz` → `{engine, ready, model_loaded, vram_used_gb, capabilities: {accepts_reference_audio, needs_transcript, max_ref_seconds, output_sample_rate, multi_speaker}}`.
- `/v1/` prefix is versioned so future breaking changes don't trap us.

**Two synthesis paths** share the orchestrator:

- `speak` / `POST /synthesize` — returns raw WAV bytes inline. Routes through the engine chain (not single-engine anymore). Use when an agent needs bytes to process locally (splice, analyze, loop).
- `speak_url` / `POST /synthesize-url` — runs the engine chain, transcodes WAV → OGG/Opus (`app/audio.py` via ffmpeg: libopus, 32 kbps, 48 kHz mono, VoIP preset), writes to the disk cache (`app/cache.py`), returns `{audio_url, engine, duration_s, bytes}` and an `X-Vox-Engine` header. This is the Telegram/Discord/HA/browser-ready path.

**Audio cache lifecycle.** `app/cache.py` writes `<uuid>.ogg` under `VOX_AUDIO_CACHE_DIR`, returns an opaque hex id, and runs a background sweep task every `VOX_AUDIO_SWEEP_INTERVAL` (default 300s) to delete entries older than `VOX_AUDIO_TTL_SECONDS` (default 3600s). The cache is write-once, fetch-once, not a CDN. Any `cache_id` that isn't pure hex returns 404 (path-traversal guard). Served via `GET /audio/{cache_id}.ogg` — Telegram's servers fetch this directly when an agent passes the URL to `sendVoice` with `asVoice: true`.

**Lifespan nesting matters.** `lifespan()` in `main.py` wraps `mcp_app.lifespan(app)` inside its own `try/finally` — without this, FastMCP's task group never boots and the MCP transport silently 500s. Preserve this structure if you touch startup.

**MCP mount quirk.** `mcp.http_app(path="/")` is mounted at `/mcp`, so the endpoint is `/mcp/` (with trailing slash). Clients registering the URL must include the trailing slash — without it FastAPI 307-redirects and HTTPX drops the POST body. The README documents this; don't "fix" it by removing the mount prefix.

**Why `speak_url` exists alongside `speak`.** Telegram, Discord, Home Assistant, and HTML5 `<audio>` all accept a remote URL and fetch it themselves. Passing base64 WAV through the MCP channel for these cases wastes agent tokens and forces the caller to re-upload bytes that Telegram would happily fetch. `speak_url` returns a tiny JSON blob and lets the target's own infrastructure do the transfer. Only use `speak` when the agent needs to process bytes inline (splice, analyze, loop).

**Voice upload pipeline** (`POST /voices`): audio → tempfile → `sf.info` probe → read first `REF_AUDIO_MAX_SECONDS`×sr frames → mono downmix → PCM_16 WAV at `VOICES_DIR/<name>.wav` → upsert row. On upload, `vibevoice_ref_path` is auto-populated from `wav_path` so new voices work on both local engines by default. Admins can override per-voice for engine-specific reference clips. Engine-side trim logic (in each engine's own `synth.py`) runs again on generate as a safety net for bytes that bypassed the API (scp'd into the bind mount, or shipped inline from a voice that wasn't uploaded through core).

**Reference audio travels inline.** Core reads the engine-appropriate WAV from disk, base64-encodes it, and ships it in the `POST /v1/synthesize` body. Engines are stateless — no shared volume between core and engines. Cost: ~100 KB overhead per request for a 3 s clip. Benefit: engines can migrate to tailscale or a different host with zero changes.

**Deployment surface.** `compose.yml` attaches `voxxy-core` (container name `vox`) to the external `proxy` network and publishes it via Traefik labels (`vox.delo.sh`, letsencrypt, port 8000). Host postgres is reached via `host.docker.internal` (mapped to `host-gateway`). `compose.engines.yml` is the overlay that brings up `voxxy-engine-voxcpm` and `voxxy-engine-vibevoice` on the same `proxy` network with GPU reservations but no Traefik labels (internal-only). The huggingface cache is bind-mounted into each engine from `~/.cache/huggingface` so model weights (~4.58 GB voxcpm, ~3 GB vibevoice) survive image rebuilds. Don't convert these to named volumes — rebuilds would re-download.

## Engine containers

Both engines share the same shape and are independently buildable / runnable.

- **Contract:** `app/engine_contract.py` on core is the single source of truth. Each engine vendors a copy at `engines/<name>/engine/contract.py` (identical pydantic models) so engine deps stay decoupled from core's. When you touch the contract, update all three files.
- **Per-engine docs:** `engines/voxcpm/README.md`, `engines/vibevoice/README.md`. Each lists VRAM footprint, env knobs, and model-specific quirks.
- **Adding a third engine** (e.g. Kokoro, Parler, Orpheus):
  1. `engines/<name>/pyproject.toml` with the model's deps (keep `torch` on the `pytorch-cu124` uv index if CUDA).
  2. `engines/<name>/engine/main.py` implementing `POST /v1/synthesize` + `GET /healthz` per the vendored `contract.py`. Memory containment (ref-audio trim, `torch.cuda.empty_cache()` + `gc.collect()` after every generate) is the engine's responsibility, not core's.
  3. `engines/<name>/Dockerfile` with a CUDA base and `uvicorn engine.main:app --host 0.0.0.0 --port 8000`.
  4. Add a service block to `compose.engines.yml` (copy an existing one) and append `<name>=http://voxxy-engine-<name>:8000` to `VOX_ENGINES`. No core code changes.
- **Contract verifier:** `scripts/verify-engine-contract.sh` hits `/healthz` + `/v1/synthesize` on every configured engine and validates shape with `jq`. Runs as part of `mise run smoke`.

## Non-obvious conventions

- Python 3.12 only across core and every engine (`requires-python = ">=3.12, <3.13"`). `uv` provisions the interpreter inside each image.
- **Core has no `torch` dep.** Don't add one. Core runs on `python:3.12-slim` and an accidental CUDA pull re-balloons the image back to ~6 GB.
- Engine `torch` / `torchaudio` come from `pytorch-cu124` (explicit uv index in `engines/<name>/pyproject.toml`). Don't let them drift to PyPI wheels — CPU-only wheels will "work" locally and then fail at model load in the container.
- The MCP tool that lists voices is named `list_voices_tool` in Python but surfaces as `vox:list_voices` — FastMCP strips the `_tool` suffix convention; keep it when adding new tools.
- `prompt_text` on a voice row switches voxcpm generation from "voice clone" mode to "Ultimate Cloning" mode (uses `prompt_wav_path` + `prompt_text` together). Only set it when you have an accurate transcript of the reference audio. VibeVoice ignores `prompt_text` entirely.
- `voices/**/*` is gitignored — only `voices/rick.wav` (the seed) is tracked. Don't commit uploaded voices.
- `audio-cache/` is gitignored in its entirety; contents are ephemeral.
- `voices.elevenlabs_voice_id` (nullable) pins a per-voice ElevenLabs mapping so the fallback stays on-character when possible. NULL falls back to `ELEVENLABS_DEFAULT_VOICE`. Applied via `migrations/001_elevenlabs_mapping.sql` for existing DBs; baked into `init.sql` for fresh installs.
- `voices.vibevoice_ref_path` (nullable) points at a VibeVoice-preferred reference clip. Auto-populated from `wav_path` on upload, so new voices just work on both engines. Override per-voice if you want a shorter / cleaner clip for VibeVoice specifically. Migration 002 (`migrations/002_engine_mapping.sql`). `vibevoice_speaker_tag` is reserved for future multi-speaker dialog and unused in v1.
- ElevenLabs output format is `pcm_24000` by default — we wrap the raw PCM in a WAV container inside `ElevenLabsEngine.generate` so the downstream transcoder stays format-agnostic. Changing `ELEVENLABS_OUTPUT_FORMAT` to any `mp3_*` format skips the wrap and hands bytes straight to ffmpeg.
- VibeVoice output carries an **embedded watermark** (audible + imperceptible) baked into the model weights. Can't be disabled. Call it out if user-facing.
- VibeVoice processor rejects plain text; the engine wrapper auto-promotes to `Speaker 1: <text>` before calling into transformers. Callers don't need to pre-format. For multi-speaker dialog, pass labeled text directly and it's passed through untouched.
- VibeVoice's `VOX_REF_AUDIO_MAX_SECONDS` default is `10` (not 30 like voxcpm) because quality degrades on longer clips. Set in `compose.engines.yml`.
- Engine containers share the host HF cache via bind mount at `/home/delorenj/.cache/huggingface → /cache/huggingface` so voxcpm and vibevoice weights both survive rebuilds without a named volume.

## When things go wrong

- **`libcudnn` missing / engine model fails to load** → nvidia runtime isn't the default; `docker info | grep -i runtime` must show `nvidia` before `compose up`. Core itself has no CUDA deps; if core fails this way, something is misrouted.
- **OOM during synthesis (voxcpm)** → almost always oversized reference audio. Check `docker logs voxxy-engine-voxcpm` for `[MEM ...]` / `synthesis peak VRAM` lines. `VOX_REF_AUDIO_MAX_SECONDS` on the voxcpm container is the knob (default 30).
- **VibeVoice fails to load with `flash_attn` ImportError** → `VOX_VIBEVOICE_ATTN=sdpa` is the default in `compose.engines.yml`. Flash-attn needs a CUDA-toolkit build step that isn't in the image. Rebuild with flash-attn pinned if you want it; otherwise stay on sdpa.
- **VibeVoice "text must start with a speaker label"-style processor errors** → shouldn't happen: the engine wrapper auto-promotes plain text to `Speaker 1: <text>`. If it does, check `docker logs voxxy-engine-vibevoice` for the raw request shape that bypassed the wrapper.
- **VibeVoice quality drops on long references** → engine caps at 10 s via `VOX_REF_AUDIO_MAX_SECONDS`. Raise only if you know what you're doing.
- **Audible high-frequency tone in VibeVoice output** → embedded watermark. Not a bug, not removable.
- **MCP tool invisible to Hermes/OpenClaw** → verify trailing slash on the registered URL (`/mcp/` not `/mcp`), then `hermes mcp test vox`.
- **VRAM contention with ollama on the same 3090** → set `OLLAMA_KEEP_ALIVE=0` on the ollama side; voxcpm holds ~5 GB and vibevoice holds ~7.5 GB, neither evictable. Disable one engine via `VOX_ENGINES` if the GPU is oversubscribed.
- **Telegram `sendVoice` fails with the returned URL** → always pass the `speak_url` result (OGG/Opus), never the `speak` / `/synthesize` result (WAV). Telegram voice notes require OGG.
- **ElevenLabs fallback never engages** → `ELEVENLABS_API_KEY` unset in the core container env. `GET /healthz` reports per-engine availability; use it to confirm before debugging further.
- **All engines show unavailable in `/healthz`** → compose network issue or engine sidecars didn't start. `docker ps | grep voxxy` should show core + every engine in `VOX_ENGINES`. Check `docker logs voxxy-engine-<name>` for startup errors.
- **Core routes to an engine that isn't there** → `VOX_ENGINES` lists it but the sidecar isn't running. Either start it (`mise run up:engines`) or drop it from `VOX_ENGINES` and `mise run restart`.
- **`/audio/<id>.ogg` returns 404 shortly after synthesis** → cache TTL expired (`VOX_AUDIO_TTL_SECONDS`), or the id had non-hex chars (path-traversal guard). Check `docker exec vox ls -la /data/audio-cache/`.
