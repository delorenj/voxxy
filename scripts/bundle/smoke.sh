#!/usr/bin/env bash
# Install a built self-hosted bundle for real, on this machine, with a stand-in engine.
#
#   scripts/bundle/smoke.sh [dist/bundle/voxxy-selfhost-<version>.tar.gz]
#
# Unpacks the tarball into a scratch folder, runs its install.sh exactly as a buyer
# would (but with --no-cli, so the voxxy command on this machine is left alone), and
# swaps the GPU engine for the contract's fake engine so the check needs no graphics
# memory and no 4.6 GB download. Then it checks what a buyer gets:
#   the database schema loads, the four house voices are added, a synthesis request
#   with the generated API key returns audio, a request without it is refused, and the
#   bundled CLI (in its own venv) finds the server through `file:.env#VOX_API_KEY`.
# Finally it removes the scratch stack (its own containers and volumes only).
#
# Exit 0 when every step passes, 3 otherwise. KEEP=1 leaves the stack running.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARBALL="${1:-$(ls -t "$REPO"/dist/bundle/*.tar.gz 2>/dev/null | head -n 1)}"
[[ -f "$TARBALL" ]] || { echo "no bundle tarball; run: mise run bundle:build" >&2; exit 3; }

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/voxxy/bundle-smoke"
PROJECT="voxxy-bundle-smoke"
PORT="${SMOKE_PORT:-18766}"
WORK="$STATE/run"
FAIL=0
pass() { printf '  \033[32mpass\033[0m  %s\n' "$*"; }
miss() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=1; }

# The containers write data/ as root; clear it from inside a container before removing the folder.
clear_data() {
  [[ -d "$1/data" ]] && docker run --rm -v "$1/data:/d" voxxy-bundle-smoke-engine:local \
    sh -c 'rm -rf /d/* /d/.[!.]*' >/dev/null 2>&1
  return 0
}
if [[ -d "$WORK" ]]; then
  for d in "$WORK"/*/; do
    [[ -f "$d/compose.yml" ]] || continue
    (cd "$d" && COMPOSE_PROJECT_NAME=$PROJECT COMPOSE_FILE="compose.yml:smoke/compose.override.yml" \
      docker compose down -v --remove-orphans >/dev/null 2>&1)
    clear_data "${d%/}"
  done
  rm -rf "$WORK"
fi
mkdir -p "$WORK"
tar -xzf "$TARBALL" -C "$WORK"
BUNDLE="$(ls -d "$WORK"/*/ | head -n 1)"; BUNDLE="${BUNDLE%/}"
cd "$BUNDLE" || exit 3
echo "bundle: $BUNDLE"

# The stand-in engine: the contract's fake engine, run from the core image.
mkdir -p smoke
cp "$REPO/scripts/fake-engine.py" smoke/fake_engine.py
cat > smoke/compose.override.yml <<'EOF'
services:
  engine-voxcpm:
    build: ./core
    image: voxxy-bundle-smoke-engine:local
    working_dir: /app
    environment:
      PYTHONPATH: /app
    command: ["uvicorn", "--app-dir", "/smoke", "fake_engine:app", "--host", "0.0.0.0", "--port", "8000"]
    volumes: !override
      - ./smoke/fake_engine.py:/smoke/fake_engine.py:ro
    deploy: !reset {}
EOF
export COMPOSE_PROJECT_NAME="$PROJECT" COMPOSE_FILE="compose.yml:smoke/compose.override.yml"

echo "install.sh --no-start"
./install.sh --no-start --no-cli >"$STATE/install-1.log" 2>&1; rc=$?
(( rc == 0 || rc == 2 )) && pass "settings written (exit $rc)" || miss "install.sh --no-start exited $rc ($STATE/install-1.log)"
[[ "$(stat -c %a .env)" == 600 ]] && pass ".env is mode 600" || miss ".env mode is $(stat -c %a .env)"
sed -i "s/^VOXXY_PORT=.*/VOXXY_PORT=$PORT/; s|^VOX_PUBLIC_BASE_URL=.*|VOX_PUBLIC_BASE_URL=http://127.0.0.1:$PORT|" .env

