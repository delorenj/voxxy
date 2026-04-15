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

`vox-tts` — a universal TTS service wrapping `openbmb/VoxCPM2`, exposing one model via three transports:

- **HTTP** (`POST /synthesize`, `/voices` CRUD) for anything
- **MCP** at `/mcp/` (FastMCP streamable HTTP) for Hermes / OpenClaw agents
- **Node-RED** via the sibling `node-red-contrib-vox/` package (thin HTTP wrapper)

Voice profile metadata lives in a host postgres DB (`vox`); WAV bytes live on disk under `./voices/` (bind-mounted to `/data/voices`).

## Common commands

Build + run (primary workflow — the service is GPU-bound and only sensibly runs in the container):
```bash
docker compose up -d --build
docker logs -f vox
curl http://localhost:8000/healthz   # or https://vox.delo.sh/healthz via Traefik
```

Local Python work (editing, type checks, quick iteration without GPU):
```bash
uv sync                              # resolves deps from uv.lock (pytorch-cu124 index)
uv run uvicorn app.main:app --reload # will fail at model load without CUDA
```

Dependency changes:
```bash
uv add <pkg>                         # updates pyproject.toml + uv.lock
# Dockerfile copies pyproject.toml + uv.lock and runs `uv sync --frozen`,
# so rebuild the image after lock changes: docker compose build --no-cache vox
```

DB schema (applied once against the host postgres):
```bash
psql -h localhost -U "$DEFAULT_USERNAME" -d vox -f init.sql
```

There is no test suite, linter config, or CI in-repo yet. Don't invent commands for those.

## Architecture (the parts that span files)

**Single-process, single-GPU, single model-in-memory.** `app/main.py` owns the FastAPI app and mounts a FastMCP sub-app at `/mcp`. Both transports call the same `_synthesize_bytes()` helper, which in turn uses two module-level singletons populated in the FastAPI `lifespan`:

- `_synth: Synth` (app/synth.py) — wraps `voxcpm.VoxCPM`, loaded once on startup. Holds ~5 GB VRAM permanently. All memory-containment logic (reference-audio trim to `VOX_REF_AUDIO_MAX_SECONDS`, `torch.cuda.empty_cache()` + `ipc_collect()` after every generate, `torch.compile` gated behind `VOX_OPTIMIZE=1`) lives here so it follows the model regardless of caller.
- `_repo: VoiceRepo` (app/voices.py) — asyncpg pool (1–4 conns) over the `voices` table. Schema in `init.sql`. The repo stores only metadata + a relative `wav_path`; the bytes are on disk under `VOICES_DIR`.

**Lifespan nesting matters.** `lifespan()` in `main.py` wraps `mcp_app.lifespan(app)` inside its own `try/finally` — without this, FastMCP's task group never boots and the MCP transport silently 500s. Preserve this structure if you touch startup.

**MCP mount quirk.** `mcp.http_app(path="/")` is mounted at `/mcp`, so the endpoint is `/mcp/` (with trailing slash). Clients registering the URL must include the trailing slash — without it FastAPI 307-redirects and HTTPX drops the POST body. The README documents this; don't "fix" it by removing the mount prefix.

**Voice upload pipeline** (`POST /voices`): audio → tempfile → `sf.info` probe → read first `REF_AUDIO_MAX_SECONDS`×sr frames → mono downmix → PCM_16 WAV at `VOICES_DIR/<name>.wav` → upsert row. The same trim logic runs again on generate via `Synth._maybe_trim_reference` as a safety net for bytes that bypassed the API (scp'd into the bind mount).

**Deployment surface.** `compose.yml` attaches the container to the external `proxy` network and publishes via Traefik labels (`vox.delo.sh`, letsencrypt, port 8000). Host postgres is reached via `host.docker.internal` (mapped to `host-gateway`). The huggingface cache is bind-mounted from the host (`~/.cache/huggingface`) so the 4.58 GB model weights survive image rebuilds. Don't add a named volume for this — rebuilds would re-download.

## Non-obvious conventions

- Python 3.12 only (`requires-python = ">=3.12, <3.13"`). `uv` provisions the interpreter inside the image.
- `torch` / `torchaudio` come from `pytorch-cu124` (explicit uv index). Don't let them drift to PyPI wheels — CPU-only wheels will "work" locally and then OOM or fail in the container.
- The MCP tool that lists voices is named `list_voices_tool` in Python but surfaces as `vox:list_voices` — FastMCP strips the `_tool` suffix convention; keep it when adding new tools.
- `prompt_text` on a voice row switches generation from "voice clone" mode to "Ultimate Cloning" mode (uses `prompt_wav_path` + `prompt_text` together). Only set it when you have an accurate transcript of the reference audio.
- `voices/**/*` is gitignored — only `voices/rick.wav` (the seed) is tracked. Don't commit uploaded voices.

## When things go wrong

- **`libcudnn` missing / model fails to load** → nvidia runtime isn't the default; `docker info | grep -i runtime` must show `nvidia` before `compose up`.
- **OOM during synthesis** → almost always oversized reference audio. Check container logs for `[MEM ...]` / `synthesis peak VRAM` lines. `VOX_REF_AUDIO_MAX_SECONDS` is the knob.
- **MCP tool invisible to Hermes/OpenClaw** → verify trailing slash on the registered URL (`/mcp/` not `/mcp`), then `hermes mcp test vox`.
- **VRAM contention with ollama on the same 3090** → set `OLLAMA_KEEP_ALIVE=0` on the ollama side; the 2B model here is not evictable.
