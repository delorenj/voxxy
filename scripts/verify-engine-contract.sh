#!/usr/bin/env bash
# verify-engine-contract.sh
#
# Starts a loopback fake engine (scripts/fake-engine.py), then exercises the
# /v1/synthesize + /healthz contract end-to-end:
#   1. /healthz returns an EngineHealth-shaped body with ready=true
#   2. /v1/synthesize with a valid request returns an EngineSynthesizeResponse
#      whose wav_b64 decodes to a non-empty WAV
#   3. /v1/synthesize with empty text returns 4xx + {error: {code, message}}
#   4. Core's RemoteEngineClient routes to the fake and returns matching bytes
#
# Used in Phase 1 to prove the contract + client work before physically
# splitting engines into separate containers.
#
# Usage: bash scripts/verify-engine-contract.sh
# Prereqs: uv sync (FastAPI + uvicorn + pydantic); jq; curl.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${VOX_FAKE_ENGINE_PORT:-18001}"
BASE="http://127.0.0.1:${PORT}"

die() { echo "FAIL: $*" >&2; exit 1; }

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
