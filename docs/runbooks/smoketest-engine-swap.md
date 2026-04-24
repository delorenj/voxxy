# Smoketest: Engine Swap + Voice Cloning

Five-scenario walkthrough that exercises the decoupled engine topology end-to-end,
driven entirely from the `voxxy` CLI.

**Related:** [[engine-decoupling|docs/specs/engine-decoupling.md]] · [[engine-decoupling.plan|docs/specs/engine-decoupling.plan.md]] · [[voxxy-cli|docs/specs/voxxy-cli.md]]

**Scope:**

1. Bring up the stack with voxcpm primary
2. Speak with the default voice (routes to voxcpm)
3. Flip to vibevoice primary
4. Speak with the default voice (routes to vibevoice)
5. Clone a new voice via vibevoice zero-shot

All commands assume `voxxy` is on `$PATH` (`uv tool install /home/delorenj/code/voxxy/cli`
has been run) and a fresh shell. `voxxy` discovers the project root via `VOXXY_HOME`,
config, or cwd walk-up — so you don't need to `cd` into the repo for most commands.

---

## 1. Bring up the stack, voxcpm primary

Default state (no `.voxxy.state.json`) puts voxcpm first in `VOX_ENGINES`.
`daemon start` restores the persisted order if present, otherwise uses the
default from `compose.yml`.

```bash
voxxy daemon start
voxxy health
```

**Expected `health` output:**

```json
{
  "status": "ok",
  "engines": [
    { "name": "voxcpm",     "ready": true },
    { "name": "vibevoice",  "ready": true },
    { "name": "elevenlabs", "ready": true }
  ]
}
```

If `voxcpm.ready=false`, tail `voxxy engine logs voxcpm`. First-boot model load is ~45s.

Full container state + rollup:

```bash
voxxy daemon status
```

---

## 2. Speak with default voice -> voxcpm

The default voice is `rick` (`voices/rick.wav`, referenced by `VOX_VOICE` env
or the `default_voice` key in `~/.config/voxxy/config.toml`).

```bash
voxxy speak "Hello from VoxCPM on the decoupled stack."
```

Or write to a file for inspection:

```bash
voxxy speak --out /tmp/voxcpm.ogg "Hello from VoxCPM."
```

**Expected:** the engine name is echoed to stderr on success (e.g. `engine=voxcpm`).
The underlying `POST /synthesize-url` response carries `"engine": "voxcpm"`
plus an `X-Vox-Engine: voxcpm` header.

Confirm structured log:

```bash
voxxy logs core --tail 5 | grep synth.completed
# → synth.completed engine=voxcpm text_len=N bytes=N sample_rate=48000 fallback_from=
```

---

## 3. Flip to vibevoice primary

Engines already running; only core needs to rebind to the reordered registry.
`voxxy engine use` persists the new order to `.voxxy.state.json`, recreates
core with the right `VOX_ENGINES`, and polls `/healthz` until the new primary
reports `ready`.

```bash
voxxy engine use vibevoice
voxxy engine list   # vibevoice first
```

`engine list` shows the full chain with per-engine VRAM and capabilities; the
primary is the first row. No raw `docker compose` or `op run` needed.

---

## 4. Speak with default voice -> vibevoice

Same invocation as step 2; primary is now vibevoice:

```bash
voxxy speak "Hello from VibeVoice on the same stack."
```

**Expected:** `engine=vibevoice` echoed to stderr; response carries
`"engine": "vibevoice"` + `X-Vox-Engine: vibevoice` header.

Verify engine-side:

```bash
voxxy engine logs vibevoice --tail 20 | tail -5
# → POST /v1/synthesize HTTP/1.1" 200 OK
```

Audio should be noticeably different timbre from step 2 (different model, same reference).

---

## 5. Clone a new voice via vibevoice zero-shot

### 5a. Prepare a reference clip

