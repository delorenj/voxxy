# Voxxy API-Key + Generic Wrapper Implementation Plan

> For Hermes: use subagent-driven-development to execute this plan task-by-task.

Goal: Add optional API-key protection to the public Voxxy service and ship a reusable automation-friendly wrapper that complements the rich Python Hermes plugin without replacing it.

Architecture: Guard the FastAPI/FastMCP data-plane with a small request authenticator keyed off `VOX_API_KEY`, leaving only health checks, installer assets, and cached audio fetches public. Extend the existing CLI HTTP client to send `Authorization: Bearer` and `X-API-Key` automatically from `VOX_API_KEY`, then add an installable `voxxy-http-tts` wrapper that turns Voxxy’s HTTP API into a simple file-in/file-out command suitable for Hermes command providers, n8n Execute Command nodes, and Claude-style terminal workflows.

Tech stack: FastAPI, FastMCP, httpx, Typer/CLI packaging, ffmpeg for optional client-side transcode, pytest.

---

### Task 1: Add core API-key auth guard

Objective: Protect synthesis, voice CRUD/listing, and MCP traffic whenever `VOX_API_KEY` is configured.

Files:
- Create: `app/auth.py`
- Modify: `app/main.py`
- Modify: `compose.yml`
- Modify: `.env.example`

Requirements:
- `VOX_API_KEY` is optional; when unset, current behavior remains open.
- When set, accept either:
  - `Authorization: Bearer <key>`
  - `X-API-Key: <key>`
- Keep these routes public:
  - `GET /healthz`
  - `GET /audio/<id>.ogg`
  - `GET /install.sh`
  - `GET /bin/vox-speak`
- Protect everything else under FastAPI/FastMCP, including `/synthesize`, `/synthesize-url`, `/voices*`, and `/mcp*`.
- Use constant-time comparison.
- Return clean 401 responses with no key leakage.
- Do not break compose healthcheck.

Verification:
- focused auth tests pass
- `compose.yml` exposes `VOX_API_KEY` env to core

### Task 2: Teach the Voxxy clients to send the API key

Objective: Make first-party callers work automatically against a secured deployment.

Files:
- Modify: `cli/voxxy/client.py`
- Modify: `cli/voxxy/commands/speak.py`
- Modify: `scripts/vox-speak`
- Modify: `README.md`

Requirements:
- `VoxClient` should automatically read `VOX_API_KEY` from env unless an explicit `api_key` arg overrides it.
- Send both Bearer and `X-API-Key` headers when a key is present.
- Existing CLI paths (`voxxy speak`, `voice list/info/add/delete`, `engine *`, `health`, etc.) should keep working without touching every call site.
- Update docs/examples to show auth headers / `VOX_API_KEY` where relevant.

Verification:
- focused client/header tests pass
- existing plugin still uses `VOX_API_KEY`

### Task 3: Add the reusable companion wrapper

Objective: Ship an automation-friendly wrapper that is simpler than the rich plugin and usable by Hermes command providers, n8n, Claude, and shell scripts.

Files:
- Create: `cli/voxxy/http_tts.py`
- Modify: `cli/pyproject.toml`
- Optionally create: repo shim only if it adds value (avoid duplicate logic)

Wrapper behavior:
- Installable entrypoint name: `voxxy-http-tts`
- Inputs:
  - text from `--text`, `--text-file`, or stdin
  - `--voice`, `--url`, `--api-key`, `--format`, `--cfg`, `--steps`, `--out`
- Defaults from env when appropriate:
  - `VOX_URL`, `VOX_API_KEY`, `VOX_VOICE`
- Output behavior:
  - `wav` → call `/synthesize`, write WAV directly
  - `ogg` / `opus` → call `/synthesize-url`, fetch returned audio, write to file
  - `mp3` / `flac` → call `/synthesize`, transcode locally with ffmpeg, write to file
- Optional `--json` metadata output for automation use-cases.
- Clean non-zero exit on auth failure / HTTP failure / missing ffmpeg for transcode modes.

Verification:
- focused wrapper tests pass
- local smoke command writes a non-empty audio file

### Task 4: Add companion Hermes command-provider template and integration docs

Objective: Show how the wrapper complements the Python plugin.

Files:
- Modify: `plugins/tts/voxxy/README.md`
- Create: `plugins/tts/voxxy/templates/config.command-provider.example.yaml`
- Modify: `README.md`

Must cover:
- rich plugin remains preferred for Voxxy
- companion wrapper is for generic automation / command-provider integration
- sample Hermes config using `tts.providers.voxxy-http`
- sample n8n Execute Command usage
- sample Claude/terminal usage
- `VOX_API_KEY` setup notes

### Task 5: Add focused tests and verify end-to-end behavior

Objective: Prove auth logic and wrapper behavior with real execution, not vibes.

Files:
- Create: `tests/test_api_auth.py` or equivalent focused auth test module
- Create: `cli/tests/test_client_auth.py`
- Create: `cli/tests/test_http_tts.py`

Coverage:
- open behavior when `VOX_API_KEY` unset
- 401 on protected route without auth when key set
- allow Bearer and `X-API-Key`
- public-route exemptions remain public
- client auto-adds headers from env / explicit arg
- wrapper mode selection (`wav`, `ogg`, `mp3/flac`) and error paths

Verification:
- run targeted pytest modules
- run `voxxy-http-tts` smoke against a live Voxxy URL and capture non-zero output file size
- if local auth enforcement cannot be exercised against the deployed host yet, prove it with focused auth tests against the FastAPI app/middleware and call that out explicitly
