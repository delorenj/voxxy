#!/usr/bin/env bash
# Voxxy self-hosted installer.
#
#   ./install.sh            check this machine, write .env, start Voxxy, add the house voices,
#                           install the voxxy command
#   ./install.sh --check    only check this machine; change nothing
#
# Options: --no-cli (skip the voxxy command), --no-start (write .env only), --yes (no prompts).
# Exit codes: 0 everything worked, 2 worked with warnings, 3 something failed.
# Run it again at any time: it keeps your .env and your voices, and skips what is done.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 3

CHECK_ONLY=0 NO_CLI=0 NO_START=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --no-cli) NO_CLI=1 ;;
    --no-start) NO_START=1 ;;
    --yes|-y) ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see ./install.sh --help)" >&2; exit 3 ;;
  esac
done

WARN=0 FAIL=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; WARN=1; }
fail() { printf '  \033[31mfail\033[0m  %s\n' "$*"; FAIL=1; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
finish() {
  if (( FAIL )); then echo; echo "Voxxy is not installed: fix the items marked fail and run ./install.sh again."; exit 3; fi
  if (( WARN )); then exit 2; fi
  exit 0
}

VERSION="$(cat "$HERE/VERSION" 2>/dev/null || echo unknown)"
echo "Voxxy self-hosted $VERSION"

# ---------------------------------------------------------------- 1. this machine
step "Checking this machine"

[[ "$(uname -s)" == "Linux" ]] && ok "Linux" || fail "Voxxy self-hosted runs on Linux only (found $(uname -s))"

if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  else fail "Docker is installed but not reachable: start it, or add yourself to the docker group"; fi
  if docker compose version >/dev/null 2>&1; then ok "Docker Compose $(docker compose version --short 2>/dev/null)"
  else fail "Docker Compose v2 is missing (the 'docker compose' plugin)"; fi
  if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then ok "NVIDIA container toolkit"
  else fail "NVIDIA container toolkit not found in Docker: install nvidia-container-toolkit and run 'sudo nvidia-ctk runtime configure --runtime=docker'"; fi
else
  fail "Docker is not installed"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -n 1 | tr -d ' ')"
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)"
  if [[ -z "$VRAM_MB" ]]; then fail "nvidia-smi found no graphics card"
  elif (( VRAM_MB < 8000 )); then fail "$GPU_NAME has $((VRAM_MB / 1024)) GB of memory; Voxxy needs at least 8 GB"
  elif (( VRAM_MB < 12000 )); then warn "$GPU_NAME has $((VRAM_MB / 1024)) GB: enough, but close the other programs using it"
  else ok "$GPU_NAME, $((VRAM_MB / 1024)) GB"; fi
else
  fail "No NVIDIA driver (nvidia-smi not found)"
fi

FREE_GB="$(df -Pk "$HERE" | awk 'NR==2 {print int($4 / 1048576)}')"
if (( FREE_GB < 20 )); then fail "${FREE_GB} GB free here; Voxxy needs about 20 GB (images and model weights)"
else ok "${FREE_GB} GB free disk"; fi

command -v curl >/dev/null 2>&1 && ok "curl" || fail "curl is not installed"

CLI_TOOL=""
if (( ! NO_CLI )); then
  if command -v uv >/dev/null 2>&1; then CLI_TOOL=uv; ok "uv (installs the voxxy command)"
  elif command -v pipx >/dev/null 2>&1; then CLI_TOOL=pipx; ok "pipx (installs the voxxy command)"
  else warn "Neither uv nor pipx: the voxxy command will not be installed (Voxxy itself still works)"; fi
fi

if (( CHECK_ONLY )); then
  (( FAIL )) && finish
  echo; echo "This machine can run Voxxy."; finish
fi
(( FAIL )) && finish

# ---------------------------------------------------------------- 2. settings
step "Settings"

random_secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

if [[ -f .env ]]; then
  ok ".env exists; keeping it"