VibeVoice quality degrades above ~10s; `voxxy voice add` runs its own
preprocessing (ffprobe + ffmpeg trim + mono downmix + 24 kHz resample) and
uploads the clean WAV. You can skip manual prep entirely:

```bash
# Raw source is fine — voxxy preprocesses before upload
voxxy voice add /path/to/source.wav --name demoguy --tags demo,male --no-prompt
```

If you want to pre-trim manually for any reason, the equivalent ffmpeg is:

```bash
ffmpeg -i /path/to/source.wav -ss 5 -t 8 -ac 1 -ar 24000 /tmp/new_voice.wav
voxxy voice add /tmp/new_voice.wav --name demoguy --tags demo,male --no-prompt
```

### 5b. Interactive flow (alternative)

Drop `--no-prompt` for the interactive path:

```bash
voxxy voice add /tmp/new_voice.wav
# → probing audio: 44100 Hz, 2ch, 17.3s
# → preprocessing to 24kHz mono, 8s trim: /tmp/voxxy-prep.wav (ok)
# ? Voice name (slug): demoguy
# ? Display name: Demo Guy
# ? Tags (comma-separated): demo,male
# ? Apply to engines [voxcpm, vibevoice]: (enter for default)
# → POST /voices... created (id=demoguy, vibevoice_ref_path=demoguy.wav)
```

### 5c. Confirm registration

```bash
voxxy voice list                  # tabled view
voxxy voice info demoguy          # full detail incl. vibevoice_ref_path
voxxy voice info demoguy --json   # machine-readable
```

`vibevoice_ref_path` should be `demoguy.wav` (auto-populated on upload, per T3.5).

### 5d. Speak with the cloned voice

```bash
voxxy speak --voice demoguy "This is a zero-shot clone through VibeVoice."
```

**Expected:** `engine=vibevoice`, output voice matches the timbre of the source clip.

### 5e. Cross-engine sanity (optional)

Upload auto-populates both engine ref paths. Swap back to voxcpm to confirm:

```bash
voxxy engine use voxcpm
voxxy speak --voice demoguy "Same clip, different engine."
# → engine=voxcpm, different-sounding clone of the same source
```

---

## Restore default state

```bash
voxxy engine use voxcpm
```

Restores the default primary (voxcpm), with vibevoice secondary and elevenlabs
terminal fallback. State is persisted so subsequent `voxxy daemon start` boots
with this order.

To drop back to a clean slate (remove persisted state + wipe audio cache):

```bash
voxxy daemon reset
rm -f /home/delorenj/code/voxxy/.voxxy.state.json
voxxy daemon start
```

To also remove the throwaway clone from step 5:

```bash
voxxy voice delete demoguy --yes
```

---

## Failure-mode matrix

| Step | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `ready=false` on voxcpm | Model still loading | Wait, retry `voxxy health` |
| 2/4 | 500, `synth.failed` in logs | Engine OOM or crashed | `voxxy engine logs <engine>`; VRAM pressure likely |
| 4 | Engine returns `voxcpm` not `vibevoice` | `voxxy engine use` didn't recreate core, or `.voxxy.state.json` stale | `voxxy engine list` to verify chain; `voxxy daemon restart` |
| 5a | `voice add` fails at probe | Audio format unreadable by ffprobe | Convert to WAV/OGG first; `ffmpeg -i src.ext out.wav` |
| 5d | 400 `No valid speaker lines` | Speaker-label auto-promote regex missed | Prefix text with `Speaker 1: ` manually via `voxxy speak "Speaker 1: ..."` |
| 5d | Clone sounds like default, not source | `vibevoice_ref_path` NULL | `voxxy voice info demoguy --json` and check the field |

---

## See also

- [[engine-decoupling|docs/specs/engine-decoupling.md]] full architecture
- [[voxxy-cli|docs/specs/voxxy-cli.md]] CLI surface + design
- `scripts/verify-engine-contract.sh --live` cheaper health-only probe
- `mise run smoke` (alias for `voxxy` default-engine codec probe; subset of this runbook)
