# Spec: `voxxy` unified CLI

- **Status:** Shipped 2026-04-24
- **Owner:** jarad
- **Date:** 2026-04-24
- **Related:** [[engine-decoupling|docs/specs/engine-decoupling.md]] · [[smoketest-engine-swap|docs/runbooks/smoketest-engine-swap.md]]

---

## 1. Objective

Collapse the operational surface of vox-tts into a single command-tree CLI so day-to-day tasks (stack lifecycle, engine swap, voice management, one-shot speak) are one verb instead of a composition of `docker compose` + `op run` + `curl` + `ffmpeg` + `psql` invocations.

**In scope (v1):**

- Stack lifecycle: `voxxy daemon start|stop|restart|reset|status|install`
- Engine control: `voxxy engine list|use|enable|disable|logs`
- Voice CRUD + cloning: `voxxy voice list|info|add|delete`
- Synthesis: `voxxy speak [TEXT]`
- Health + version + logs: `voxxy health|logs|version`

**Out of scope (v1, deferred):**

- MCP client registration helper (`voxxy mcp register`)
- Batch voice import from directory
- Audio-prep-only workflows (`voxxy audio clean/ref`) — subsumed into `voice add` for now
- Web UI / TUI dashboard
- Multi-profile support (multiple vox-tts deployments from one CLI)
- Telemetry / metrics scraping subcommands

## 2. Target Users

- **Primary:** Me running vox-tts on `big-chungus` locally and via ssh.
- **Secondary:** Agents consuming the CLI via shell (`ssh host voxxy speak --raw '...' | paplay`). Non-interactive scriptable mode is first-class.
- **Tertiary:** Future workstation bootstrap: `voxxy daemon install` on a fresh machine should get me from clone to "stack running" in one command.

## 3. Surfaced Assumptions

1. **Language: Python 3.12 + Typer + Rich.** Subprocesses out to `docker compose`, `op`, `ffmpeg` rather than using SDKs.
2. **Distribution: `uv tool install`.** The CLI is a standalone uv project at `cli/` in the repo, installed globally via `uv tool install /path/to/voxxy-cli` or `uv tool install git+…` later.
3. **Project discovery:** CLI walks up from cwd looking for `compose.yml` + `engines/`; falls back to `$VOXXY_HOME`; falls back to `~/.config/voxxy/config.toml :: project_root`. Fails fast with a clear error if none resolve.
4. **Secrets:** CLI subprocesses `op run --env-file {project}/.env.template -- <cmd>` for any task touching compose/docker. Fails fast if `op` is unauthenticated.
5. **Boot persistence:** Already handled by docker's `restart: unless-stopped` policy + systemd-enabled `docker.service`. `daemon install` verifies, does not duplicate.
6. **State scope:** Ephemeral per-project state (current engine order) lives at `{project}/.voxxy.state.json` (gitignored). User prefs live at `~/.config/voxxy/config.toml`.
7. **Coexistence (option C):** `mise.toml` tasks remain as thin aliases calling `voxxy`. `scripts/vox-speak` becomes a symlink/shim to `voxxy speak --raw`. Single source of truth; muscle memory preserved.
8. **No new wire contract.** The CLI is a client of the existing HTTP surface (`/healthz`, `/voices`, `/synthesize-url`). It does NOT talk directly to engine sidecars or postgres.
9. **Interactive vs scripting:** interactive prompts default-on when `sys.stdin.isatty()`; bypass via `--no-prompt` + explicit flags.
10. **Python deps for client-side validation:** CLI imports pydantic models from `app/engine_contract.py` OR vendor-copies them (TBD in §11). Zero torch/voxcpm/transformers deps in the CLI venv.

## 4. Core Features & Acceptance Criteria

### AC1: Command tree

Concrete surface for v1:

```
voxxy daemon
  start                   Start the full stack (core + engines). Restores state.
  stop                    Stop all containers.
  restart                 Recreate core only (fast, picks up app/ changes).
  reset                   Destructive: stop + remove containers + wipe audio-cache. Prompts.
  status                  Health + container states, tabled.
  install                 Bootstrap: install CLI, create config, completions, prereq check.

voxxy engine
  list                    Table of engines, ready state, VRAM, capabilities.
  use <name>              Make <name> primary. Persists, recreates core.
  enable <name>           Add to chain end if absent. Recreates core.
  disable <name>          Remove from chain. Recreates core.
  logs <name>             docker logs -f voxxy-engine-<name>.

voxxy voice
  list                    Table of voices, tags, duration, per-engine ref paths.
  info <name>             Full detail for one voice.
  add <path>              Interactive: preprocess + prompt metadata + upload.
                          Flags: --name, --tags, --display-name, --no-prompt,
                                 --engine (comma-separated; default both local engines),
                                 --trim-seconds (default 8).
  delete <name>           Prompts for confirmation. --yes to skip.

voxxy speak [TEXT]        Synthesize. Text from args or stdin.
                          Flags: --voice (default from config), --engine (override primary),
                                 --out FILE (write file), --raw (WAV to stdout), --play (default).
                          Emits engine name to stderr on success.

voxxy health              Formatted /healthz output. Exit code reflects status.
voxxy logs                docker logs -f vox (core).
voxxy version             CLI version + server version from /healthz.
```

### AC2: `daemon install` behavior

On a fresh workstation:

1. Verify prereqs: `docker`, `docker compose`, `op`, `ffmpeg`, `psql` present. Report missing with install hints.
2. Verify `nvidia` runtime is default (`docker info`). Warn if not — engines will fail.
3. Resolve project root (must be run from inside the repo OR pass `--project`).
4. Create `~/.config/voxxy/config.toml` if absent, populated with resolved `project_root`, `default_url` (`https://vox.delo.sh`), `default_voice` (`rick`).
5. Install `voxxy` via `uv tool install {project}/cli` (idempotent).
6. Generate shell completions for detected shell and install to the right path.
7. Confirm `restart: unless-stopped` policy is present on all services and docker.service is enabled. If not, print the command to enable it.
8. Offer (optional, prompted) to install a systemd user unit that runs `voxxy daemon status --wait-healthy --apply-migrations` after boot. Skipped by default.

### AC3: `engine use <name>` persistence + reload

1. Validate `<name>` appears in the current `/healthz` engines list. Error otherwise.
2. Build new `VOX_ENGINES` string: `<name>=URL,<remaining-in-original-order>=URLs`.
3. Write to `{project}/.voxxy.state.json :: VOX_ENGINES`.
4. Subprocess `op run --env-file .env.template -- docker compose ... up -d --force-recreate vox` with `VOX_ENGINES` injected into the env.
5. Poll `/healthz` until `engines[0].name == <name>` and `ready == true`, timeout 60s.
6. Report new order.

### AC4: `voice add <path>` interactive flow

```
$ voxxy voice add /tmp/morty.ogg
→ probing audio: 44100 Hz, 2ch, 17.3s
→ preprocessing to 24kHz mono, 8s trim: /tmp/voxxy-prep.wav (ok)
? Voice name (slug): morty
? Display name (shown in lists): Morty
? Tags (comma-separated): rickandmorty,cartoon,funny
? Apply to engines [voxcpm, vibevoice]: (enter for default)
→ POST /voices... created (id=morty, vibevoice_ref_path=morty.wav)
```

Non-interactive:
```
voxxy voice add /tmp/morty.ogg --name morty --tags rickandmorty,cartoon --no-prompt
```

Preprocessing pipeline (ffmpeg subprocess):
- Probe duration + channels + sample rate (`ffprobe -of json`)
- Transcode to 24kHz mono WAV, trimmed to `--trim-seconds` (default 8)
- Upload the preprocessed WAV, not the original, so server-side upload always gets clean input

### AC5: `speak` behavior

- `voxxy speak "hello"` → synthesize with default voice, play via `paplay` or system default audio player
- `voxxy speak --raw "hi" > out.wav` → WAV to stdout (matches current `vox-speak --raw`)
- `voxxy speak --out foo.ogg "hi"` → write OGG/Opus directly (via `/synthesize-url` + fetch)
- `voxxy speak` with no args → read text from stdin (for `echo ... | voxxy speak`)
- `--engine vibevoice` → temporarily override `VOX_ENGINES` for this one request (if server supports per-request engine override; if not, warn and route through default)

**Note:** per-request engine override requires server-side support that doesn't exist today. V1 either drops this flag or implements it by temporarily flipping `engine use` (expensive). Flagged in §11.

### AC6: Config discovery

Search order for project root:
1. `--project /path` CLI flag
2. `VOXXY_HOME` env var
3. `~/.config/voxxy/config.toml :: project_root`
4. Walk up from cwd, looking for a dir containing both `compose.yml` AND `engines/`
5. Error out with the exact search path tried

### AC7: State file

