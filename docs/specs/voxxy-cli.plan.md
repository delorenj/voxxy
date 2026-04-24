# Plan: `voxxy` unified CLI

- **Spec:** [voxxy-cli.md](./voxxy-cli.md)
- **Status:** Ready for build
- **Date:** 2026-04-24

---

## Dependency Graph

```
Phase 0 (scaffold + vendor contract)
    ↓
Phase 1 (foundation: config, state, client, subprocess helpers)
    ↓
Phase 2 (read-only commands: health, version, engine list, voice list/info, logs)
    ↓
        ┌──→ Phase 3 (mutating stack + engine commands)
        │         ↓
        │         └──→ Phase 5 (speak + compat shim)
        │                   ↓
        └──→ Phase 4 (voice lifecycle: add, delete)  → Phase 6 (install + integration)
                                                           ↓
                                                      Phase 7 (polish)
```

**Slicing principle:** each phase lands a visibly usable slice. Read-only before write so the seam is proven cheaply before anything mutates state.

| Phase | End state | Rollback |
|-------|-----------|----------|
| 0 | `cli/` scaffold builds + lints; no CLI behavior yet | delete `cli/` |
| 1 | CLI package can call core's HTTP API + resolve config/state; no commands yet | revert commits |
| 2 | `voxxy health`, `voxxy voice list`, `voxxy engine list`, `voxxy logs`, `voxxy version` all work | revert |
| 3 | `voxxy daemon *` + `voxxy engine use/enable/disable` work end-to-end | revert; stack state unchanged |
| 4 | `voxxy voice add/delete` work end-to-end | revert; voices unchanged |
| 5 | `voxxy speak` works; `vox-speak` symlink forwards correctly | restore `scripts/vox-speak` from git |
| 6 | `voxxy daemon install` bootstrap + mise aliases + docs | revert |
| 7 | Tests + `--json` + polish | non-functional |

---

## Phase 0 — Scaffold + vendor contract (S)

### T0.1 — CLI project scaffold
- **Size:** S
- **Deps:** none
- **Files:** `cli/pyproject.toml`, `cli/uv.lock`, `cli/.python-version`, `cli/voxxy/__init__.py`, `cli/voxxy/__main__.py`, `cli/voxxy/app.py`, `cli/README.md`
- **Acceptance:**
  - `name = "voxxy"` in pyproject with `[project.scripts] voxxy = "voxxy.app:main"`
  - Python 3.12; deps: `typer>=0.12`, `rich>=13`, `httpx>=0.27`, `pydantic>=2`, `tomli-w>=1`, `questionary>=2` (or `prompt-toolkit` via Rich)
  - `voxxy.app:main` is a Typer app that registers a single `version` command returning `"voxxy 0.1.0"` so the entrypoint works
  - No network/subprocess calls yet
- **Verify:**
  ```bash
  cd cli && uv lock && uv sync
  uv run voxxy version
  # expect: "voxxy 0.1.0"
  ```

### T0.2 — Vendor contract models
- **Size:** S
- **Deps:** T0.1
- **Files:** `cli/voxxy/contract.py` (vendor-copy of `app/engine_contract.py`)
- **Acceptance:**
  - Byte-identical copy (use `cp`)
  - Importable from the CLI venv (no app.* refs)
- **Verify:**
  ```bash
  diff /home/delorenj/code/voxxy/app/engine_contract.py \
       /home/delorenj/code/voxxy/cli/voxxy/contract.py
  cd cli && uv run python -c "from voxxy.contract import EngineHealth; print('ok')"
  ```

**Phase 0 checkpoint:** `uv run voxxy version` prints; contract importable.

---

## Phase 1 — Foundation (M)

Four small modules; each self-contained. No Typer commands yet.

### T1.1 — `config.py`: user config + project discovery
- **Size:** S
- **Deps:** T0.1
- **Files:** `cli/voxxy/config.py`
- **Acceptance:**
  - `load_config() -> Config` reads `~/.config/voxxy/config.toml` (fields: `project_root`, `default_url`, `default_voice`)
  - `save_config(cfg)` writes with `0600` perms (via `os.open(..., 0o600)` + `tomli_w`)
  - `discover_project_root()` implements AC6 walk-up: `--project` flag → `$VOXXY_HOME` → config → walk up looking for `compose.yml` + `engines/`
  - Raises a typed `ProjectNotFound` with actionable message listing the paths tried
