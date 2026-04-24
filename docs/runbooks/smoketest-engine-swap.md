# Smoketest: Engine Swap + Voice Cloning

Five-scenario walkthrough that exercises the decoupled engine topology end-to-end.

**Related:** [[engine-decoupling|docs/specs/engine-decoupling.md]] · [[engine-decoupling.plan|docs/specs/engine-decoupling.plan.md]]

**Scope:**

1. Bring up the stack with voxcpm primary
2. Speak with the default voice (routes to voxcpm)
3. Flip to vibevoice primary
4. Speak with the default voice (routes to vibevoice)
5. Clone a new voice via vibevoice zero-shot

All commands assume `cwd = /home/delorenj/code/voxxy` and a fresh shell.

---

## 1. Bring up the stack, voxcpm primary

Default `compose.yml` already puts voxcpm first in `VOX_ENGINES`. No override needed.

```bash
mise run up
mise run health
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

If `voxcpm.ready=false`, tail `mise run logs:voxcpm`. First-boot model load is ~45s.

---

## 2. Speak with default voice → voxcpm

The default voice is `rick` (`voices/rick.wav`, referenced by `VOX_VOICE` env).

```bash
vox-speak "Hello from VoxCPM on the decoupled stack."
```

Or via raw HTTP:

```bash
curl -fsS -X POST https://vox.delo.sh/synthesize-url \
  -H 'content-type: application/json' \
  -d '{"text":"Hello from VoxCPM.","voice":"rick"}' | python3 -m json.tool
```

**Expected:** JSON body with `"engine": "voxcpm"`, plus `X-Vox-Engine: voxcpm` header.

Confirm structured log:

```bash
docker logs --tail 5 vox | grep synth.completed
# → synth.completed engine=voxcpm text_len=N bytes=N sample_rate=48000 fallback_from=
```

---

## 3. Flip to vibevoice primary

Engines already running; only core needs to rebind to the reordered registry.

```bash
VOX_ENGINES="vibevoice=http://voxxy-engine-vibevoice:8000,voxcpm=http://voxxy-engine-voxcpm:8000" \
  op run --env-file .env.template -- \
  docker compose -f compose.yml -f compose.engines.yml up -d --force-recreate vox

mise run health   # engines array: vibevoice first
```

---

## 4. Speak with default voice → vibevoice

Same invocation as step 2; primary is now vibevoice:

```bash
vox-speak "Hello from VibeVoice on the same stack."
```

**Expected:** `"engine": "vibevoice"`, `X-Vox-Engine: vibevoice` header.

Verify engine-side:

```bash
docker logs --tail 20 voxxy-engine-vibevoice | tail -5
# → POST /v1/synthesize HTTP/1.1" 200 OK
```

Audio should be noticeably different timbre from step 2 (different model, same reference).

---

## 5. Clone a new voice via vibevoice zero-shot

### 5a. Prepare a 3-10s reference clip

VibeVoice quality degrades above 10s. Clean, isolated speech preferred.

```bash
ffmpeg -i /path/to/source.wav -ss 5 -t 8 -ac 1 -ar 24000 /tmp/new_voice.wav
```

- `-ac 1` mono
- `-ar 24000` VibeVoice native rate (resample-anyway but cheap to pre-do)

### 5b. Upload the voice profile

```bash
curl -fsS -X POST https://vox.delo.sh/voices \
  -F 'name=demoguy' \
  -F 'display_name=Demo Guy' \
  -F 'tags=demo,male' \
  -F 'audio=@/tmp/new_voice.wav' | python3 -m json.tool
```

**Expected response** shows `vibevoice_ref_path: "demoguy.wav"` (auto-populated on upload, per T3.5).

### 5c. Confirm registration

```bash
curl -fsS https://vox.delo.sh/voices | python3 -m json.tool | grep -A 5 demoguy
```

### 5d. Speak with the cloned voice

```bash
vox-speak --voice demoguy "This is a zero-shot clone through VibeVoice."
```

**Expected:** `engine=vibevoice`, output voice matches the timbre of `/tmp/new_voice.wav`.

### 5e. Cross-engine sanity (optional)

Upload auto-populates both engine ref paths. Swap back to voxcpm to confirm:

```bash
op run --env-file .env.template -- \
  docker compose -f compose.yml -f compose.engines.yml up -d --force-recreate vox
sleep 5
vox-speak --voice demoguy "Same clip, different engine."
# → engine=voxcpm, different-sounding clone of the same source
```

---

## Restore default state

```bash
op run --env-file .env.template -- \
  docker compose -f compose.yml -f compose.engines.yml up -d --force-recreate vox
```

Restores `compose.yml`'s default `VOX_ENGINES` (voxcpm primary, vibevoice secondary, elevenlabs fallback).

---

## Failure-mode matrix

| Step | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | `ready=false` on voxcpm | Model still loading | Wait, retry `mise run health` |
| 2/4 | 500, `synth.failed` in logs | Engine OOM or crashed | `mise run logs:<engine>`; VRAM pressure likely |
| 4 | Engine returns `voxcpm` not `vibevoice` | `VOX_ENGINES` env not applied | `docker exec vox env \| grep VOX_ENGINES` |
| 5b | 400 on upload | Audio format unreadable by `sf.info` | Re-encode via step 5a ffmpeg |
| 5d | 400 `No valid speaker lines` | Speaker-label auto-promote regex missed | Prefix text with `Speaker 1: ` manually |
| 5d | Clone sounds like default, not source | `vibevoice_ref_path` NULL | `psql -c "SELECT name, vibevoice_ref_path FROM voices WHERE name='demoguy'"` |

---

## See also

- [[engine-decoupling|docs/specs/engine-decoupling.md]] full architecture
- `scripts/verify-engine-contract.sh --live` cheaper health-only probe
- `mise run smoke` default-engine codec probe (subset of this runbook)