`{project}/.voxxy.state.json`:
```json
{
  "vox_engines": "voxcpm=http://voxxy-engine-voxcpm:8000,vibevoice=http://voxxy-engine-vibevoice:8000",
  "last_engine_change": "2026-04-24T10:15:00Z",
  "last_engine_change_by": "engine use vibevoice"
}
```

`daemon start` reads this file and injects `VOX_ENGINES` into the compose env (prepended before `op run`). `.gitignore` entry ensures it never ships.

### AC8: Coexistence (Option C)

- All `mise.toml` tasks continue to work but internally call `voxxy`:
  ```toml
  [tasks.up]
  run = "voxxy daemon start"
  ```
- `scripts/vox-speak` becomes a compatibility wrapper:
  ```bash
  #!/usr/bin/env bash
  # Compatibility shim: forwards to `voxxy speak`.
  exec voxxy speak "$@"
  ```
  Existing flag surface (`--voice`, `--raw`, `--via`, `--url`) is preserved by `voxxy speak` accepting the same flags.
- Remote ssh pipeline keeps working: `ssh host vox-speak --raw "hi"` and `ssh host voxxy speak --raw "hi"` are equivalent.

### AC9: Exit codes + output

- Exit 0 on success, non-zero on any failure (shell-script friendly)
- Default output: human-readable Rich tables
- `--json` flag on list/info/status/health for machine consumption
- Structured errors to stderr; stdout reserved for data

## 5. Architecture

```
voxxy CLI (Python/Typer/Rich)
├── cli/voxxy/
│   ├── __main__.py          # entrypoint: python -m voxxy
│   ├── app.py               # top-level Typer app
│   ├── config.py            # load ~/.config/voxxy/config.toml + discovery
│   ├── state.py             # read/write {project}/.voxxy.state.json
│   ├── client.py            # httpx wrapper over core's HTTP API
│   ├── docker.py            # subprocess wrappers: compose, op run
│   ├── audio.py             # ffmpeg/ffprobe subprocess helpers
│   ├── commands/
│   │   ├── daemon.py        # start/stop/restart/reset/status/install
│   │   ├── engine.py        # list/use/enable/disable/logs
│   │   ├── voice.py         # list/info/add/delete
│   │   ├── speak.py         # synthesis + playback
│   │   └── util.py          # health/logs/version
│   └── contract.py          # vendored pydantic models from app/engine_contract.py
├── cli/pyproject.toml
└── cli/uv.lock
```

**Data flow for `voxxy voice add`:**

```
user → cli/voxxy/commands/voice.py :: cmd_add
       → cli/voxxy/audio.py :: preprocess (ffprobe + ffmpeg)
       → prompt for metadata (Rich)
       → cli/voxxy/client.py :: post_voice (httpx → vox.delo.sh/voices)
       → render result (Rich table)
```

**Data flow for `voxxy engine use vibevoice`:**

```
user → cli/voxxy/commands/engine.py :: cmd_use
       → cli/voxxy/client.py :: get_healthz → validate engine exists
       → cli/voxxy/state.py :: write new VOX_ENGINES
       → cli/voxxy/docker.py :: recreate_core (op run + compose)
       → poll /healthz until engines[0].ready
       → render success table
```

## 6. Tech Stack

- **Python 3.12** matching the rest of the project
- **Typer** for the command tree (built on click; type-hinted, auto-completion, rich help)
- **Rich** for tables + prompts + progress bars
- **httpx** for HTTP (already used by core; familiar)
- **tomllib** (stdlib) for config read, **tomli-w** for config write
- **subprocess** for docker/op/ffmpeg — no SDK deps (keeps the CLI venv tiny)
- **No torch/transformers/voxcpm** — CLI runs anywhere including CPU-only laptops

## 7. Project Structure (post-implementation)

```
voxxy/
├── app/                        # core (unchanged)
├── engines/                    # sidecars (unchanged)
├── cli/                        # NEW: voxxy CLI
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── .python-version
│   └── voxxy/
│       ├── __init__.py
│       ├── __main__.py
│       ├── app.py
│       ├── config.py
│       ├── state.py
│       ├── client.py
│       ├── docker.py
│       ├── audio.py
│       ├── contract.py
│       └── commands/
│           ├── __init__.py
│           ├── daemon.py
│           ├── engine.py
│           ├── voice.py
│           ├── speak.py
│           └── util.py
├── mise.toml                   # tasks become aliases (AC8)
├── scripts/
│   └── vox-speak               # becomes shim (AC8)
├── docs/specs/voxxy-cli.md     # this file
├── docs/runbooks/              # existing
├── compose.yml
├── compose.engines.yml
└── .voxxy.state.json           # NEW: gitignored
```