echo "install.sh (full, no CLI)"
./install.sh --no-cli >"$STATE/install-2.log" 2>&1; rc=$?
(( rc == 0 || rc == 2 )) && pass "install.sh finished (exit $rc; 2 = the stand-in engine is not voxcpm)" \
  || miss "install.sh exited $rc ($STATE/install-2.log)"

BASE="http://127.0.0.1:$PORT"
KEY="$(grep -E '^VOX_API_KEY=' .env | cut -d= -f2-)"
voices="$(curl -fsS -H "X-API-Key: $KEY" "$BASE/voices" 2>/dev/null)"
names="$(printf '%s' "$voices" | python3 -c 'import json,sys; print(",".join(sorted(v["name"] for v in json.load(sys.stdin))))' 2>/dev/null)"
[[ "$names" == "house-f1,house-f2,house-m1,house-m2" ]] && pass "house voices listed: $names" || miss "voices: '$names'"
tagged="$(printf '%s' "$voices" | python3 -c 'import json,sys; print(all("house" in v["tags"] and v.get("prompt_text") for v in json.load(sys.stdin)))' 2>/dev/null)"
[[ "$tagged" == True ]] && pass "each voice carries the house tag and its transcript" || miss "voice tags or prompt_text missing"

code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/voices")"
[[ "$code" == 401 ]] && pass "no key: 401" || miss "no key answered $code"
code="$(curl -s -o "$STATE/synth.wav" -w '%{http_code}' -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"text":"Hello from the bundle.","voice":"house-f1"}' "$BASE/synthesize")"
[[ "$code" == 200 ]] && head -c 4 "$STATE/synth.wav" | grep -q RIFF && pass "synthesis with house-f1 returns a WAV" \
  || miss "synthesis answered $code"
edition="$(curl -fsS "$BASE/app-config" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("edition"))' 2>/dev/null)"
[[ "$edition" == self ]] && pass "edition: self" || miss "edition: $edition"

echo "bundled CLI in its own venv"
VENV="$STATE/cli-venv"; rm -rf "$VENV"
uv venv -q --python 3.12 "$VENV" && uv pip install -q --python "$VENV/bin/python" cli/*.whl
CLI_HOME="$STATE/cli-home"; rm -rf "$CLI_HOME"; mkdir -p "$CLI_HOME"
run_cli() { env -u VOX_API_KEY -u VOX_API_KEY_REF -u VOX_URL HOME="$CLI_HOME" XDG_CONFIG_HOME="$CLI_HOME/.config" "$VENV/bin/voxxy" "$@"; }
if run_cli setup --url "$BASE" --key-ref "file:$BUNDLE/.env#VOX_API_KEY" --no-speak-here --json >"$STATE/cli-setup.json" 2>&1; then
  pass "voxxy setup accepted the file: key reference"
else miss "voxxy setup failed ($STATE/cli-setup.json)"; fi
run_cli voice list --json 2>/dev/null | grep -q '"house-f1"' && pass "voxxy voice list sees house-f1" || miss "voxxy voice list"

if [[ "${KEEP:-0}" == 1 ]]; then
  echo "KEEP=1: stack left running at $BASE (cd $BUNDLE; COMPOSE_PROJECT_NAME=$PROJECT COMPOSE_FILE=$COMPOSE_FILE docker compose down -v)"
else
  docker compose down -v --remove-orphans >/dev/null 2>&1 && pass "scratch stack removed"
  clear_data "$BUNDLE"
  rm -rf "$VENV" "$CLI_HOME" "$WORK"
fi
(( FAIL )) && { echo "smoke: FAILED"; exit 3; }
echo "smoke: passed"
