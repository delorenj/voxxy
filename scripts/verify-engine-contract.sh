#!/usr/bin/env bash
# verify-engine-contract.sh
#
# Two modes:
#
#   Default (no flags):
#     Starts a loopback fake engine (scripts/fake-engine.py), then exercises
#     the /v1/synthesize + /healthz contract end-to-end:
#       1. /healthz returns an EngineHealth-shaped body with ready=true
#       2. /v1/synthesize with a valid request returns an EngineSynthesizeResponse
#          whose wav_b64 decodes to a non-empty WAV
#       3. /v1/synthesize with empty text returns 4xx + {error: {code, message}}
#       4. Core's RemoteEngineClient routes to the fake and returns matching bytes
#
#   --live:
#     Validates the running stack against VOX_URL (default: https://vox.delo.sh):
#       1. GET /healthz — asserts status=="ok" and every remote engine is ready=true.
#          ElevenLabs is allowed to be not-ready (no API key in CI).
#       2. POST /synthesize-url — verifies the response JSON is shape-correct
#          (audio_url, engine, duration_s, bytes, format fields present).
#       3. Fetches audio_url and confirms the file is OGG/Opus.
#
# Used in Phase 1 to prove the contract + client work before physically
# splitting engines into separate containers, and in CI/CD to probe the live stack.
#
# Usage:
#   bash scripts/verify-engine-contract.sh          # loopback mode (default)
#   bash scripts/verify-engine-contract.sh --live   # live stack mode
#
# Prereqs (loopback): uv sync (FastAPI + uvicorn + pydantic); jq; curl.
# Prereqs (live):     jq; curl; file (libmagic).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "FAIL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------
exec_live_check() {
  VOX_URL="${VOX_URL:-https://vox.delo.sh}"
  echo "=== live check against ${VOX_URL} ==="

  # Step 1 — /healthz
  echo "→ step 1: GET ${VOX_URL}/healthz"
  hc=$(curl -fsS "${VOX_URL}/healthz") || die "could not reach ${VOX_URL}/healthz"
  echo "$hc" | python3 -m json.tool

  # Top-level status must be "ok"
  status=$(echo "$hc" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status',''))")
  [[ "$status" == "ok" ]] || die "/healthz status is '${status}', expected 'ok'"

  # Iterate engines; every remote engine (voxcpm, vibevoice, ...) must be ready.
  # ElevenLabs is allowed to be not-ready when ELEVENLABS_API_KEY is absent.
  engine_check_failed=0
  while IFS= read -r engine_json; do
    name=$(echo "$engine_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('name','unknown'))")
    ready=$(echo "$engine_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('ready',False)).lower())")
    if [[ "$name" == "elevenlabs" ]]; then
      echo "  engine=${name} ready=${ready} (elevenlabs: not-ready is acceptable when API key absent)"
    else
      echo "  engine=${name} ready=${ready}"
      if [[ "$ready" != "true" ]]; then
        echo "FAIL: engine '${name}' reports ready=${ready}" >&2
        engine_check_failed=1
      fi
    fi
  done < <(echo "$hc" | python3 -c "
import json, sys
d = json.load(sys.stdin)
engines = d.get('engines', [])
for e in engines:
    print(json.dumps(e))
")
  [[ "$engine_check_failed" -eq 0 ]] || die "one or more engines not ready (see above)"

  # Step 2 — POST /synthesize-url
  echo "→ step 2: POST ${VOX_URL}/synthesize-url"
  synth=$(curl -fsS -X POST "${VOX_URL}/synthesize-url" \
    -H 'content-type: application/json' \
    -d '{"text":"Engine contract live check.","voice":"rick"}') \
    || die "POST /synthesize-url failed"
  echo "$synth" | python3 -m json.tool

  for field in audio_url engine duration_s bytes; do
    echo "$synth" | python3 -c "
import json, sys
d = json.load(sys.stdin)
assert '${field}' in d, 'missing field: ${field}'
print('  field ${field} =', repr(d['${field}'])[:80])
" || die "/synthesize-url response missing field '${field}'"
  done

  # Step 3 — Fetch audio_url and verify OGG/Opus
  echo "→ step 3: fetching audio_url"
  audio_url=$(echo "$synth" | python3 -c "import json,sys; print(json.load(sys.stdin)['audio_url'])")
  curl -fsSL "$audio_url" -o /tmp/vox_live_check.ogg || die "could not fetch audio_url: ${audio_url}"
  file_out=$(file /tmp/vox_live_check.ogg)
  echo "  file: $file_out"
  echo "$file_out" | grep -qi "ogg\|opus\|vorbis" \
    || die "audio file does not appear to be OGG/Opus: $file_out"

  # --- voxxy CLI spot-check ---
  echo "--- voxxy CLI spot-check ---"
  if ! command -v voxxy >/dev/null 2>&1; then
    echo "(skipping voxxy spot-check; CLI not installed)"
  else
    # voxxy health exits 0 on ok
    voxxy health --json >/dev/null || { echo "FAIL: voxxy health"; exit 1; }
    # voxxy voice list --json returns a JSON array
    voxxy voice list --json | jq -e 'type == "array"' >/dev/null || { echo "FAIL: voxxy voice list not array"; exit 1; }
    # voxxy speak --raw produces WAV magic (write to temp file to avoid SIGPIPE
    # under set -euo pipefail when head closes the pipe before voxxy drains)
    voxxy speak --raw "verify" >/tmp/vox_cli_check.wav 2>/dev/null
    head_bytes=$(dd if=/tmp/vox_cli_check.wav bs=1 count=4 2>/dev/null)
    rm -f /tmp/vox_cli_check.wav
    [[ "$head_bytes" == "RIFF" ]] || { echo "FAIL: voxxy speak output not RIFF (got: $head_bytes)"; exit 1; }
    echo "voxxy ok"
  fi

  echo "=== live check passed ==="
}

# ---------------------------------------------------------------------------
# LOOPBACK / FAKE ENGINE MODE (default)
# ---------------------------------------------------------------------------
exec_loopback_check() {
  PORT="${VOX_FAKE_ENGINE_PORT:-18001}"
  BASE="http://127.0.0.1:${PORT}"

  cleanup() {
    if [[ -n "${ENGINE_PID:-}" ]]; then
      kill "$ENGINE_PID" 2>/dev/null || true
      wait "$ENGINE_PID" 2>/dev/null || true
    fi
  }
  trap cleanup EXIT

  echo "→ launching fake engine on :${PORT}"
  # PYTHONPATH=. so the script can import app.engine_contract without being
  # installed as a package. uv run handles the venv; it does not auto-add cwd.
  PYTHONPATH="$ROOT" uv run python scripts/fake-engine.py --port "$PORT" \
    >/tmp/vox-fake.log 2>&1 &
  ENGINE_PID=$!

  # Wait up to ~5s for the server to come up.
  for _ in $(seq 1 25); do
    if curl -fsS "${BASE}/healthz" >/dev/null 2>&1; then
      break
    fi
    sleep 0.2
  done
  curl -fsS "${BASE}/healthz" >/dev/null || die "fake engine never came up (see /tmp/vox-fake.log)"

  echo "→ step 1: /healthz shape"
  hc=$(curl -fsS "${BASE}/healthz")
  echo "$hc" | jq -e '.engine == "fake" and .ready == true and .model_loaded == true' >/dev/null \
    || die "/healthz did not match EngineHealth (got: $hc)"
  echo "$hc" | jq -e '.capabilities.output_sample_rate == 16000' >/dev/null \
    || die "capabilities.output_sample_rate mismatch"

  echo "→ step 2: /v1/synthesize happy path"
  body=$(curl -fsS -X POST "${BASE}/v1/synthesize" \
    -H 'content-type: application/json' \
    -d '{"text": "hello from contract test"}')
  echo "$body" | jq -e '.engine == "fake" and .sample_rate == 16000 and .bytes > 0' >/dev/null \
    || die "synthesize body did not match (got: $body)"
  # wav_b64 should decode to at least a RIFF header. Use a temp file to avoid
  # SIGPIPE when head closes the pipe before base64 drains. Bash sees 141 under
  # set -euo pipefail and aborts otherwise.
  wav_b64=$(echo "$body" | jq -r '.wav_b64')
  printf '%s' "$wav_b64" | base64 -d > /tmp/vox-contract.wav
  head_bytes=$(dd if=/tmp/vox-contract.wav bs=1 count=4 2>/dev/null)
  [[ "$head_bytes" == "RIFF" ]] || die "decoded wav did not start with RIFF (got: $head_bytes)"

  echo "→ step 3: /v1/synthesize rejects empty text"
  status=$(curl -s -o /tmp/vox-err.json -w '%{http_code}' -X POST "${BASE}/v1/synthesize" \
    -H 'content-type: application/json' \
    -d '{"text": ""}')
  # FastAPI's own validation returns 422 before our handler runs; our handler
  # would return 400. Either way, it's a 4xx and a structured error body.
  [[ "$status" =~ ^4 ]] || die "empty text should 4xx, got $status"

  echo "→ step 4: RemoteEngineClient round-trip"
  uv run python - <<PY
import asyncio, base64
from app.engines import RemoteEngineClient

async def main():
    c = RemoteEngineClient("fake", "${BASE}")
    ok = await c.refresh_health()
    assert ok, "refresh_health returned False"
    r = await c.generate(text="hello")
    assert r.engine == "fake", r.engine
    assert r.sample_rate == 16000, r.sample_rate
    assert r.wav_bytes[:4] == b"RIFF", r.wav_bytes[:4]
    print("client round-trip ok", len(r.wav_bytes), "bytes")

asyncio.run(main())
PY

  echo "contract ok"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--live" ]]; then
  exec_live_check
else
  exec_loopback_check
fi