## 8. Boundaries

### Always do

- Use the existing HTTP API (`/healthz`, `/voices`, `/synthesize-url`) as the only server-side surface. Don't talk to engines or postgres directly from the CLI.
- Honor the project-discovery chain in AC6 exactly.
- Preserve `vox-speak` flag compatibility so the shim is transparent.
- Write config/state files with `0600` permissions (no group/other read).
- Fail fast with an actionable error when prereqs are missing (`docker`, `op`, `ffmpeg`).
- Exit 0 only on verified success; non-zero on any failure.

### Ask first

- Any new server-side endpoint required by the CLI (e.g. per-request engine override).
- Distributing the CLI as a PyPI package or GitHub release.
- Adding telemetry/analytics.
- Changing the `voices` schema to support something the CLI surfaces (e.g. UI-only metadata fields).
- Breaking `vox-speak` flag compatibility.

### Never do

- Don't reimplement `docker compose` — always subprocess.
- Don't read secrets from the project's `.env` or 1password directly; always go through `op run --env-file`.
- Don't cache `/healthz` responses; they're cheap and staleness hurts `engine use`.
- Don't silently swallow docker subprocess failures; surface stderr verbatim.
- Don't write to the voices bind mount from the CLI (`/data/voices` inside core). Use the HTTP API.
- Don't persist destructive actions (`daemon reset`) without an interactive confirm (or `--yes`).

## 9. Testing Strategy

No test suite exists in-repo today. Adopt these additions scoped to the CLI:

- **Unit tests** (pytest in `cli/tests/`) for:
  - Config + state file read/write
  - Project discovery walk-up logic
  - VOX_ENGINES string builder (`engine use` reorder math)
  - Audio preprocess argv construction
- **Integration smoke** (shell) added to `scripts/verify-engine-contract.sh --live` tail:
  - `voxxy health` exits 0 and mentions all configured engines
  - `voxxy voice list` returns non-empty
  - `voxxy speak --raw "test"` writes valid WAV bytes
- **No e2e tests that mutate state** (voice add/delete, engine use, daemon reset) in CI without opt-in. Gated by `VOX_TEST_MUTATIONS=1`.

## 10. Migration / Rollback Plan

**Migration (Option C):**
1. Build CLI at `cli/`, verify passing tests
2. `uv tool install` locally, verify `voxxy --help` works
3. Switch `mise.toml` tasks to call `voxxy` internally (atomic commit)
4. Replace `scripts/vox-speak` with shim (atomic commit, preserves ssh pipeline)
5. Update README / CLAUDE.md to lead with `voxxy`, keep mise + vox-speak as secondary docs

**Rollback:**
- `mise.toml` + `scripts/vox-speak` revert via git; original logic is preserved in history
- CLI uninstall: `uv tool uninstall voxxy`
- Config/state: `rm ~/.config/voxxy/config.toml {project}/.voxxy.state.json`
- No schema or contract changes, so rollback is purely CLI-side

## 11. Resolved Decisions

1. **Contract module:** Vendor-copy `app/engine_contract.py` into `cli/voxxy/contract.py`. Keeps CLI's uv lock independent. Drift risk managed by convention (update all three when the contract changes).

2. **`speak --engine` per-request override:** Dropped from v1. Follow-up when the server grows a per-request override endpoint.

3. **`daemon install`:** Installs CLI globally via `uv tool install`. Project discovery (§AC6) handles multi-project later.

4. **Shell completions:** Typer generates; `voxxy daemon install --completions` installs to the detected shell's path. Otherwise emit to stdout with a documented one-liner to pipe into the right file.

5. **`voice add --engine` semantics:** Flag controls which engine-specific columns get populated (vs NULL). Same clip uploaded once; multi-clip-per-engine is a v2 feature.

6. **`daemon reset`:** Compose down + audio-cache wipe only. Voice rows are user content; dropping them requires explicit `voice delete --all --yes`.

---

## Sign-off checklist (before moving to `plan`)

- [ ] Command tree in §4 AC1 is the right v1 surface
- [ ] `daemon install` scope in AC2 acceptable
- [ ] Architecture + package layout (§5, §7) acceptable
- [ ] Coexistence approach (AC8, option C) confirmed
- [ ] Open questions in §11 resolved (especially 1, 2, 5)