else
  umask 077
  sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(random_secret)/" \
      -e "s/^VOX_API_KEY=.*/VOX_API_KEY=$(random_secret)/" env.example > .env
  chmod 600 .env
  ok "wrote .env with a new database password and API key (readable by you only)"
fi

env_value() { grep -E "^$1=" .env | tail -n 1 | cut -d= -f2-; }
PORT="$(env_value VOXXY_PORT)"; PORT="${PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
API_KEY="$(env_value VOX_API_KEY)"
[[ -n "$API_KEY" ]] || { fail ".env has no VOX_API_KEY"; finish; }
mkdir -p data/voices data/audio-cache data/models

if (( NO_START )); then echo; echo "Settings written. Start Voxxy with: docker compose up -d --build"; finish; fi

# ---------------------------------------------------------------- 3. start
step "Starting Voxxy (the first build takes several minutes)"
if VOXXY_VERSION="$VERSION" docker compose up -d --build; then ok "containers started"
else fail "docker compose up failed; see the output above"; finish; fi

printf '  waiting for the service'
for _ in $(seq 1 90); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  printf '.'; sleep 2
done
echo
if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then ok "Voxxy answers at $BASE"
else fail "Voxxy did not answer at $BASE; see: docker compose logs vox"; finish; fi

# ---------------------------------------------------------------- 4. house voices
step "House voices"
while IFS=$'\t' read -r name display tags prompt; do
  [[ -z "$name" || "$name" == \#* ]] && continue
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: $API_KEY" "$BASE/voices/$name")"
  if [[ "$code" == 200 ]]; then ok "$name (already added)"; continue; fi
  if curl -fsS -o /dev/null -H "X-API-Key: $API_KEY" "$BASE/voices" \
       --form-string "name=$name" --form-string "display_name=$display" \
       --form-string "tags=$tags" --form-string "prompt_text=$prompt" \
       -F "audio=@voices/$name.wav"; then ok "$name"
  else warn "could not add $name; run ./install.sh again once 'docker compose logs vox' looks healthy"; fi
done < voices/voices.tsv

# ---------------------------------------------------------------- 5. the voxxy command
if (( ! NO_CLI )) && [[ -n "$CLI_TOOL" ]]; then
  step "The voxxy command"
  WHEEL="$(ls cli/voxxy-*.whl 2>/dev/null | head -n 1)"
  if [[ -z "$WHEEL" ]]; then warn "no CLI wheel in cli/"
  else
    if [[ "$CLI_TOOL" == uv ]]; then uv tool install --force "${WHEEL}[clone]" >/dev/null 2>&1 || uv tool install --force "$WHEEL" >/dev/null
    else pipx install --force "$WHEEL" >/dev/null; fi
    if command -v voxxy >/dev/null 2>&1; then
      if voxxy setup --url "$BASE" --key-ref "file:$HERE/.env#VOX_API_KEY" --no-speak-here >/dev/null 2>&1; then
        ok "voxxy installed and pointed at $BASE"
      else warn "voxxy installed; 'voxxy setup --url $BASE --key-ref file:$HERE/.env#VOX_API_KEY' did not finish"; fi
    else warn "voxxy installed but not on your PATH (uv: run 'uv tool update-shell'; pipx: 'pipx ensurepath')"; fi
  fi
fi

# ---------------------------------------------------------------- 6. engine
step "Speech engine"
if curl -fsS "$BASE/healthz" 2>/dev/null | grep -q '"name":"voxcpm","ready":true'; then ok "VoxCPM2 is ready"
else warn "VoxCPM2 is still loading (the first start downloads about 4.6 GB). Follow it with: docker compose logs -f engine-voxcpm"; fi

echo
echo "Voxxy is installed."
echo "  Web app:  $BASE/  (sign in with the VOX_API_KEY value in $HERE/.env)"
echo "  Try it:   voxxy speak -v house-f1 \"Hello from my own machine.\""
echo "  Stop:     docker compose down     Start: docker compose up -d     (from $HERE)"
finish