- **Verify:**
  ```bash
  cd cli && uv run python -c "
  from voxxy.config import discover_project_root
  import os; os.chdir('/home/delorenj/code/voxxy/engines/voxcpm')
  print(discover_project_root())
  # expect: /home/delorenj/code/voxxy
  "
  ```

### T1.2 — `state.py`: project state file
- **Size:** S
- **Deps:** T1.1
- **Files:** `cli/voxxy/state.py`
- **Acceptance:**
  - `load_state(project_root: Path) -> State` reads `{project}/.voxxy.state.json`, returns defaults if absent
  - `save_state(project_root, state)` writes with `0600` perms
  - `State` fields: `vox_engines: str`, `last_engine_change: datetime | None`, `last_engine_change_by: str | None`
  - `.voxxy.state.json` added to project `.gitignore` (one-line edit)
- **Verify:**
  ```bash
  cd cli && uv run python -c "
  from pathlib import Path
  from voxxy.state import load_state, save_state, State
  import tempfile
  with tempfile.TemporaryDirectory() as d:
      save_state(Path(d), State(vox_engines='x=http://y:8000'))
      s = load_state(Path(d))
      assert s.vox_engines == 'x=http://y:8000'
      print('state ok')
  "
  ```

### T1.3 — `client.py`: httpx wrapper over core's HTTP API
- **Size:** M
- **Deps:** T0.2, T1.1
- **Files:** `cli/voxxy/client.py`
- **Acceptance:**
  - `VoxClient(base_url)` with methods: `healthz() -> HealthResponse`, `list_voices() -> list[VoiceOut]`, `get_voice(name) -> VoiceOut`, `create_voice(name, display_name, tags, audio_bytes, **kwargs) -> VoiceOut`, `delete_voice(name)`, `synthesize_url(text, voice, cfg, steps) -> SynthUrlResponse`, `fetch_audio(url) -> bytes`
  - Response pydantic models defined locally (mirror core's `VoiceOut`/`SynthesizeUrlResponse`), OR import from vendored contract + a small `api.py` with client-response-specific models
  - All methods raise typed exceptions (`VoxNotFound`, `VoxValidationError`, `VoxServerError`) based on HTTP status
  - Default timeout 30s; `X-Vox-Engine` response header exposed on synth calls
- **Verify:**
  ```bash
  cd cli && uv run python -c "
  from voxxy.client import VoxClient
  c = VoxClient('https://vox.delo.sh')
  hc = c.healthz()
  print(hc.status, [e.name for e in hc.engines])
  # expect: ok ['voxcpm', 'vibevoice', 'elevenlabs']
  "
  ```

### T1.4 — `docker.py`: compose + op subprocess helpers
- **Size:** S
- **Deps:** T1.1
- **Files:** `cli/voxxy/docker.py`
- **Acceptance:**
  - `compose_up(project_root, *, full=True, services=None, recreate=False, env=None)` runs `op run --env-file .env.template -- docker compose -f compose.yml [-f compose.engines.yml] up -d [--force-recreate] [services...]` with stderr streamed
  - `compose_down(project_root, *, full=True)` runs the corresponding down
  - `compose_build(project_root, services=None, no_cache=False)`
  - `container_status(name) -> Literal['running','exited','missing','restarting']` via `docker inspect`
  - `logs_follow(container_name)` execs `docker logs -f` via `os.execvp` so Ctrl-C detaches cleanly
  - All subprocess calls raise `DockerError` with captured stderr on non-zero exit
  - Exit codes preserved from underlying docker where meaningful
- **Verify:**
  ```bash
  cd cli && uv run python -c "
  from voxxy.docker import container_status
  print(container_status('vox'))
  # expect: running
  "
  ```

### T1.5 — `audio.py`: ffmpeg + ffprobe subprocess helpers
- **Size:** S
- **Deps:** T0.1
- **Files:** `cli/voxxy/audio.py`
- **Acceptance:**
  - `probe(path) -> AudioInfo` (duration, channels, sample_rate, codec) via `ffprobe -of json`
  - `preprocess(src, dst, *, sample_rate=24000, channels=1, trim_seconds=8)` transcodes via ffmpeg
  - `AudioInfo` is a simple dataclass
  - Missing `ffmpeg`/`ffprobe` raises `FfmpegMissing` with install hint
- **Verify:**
  ```bash
  cd cli && uv run python -c "
  from pathlib import Path
  from voxxy.audio import probe, preprocess
  import tempfile
  i = probe(Path('/home/delorenj/code/voxxy/voices/rick.wav'))
  print(i)
  with tempfile.NamedTemporaryFile(suffix='.wav') as t:
      preprocess(Path('/home/delorenj/code/voxxy/voices/rick.wav'), Path(t.name))
      j = probe(Path(t.name))
      assert j.sample_rate == 24000 and j.channels == 1
      print('preprocess ok')
  "
  ```

**Phase 1 checkpoint:** every foundation module importable + individually verified against the live stack.

---

## Phase 2 — Read-only commands (M)

All commands registered as Typer sub-apps on the main app. Each exercises one or more foundation modules.

### T2.1 — Entrypoint wiring + sub-app registration
- **Size:** S
- **Deps:** Phase 1
- **Files:** `cli/voxxy/app.py`, `cli/voxxy/commands/__init__.py`, `cli/voxxy/commands/{daemon,engine,voice,speak,util}.py` (stubs)
- **Acceptance:**
  - Typer sub-apps mounted: `daemon`, `engine`, `voice` as groups; `speak`, `health`, `version`, `logs` as top-level commands
  - All stub commands print "TBD" and exit 0
  - `--project` and `--url` global options on the root app
  - `--json` and `--quiet` global flags added but not yet honored
- **Verify:**
  ```bash
  voxxy --help
  voxxy daemon --help
  voxxy engine --help
  # all render cleanly
  ```

### T2.2 — `voxxy health` + `voxxy version`
- **Size:** S
- **Deps:** T2.1, T1.3
- **Files:** `cli/voxxy/commands/util.py`
- **Acceptance:**
  - `voxxy health` GETs `/healthz` via `client.py`, renders a Rich table with engine name + ready + vram + capabilities. Exit 0 if `status=="ok"`, 2 if `"degraded"`, 3 if unreachable.
  - `voxxy version` prints CLI version AND `/healthz` for the server, noting if unreachable.
  - `--json` flag dumps raw response instead of table.
- **Verify:**
  ```bash
  voxxy health
  voxxy health --json | jq .status
  voxxy version
  ```

### T2.3 — `voxxy engine list` + `voxxy engine logs` + `voxxy logs`
- **Size:** S
- **Deps:** T2.2, T1.4
- **Files:** `cli/voxxy/commands/engine.py`, `cli/voxxy/commands/util.py`
- **Acceptance:**
  - `voxxy engine list` tables engines from `/healthz` (name, ready, capabilities.output_sample_rate, needs_transcript, max_ref_seconds)
  - `voxxy engine logs <name>` execs `docker logs -f voxxy-engine-<name>` (or `vox` if `<name>=core`)
  - `voxxy logs` = `voxxy engine logs core`
  - Invalid engine name → exit 1 with error listing valid names
- **Verify:**
  ```bash
  voxxy engine list
  voxxy engine logs voxcpm &  # manual: Ctrl-C stops
  ```

### T2.4 — `voxxy voice list` + `voxxy voice info`
- **Size:** S
- **Deps:** T2.2
- **Files:** `cli/voxxy/commands/voice.py`
- **Acceptance:**
  - `voxxy voice list` tables voices (name, display_name, duration_s, tags, engines mapped via vibevoice_ref_path/elevenlabs_voice_id non-null)
  - `voxxy voice info <name>` renders all fields; exits 1 if not found
  - `--json` on both
- **Verify:**
  ```bash
  voxxy voice list
  voxxy voice info rick
  ```

**Phase 2 checkpoint:** read-only surface fully usable against the live stack.

---

## Phase 3 — Stack + engine mutations (M)

### T3.1 — `voxxy daemon start/stop/restart`
- **Size:** S
- **Deps:** T1.4, T1.2
- **Files:** `cli/voxxy/commands/daemon.py`
- **Acceptance:**
  - `voxxy daemon start` reads `.voxxy.state.json`, injects `VOX_ENGINES`, runs `compose_up(full=True)`. Polls `/healthz` until all engines ready (timeout 120s).
  - `voxxy daemon stop` runs `compose_down`.
  - `voxxy daemon restart` recreates only `vox` (core); faster.
  - `--core-only` flag on `start` drops the engines overlay.
  - `--engines-only` flag on `start` starts only engine sidecars.
- **Verify:** `voxxy daemon stop && voxxy daemon start && voxxy health`

### T3.2 — `voxxy daemon status`
- **Size:** S
- **Deps:** T3.1, T1.3
- **Files:** `cli/voxxy/commands/daemon.py`
- **Acceptance:**
  - Tables: container name, state (running/exited/missing), health (from `/healthz`), image, VRAM if known
  - `--wait-healthy --timeout 60` flag polls until all green
  - Exit 0 if all healthy, 2 if degraded, 3 if unreachable
- **Verify:** `voxxy daemon status`

### T3.3 — `voxxy daemon reset`
- **Size:** S
- **Deps:** T3.1
- **Files:** `cli/voxxy/commands/daemon.py`
- **Acceptance:**
  - Prompts: "This will stop all containers and wipe audio-cache. Voices are preserved. Continue? [y/N]"
  - `--yes` skips prompt
  - Runs: `compose down`; `rm -rf {project}/audio-cache/*`
  - Does NOT touch `voices/`, postgres, or HF cache
- **Verify:** `voxxy daemon reset --yes` then `voxxy daemon start`

### T3.4 — `voxxy engine use/enable/disable`
- **Size:** M
- **Deps:** T3.1, T1.2, T1.3
- **Files:** `cli/voxxy/commands/engine.py`, `cli/voxxy/state.py` (helper)
- **Acceptance:**
  - `voxxy engine use <name>`: validate engine exists in `/healthz`; build new VOX_ENGINES with `<name>` first, others preserved in original order; save state; recreate core; poll until `engines[0] == <name>`
  - `voxxy engine enable <name>`: append to chain if absent; error if not in `compose.engines.yml`
  - `voxxy engine disable <name>`: drop from chain; error if only engine remaining (would leave ElevenLabs-only)
  - All three write to `.voxxy.state.json` with `last_engine_change_by`
  - Engine URL resolution: hard-coded map `{name: http://voxxy-engine-<name>:8000}` for now (v2 could auto-discover from compose)
- **Verify:**
  ```bash
  voxxy engine use vibevoice
  voxxy speak "primary is vibevoice now"   # should route to vibevoice
  voxxy engine use voxcpm
  ```

**Phase 3 checkpoint:** full stack lifecycle controllable via CLI. State persists across restarts.

---

## Phase 4 — Voice lifecycle (M)

### T4.1 — `voxxy voice add <path>` interactive
- **Size:** M
- **Deps:** T1.3, T1.5, T2.4
- **Files:** `cli/voxxy/commands/voice.py`
- **Acceptance:**
  - Probe input audio; warn if already close to spec (skip preprocess)
  - Preprocess to `/tmp/voxxy-<uuid>.wav` at 24kHz mono, `--trim-seconds` (default 8)
  - Interactive prompts (when stdin is TTY): name (slug validated `^[a-z0-9-]+$`), display name (default: name.title()), tags (comma-separated), target engines (checkboxes: voxcpm, vibevoice, both; default both)
  - POST to `/voices` with preprocessed WAV
  - Render created Voice as a Rich panel
  - Temp file cleanup on success or failure
- **Verify:**
  ```bash
  voxxy voice add /path/to/some.ogg
  # interactive flow; creates voice and surfaces ID
  voxxy voice info <new-name>
  ```

### T4.2 — `voxxy voice add --no-prompt`
- **Size:** S
- **Deps:** T4.1
- **Files:** same as T4.1
- **Acceptance:**
  - `--name`, `--display-name`, `--tags`, `--engine` flags honored
  - `--no-prompt` skips all interactive prompts; errors if required flags missing
  - Auto-detect `--no-prompt` when `not sys.stdin.isatty()` so piped stdin doesn't block
- **Verify:**
  ```bash
  voxxy voice add /path/to.ogg --name testguy --tags test --no-prompt
  ```

### T4.3 — `voxxy voice delete`
- **Size:** S
- **Deps:** T1.3
- **Files:** `cli/voxxy/commands/voice.py`
- **Acceptance:**
  - Prompts "Delete voice '<name>' (<duration>s, tags=<tags>)? [y/N]"
  - `--yes` skips
  - DELETE `/voices/<name>` via client
  - Exits 1 if voice not found
- **Verify:**
  ```bash
  voxxy voice delete testguy --yes
  voxxy voice list   # testguy gone
  ```

**Phase 4 checkpoint:** full voice CRUD via CLI. Can onboard a brand-new voice in one command.

---

## Phase 5 — Speak + compatibility (M)

### T5.1 — `voxxy speak` text + stdin modes
- **Size:** M
- **Deps:** T1.3, T2.4
- **Files:** `cli/voxxy/commands/speak.py`
- **Acceptance:**
  - `voxxy speak "text"` reads text from args
  - `voxxy speak` with no args and non-TTY stdin reads from stdin
  - Default voice from config (`default_voice`, fallback `rick`)
  - `--voice`, `--cfg`, `--steps` flags
  - Default output: fetch OGG URL + play via `paplay` (Linux), `afplay` (macOS), or `ffplay` as fallback
  - Reports engine name to stderr on success
- **Verify:** `voxxy speak "hello"` plays audio

### T5.2 — `voxxy speak --raw` / `--out`
- **Size:** S
- **Deps:** T5.1
- **Files:** same as T5.1
- **Acceptance:**
  - `--raw`: POST `/synthesize` (not `/synthesize-url`), write WAV bytes to stdout, skip transcode cache
  - `--out FILE`: fetch OGG, write to file (no play)
  - `--play` explicit
  - These three modes mutually exclusive; error if combined
- **Verify:** `voxxy speak --raw "hi" > /tmp/t.wav; file /tmp/t.wav`

### T5.3 — `vox-speak` compat shim
- **Size:** S
- **Deps:** T5.2
- **Files:** `scripts/vox-speak` (rewrite)
- **Acceptance:**
  - Replace current bash implementation with a 3-line shim: `exec voxxy speak "$@"`
  - Voxxy speak's flag surface MUST accept `--voice`, `-v`, `--raw`, `-r`, `--via`, `--url`, `-u`, `--play`, `-p` so every existing invocation works
  - `--via` flag implements the ssh pipeline: `ssh $VIA_HOST voxxy speak --raw "$@" | play-locally`
- **Verify:**
  ```bash
  vox-speak "hi"                        # works
  vox-speak --raw "hi" | paplay         # works
  ssh big-chungus vox-speak --raw "hi" | paplay   # works
  ```

**Phase 5 checkpoint:** `voxxy` replaces `vox-speak` transparently. No breakage in downstream scripts.

---

## Phase 6 — Install + integration (S each, mostly parallel)

### T6.1 — `voxxy daemon install`
- **Size:** M
- **Deps:** Phase 5
- **Files:** `cli/voxxy/commands/daemon.py`
- **Acceptance:**
  - Verify prereqs: `docker`, `docker compose`, `op`, `ffmpeg`, `ffprobe`, `psql`. Missing ones printed with install hint; fatal unless `--skip-prereq-check`
  - Verify nvidia default runtime via `docker info`; warn if not
  - Resolve project root; error if not inside a voxxy project
  - Create `~/.config/voxxy/config.toml` if absent with resolved `project_root`
  - `uv tool install {project}/cli` (idempotent; `--force` to reinstall)
  - `--completions` flag: install shell completions for detected shell ($SHELL)
  - `--systemd` flag (optional): install a user unit `voxxy-boot.service` that runs `voxxy daemon status --wait-healthy` on boot
  - Idempotent: running twice produces no drift
- **Verify:** on a fresh shell: `voxxy daemon install` then `voxxy daemon status`

### T6.2 — mise.toml → voxxy aliases
- **Size:** S
- **Deps:** Phase 5
- **Files:** `mise.toml`
- **Acceptance:**
  - Every existing task becomes a one-line shim: `run = "voxxy <equivalent>"`
  - `mise tasks` still lists everything for discoverability
  - Drop redundant compose-wrapping tasks (everything goes through voxxy)
  - Keep `mise run migrate` as-is (CLI doesn't own postgres ops in v1)
  - Keep `mise run install-cli` as an alias pointing at `voxxy daemon install`
- **Verify:**
  ```bash
  mise run up    # routes through voxxy daemon start
  mise run smoke # still passes
  ```

### T6.3 — Docs update
- **Size:** S
- **Deps:** Phase 5
- **Files:** `README.md`, `CLAUDE.md`, `docs/runbooks/smoketest-engine-swap.md`
- **Acceptance:**
  - README's Quick Start leads with `voxxy` commands
  - Mise tasks documented as "equivalent shortcuts" (not primary)
  - Runbook rewritten to use `voxxy` commands (drops raw `docker compose` + `op run` invocations for most steps)
  - CLAUDE.md's "Common commands" section shows `voxxy` surface; mise tasks in collapsed / appendix form
- **Verify:** visual review

### T6.4 — Extend `verify-engine-contract.sh --live`
- **Size:** S
- **Deps:** Phase 2
- **Files:** `scripts/verify-engine-contract.sh`
- **Acceptance:**
  - `--live` mode also runs `voxxy health --json` and `voxxy voice list --json`, asserting exit 0 + non-empty engines/voices
  - Runs `voxxy speak --raw "verify"` and validates WAV magic
- **Verify:** `bash scripts/verify-engine-contract.sh --live`

### T6.5 — `.gitignore` updates
- **Size:** S
- **Deps:** T1.2
- **Files:** `.gitignore`
- **Acceptance:** Add `.voxxy.state.json`, `cli/.venv/`, `cli/**/__pycache__/`

**Phase 6 checkpoint:** fresh-machine install works end-to-end. Mise + vox-speak are thin shims; voxxy is the single source of truth.

---

## Phase 7 — Polish (S each, parallel)

### T7.1 — Unit tests
- **Size:** S
- **Deps:** Phase 6
- **Files:** `cli/tests/test_config.py`, `test_state.py`, `test_engine_reorder.py`, `test_audio_argv.py`
- **Acceptance:**
  - pytest covers: config file round-trip with `0600` perm assertion, state round-trip, engine reorder math (`use`, `enable`, `disable` produce correct VOX_ENGINES strings), ffmpeg argv construction
  - Runs via `cd cli && uv run pytest`
  - CI-safe (no network, no docker subprocess)
- **Verify:** `cd cli && uv run pytest`

### T7.2 — `--json` honored everywhere
- **Size:** S
- **Deps:** Phase 6
- **Files:** all commands
- **Acceptance:**
  - `voxxy <any-read> --json` dumps machine-readable output instead of table
  - `--quiet` suppresses non-data stdout (still logs errors to stderr)
- **Verify:** `voxxy health --json | jq .` works

### T7.3 — Structured errors
- **Size:** S
- **Deps:** Phase 6
- **Files:** `cli/voxxy/errors.py` (new), propagate through
- **Acceptance:**
  - All raised exceptions typed (ProjectNotFound, DockerError, VoxNotFound, FfmpegMissing, etc.)
  - Top-level error handler in `app.py` catches typed errors and prints a user-friendly message with suggested fix; unexpected exceptions show traceback only with `--debug`
  - Exit codes documented: 0 success, 1 generic failure, 2 degraded, 3 unreachable, 4 not-found, 5 validation
- **Verify:** `voxxy voice info nonexistent; echo $?` → 4

**Phase 7 checkpoint:** CLI is polished, scriptable, testable.

---

## Task Summary Table

| ID | Task | Size | Deps | Phase |
|----|------|------|------|-------|
| T0.1 | CLI scaffold | S | — | P0 |
| T0.2 | Vendor contract | S | T0.1 | P0 ✔ |
| T1.1 | config.py | S | T0.1 | — |
| T1.2 | state.py | S | T1.1 | — |
| T1.3 | client.py | M | T0.2, T1.1 | — |
| T1.4 | docker.py | S | T1.1 | — |
| T1.5 | audio.py | S | T0.1 | P1 ✔ |
| T2.1 | Typer wiring | S | P1 | — |
| T2.2 | health + version | S | T2.1, T1.3 | — |
| T2.3 | engine list + logs | S | T2.2, T1.4 | — |
| T2.4 | voice list + info | S | T2.2 | P2 ✔ |
| T3.1 | daemon start/stop/restart | S | T1.4, T1.2 | — |
| T3.2 | daemon status | S | T3.1 | — |
| T3.3 | daemon reset | S | T3.1 | — |
| T3.4 | engine use/enable/disable | M | T3.1, T1.2 | P3 ✔ |
| T4.1 | voice add interactive | M | T1.3, T1.5, T2.4 | — |
| T4.2 | voice add scripting mode | S | T4.1 | — |
| T4.3 | voice delete | S | T1.3 | P4 ✔ |
| T5.1 | speak text + stdin | M | T1.3 | — |
| T5.2 | speak --raw / --out | S | T5.1 | — |
| T5.3 | vox-speak shim | S | T5.2 | P5 ✔ |
| T6.1 | daemon install | M | P5 | — |
| T6.2 | mise aliases | S | P5 | — |
| T6.3 | docs update | S | P5 | — |
| T6.4 | verify-engine-contract.sh | S | P2 | — |
| T6.5 | gitignore | S | T1.2 | P6 ✔ |
| T7.1 | unit tests | S | P6 | — |
| T7.2 | --json everywhere | S | P6 | — |
| T7.3 | structured errors | S | P6 | P7 ✔ |

**Total:** 27 tasks. 22×S, 5×M, 0×L/XL. Total effort: **L** (same shape as engine-decoupling).

---

## Parallelization opportunities

- **Phase 1 modules are independent** once scaffold lands. Single agent can write all 5, or parallelize 2 agents (client+docker vs config+state+audio).
- **Phase 2 commands are all small** and independent; one agent writes the lot.
- **Phase 3 splits cleanly:** one agent on daemon (T3.1-T3.3), another on engine mutations (T3.4). Both need Phase 1 done.
- **Phase 4 can start in parallel with Phase 3** once Phase 2 + T1.5 land.
- **Phase 6 tasks are all disjoint files** — full 4-agent fan-out (install / mise / docs / verify).
- **Phase 7 is fully parallel** — 3 agents across disjoint file sets.

## Risks and open items

- **vox-speak's `--via` ssh flag:** current bash implementation uses `printf '%q '` for arg quoting. `voxxy speak --via` must preserve that exact behavior (users pipe through for remote synth). Test with text containing spaces, quotes, and backticks before declaring T5.3 done.
- **Shell completions install paths:** `voxxy daemon install --completions` needs to detect $SHELL AND handle the distro quirks (bash-completion on Debian vs /etc/bash_completion.d vs ~/.local/share/bash-completion). Simplest v1: write to `~/.local/share/bash-completion/completions/voxxy` (and zsh equivalent). Document only those.
- **`op` auth:** the CLI subprocesses `op run` for every stack mutation. If `op` isn't authenticated, each call prompts. Can't fix from the CLI side without caching, but flag loudly on first failure with a pointer to `op signin`.
- **`docker logs -f` TTY:** `os.execvp` is the right choice for Ctrl-C behavior, but it replaces the current process — any cleanup the CLI wanted to do after will not run. Not a problem for `logs`, but worth naming.
- **Server-side changes:** none required. CLI is purely a client. Worth reconfirming if any Phase surfaces a need (e.g. `voice add --engine` requires no new endpoint, just different column values in the upsert call which `app/voices.py` already supports).

## Sign-off

- [ ] Plan reviewed, dependency order accepted
- [ ] Parallelization approach accepted
- [ ] Risks acknowledged
- [ ] Ready to start T0.1
