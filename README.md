# vox — universal TTS service

VoxCPM2 wrapped as FastAPI + FastMCP, with a postgres-backed voice profile store,
disk-backed audio URL cache, and ElevenLabs as an automatic fallback engine.
One service, three transports, every delivery surface:

- **OpenClaw / Hermes** via MCP (`vox:speak_url` → Telegram sendVoice, `vox:speak` for inline bytes)
- **Node-RED** via the `node-red-contrib-vox` package
- **Anything else** via HTTP `POST /synthesize` (WAV bytes) or `POST /synthesize-url` (OGG/Opus URL)

## Layout

```
.
├── app/                    FastAPI + FastMCP service
│   ├── main.py             routes + MCP tools + lifespan
│   ├── synth.py            VoxCPM2 model wrapper (the GPU-bound thing)
│   ├── engines.py          pluggable engine protocol + VoxCPM + ElevenLabs
│   ├── audio.py            WAV → OGG/Opus transcode via ffmpeg
│   ├── cache.py            short-lived disk cache for /audio/<id>.ogg
│   └── voices.py           asyncpg repo + Voice dataclass
├── voices/                 WAV blobs (bind-mounted into container)
├── audio-cache/            OGG blobs served at /audio/<id>.ogg (TTL'd, gitignored)
├── node-red-contrib-vox/   custom Node-RED node
├── migrations/             numbered SQL migrations applied against host postgres
├── init.sql                fresh-install schema for host postgres db `vox`
├── Dockerfile              nvidia/cuda base, uv deps, ffmpeg
├── compose.yml             service on `proxy` network, Traefik route
└── .env.example
```

## Prereqs

- Host postgres with db `vox`, user `$DEFAULT_USERNAME` (schema already applied via `init.sql`).
- Docker nvidia runtime (for GPU passthrough). `docker run --rm --gpus all nvidia/cuda:12.4.1-base nvidia-smi` should work.
- Traefik stack running on the `proxy` docker network (standard setup here).
- `VoxCPM2` weights cached in `~/.cache/huggingface` (auto, or pre-download).

## Build + run

Secrets live in 1password (`DeLoSecrets` vault). `mise.toml` wraps every docker
invocation with `op run` so nothing plaintext ever lands on disk:

```bash
cd ~/docker/stacks/utils/vox
mise run up           # build + start, secrets injected at compose time
mise run logs         # tail
mise run health       # /healthz + engine availability
mise run smoke        # end-to-end synthesize → fetch → probe codec
mise tasks            # see everything available
```

If you prefer raw compose (no 1password dependency), populate `.env` from
`.env.example` and run `docker compose up -d --build` directly.

Healthcheck: `curl http://vox.delo.sh/healthz`

## Contract

### HTTP

```bash
# Voice design (no reference) — returns raw WAV bytes
curl -X POST https://vox.delo.sh/synthesize \
  -H 'content-type: application/json' \
  -d '{"text":"Hello world"}' \
  -o /tmp/out.wav

# Synthesize for delivery (Telegram, HA, browser) — returns JSON with an
# OGG/Opus URL that third parties can fetch directly.
curl -X POST https://vox.delo.sh/synthesize-url \
  -H 'content-type: application/json' \
  -d '{"text":"System online","voice":"rick"}'
# → {"audio_url":"https://vox.delo.sh/audio/<uuid>.ogg",
#    "engine":"voxcpm","duration_s":1.9,"bytes":7640,"format":"ogg_opus"}

# List voices
curl https://vox.delo.sh/voices

# Upload a new voice (auto-trimmed to 30s)
curl -X POST https://vox.delo.sh/voices \
  -F name=alice -F display_name="Alice" \
  -F tags="female,english" \
  -F audio=@/path/to/alice.ogg

# Health + engine availability
curl https://vox.delo.sh/healthz
# → {"status":"ok","model_loaded":true,
#    "engines":[{"name":"voxcpm","available":true},
#               {"name":"elevenlabs","available":true}]}
```

### Telegram voice note (via Bot API)

```bash
# 1. Synthesize to a fetchable URL
url=$(curl -fsS -X POST https://vox.delo.sh/synthesize-url \
       -H 'content-type: application/json' \
       -d '{"text":"Deployment finished","voice":"rick"}' \
      | jq -r .audio_url)

# 2. Hand the URL to Telegram; their servers fetch it
curl -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendVoice" \
     -d "chat_id=${TG_CHAT_ID}" -d "voice=${url}"
```

Within OpenClaw agents, use the MCP tool:

```
1. vox:speak_url(text="...", voice?="rick")  →  {audio_url, engine, ...}
2. openclaw message send --channel telegram --target <chat_id>
                         --media <audio_url> --as-voice
```

### MCP (Hermes / OpenClaw)

```bash
# Register the server once. IMPORTANT: trailing slash on the URL.
# Without it, FastAPI 307-redirects to /mcp/ and HTTPX drops the POST body.
hermes mcp add vox --url https://vox.delo.sh/mcp/

# Verify
hermes mcp list
hermes tools list | grep vox

# Agent now has `vox:speak(text, voice)`, `vox:speak_url(text, voice)`,
# and `vox:list_voices_tool()` available.
```

OpenClaw uses the same MCP pattern (exact CLI subcommand may differ):

```bash
openclaw mcp add vox --url https://vox.delo.sh/mcp --transport http
```

### Node-RED

```bash
cd ~/.node-red
npm install /home/delorenj/docker/stacks/utils/vox/node-red-contrib-vox
# Restart Node-RED; drop the "vox tts" node into a flow.
```

## Environment knobs

| Var | Default | Notes |
|-----|---------|-------|
| `VOX_DATABASE_URL`            | required | postgres DSN |
| `VOX_VOICES_DIR`              | `/data/voices` | where voice WAVs live in-container |
| `VOX_AUDIO_CACHE_DIR`         | `/data/audio-cache` | where cached OGG blobs live |
| `VOX_AUDIO_TTL_SECONDS`       | `3600` | audio cache lifetime |
| `VOX_PUBLIC_BASE_URL`         | unset | if set, `speak_url` returns URLs rooted here instead of the FastAPI-computed URL |
| `VOX_REF_AUDIO_MAX_SECONDS`   | `30` | reference audio cap (VRAM guard) |
| `VOX_MAX_LEN`                 | `2048` | max generation token length |
| `VOX_OPTIMIZE`                | `0` | `1` enables torch.compile (more VRAM) |
| `ELEVENLABS_API_KEY`          | unset | enables ElevenLabs fallback engine |
| `ELEVENLABS_DEFAULT_VOICE`    | Adam | voice id used when a voice has no mapping |
| `ELEVENLABS_MODEL_ID`         | `eleven_turbo_v2_5` | ElevenLabs model tier |
| `PYTORCH_CUDA_ALLOC_CONF`     | `expandable_segments:True` | fragmentation guard |

## Known trade-offs

- **HTTP is sync.** A generation blocks the worker for a few seconds.
  For fan-out, wrap in Bloodbank so requests buffer and workers scale.
- **One model in VRAM.** The 2B model permanently holds ~5 GB.
  Don't coexist on the 3090 with ollama loaded unless you set `OLLAMA_KEEP_ALIVE=0`.
- **Audio returned as WAV only.** For bandwidth-sensitive clients, add an
  opus/mp3 codec pass; voxcpm emits 48 kHz which is worth keeping on-wire.

## Troubleshooting

- **`libcudnn` missing**: you rebuilt the image without the nvidia runtime.
  `docker info | grep -i runtime` should show `nvidia` before you `up`.
- **OOM**: check `~/docker/stacks/utils/vox/logs` for `[MEM ...]` lines.
  Oversized reference audio was the usual cause on a shared GPU.
- **MCP tool not visible in Hermes**: `hermes mcp test vox` to probe the endpoint.
- **Telegram rejects the voice URL**: use `speak_url` / `POST /synthesize-url` (OGG/Opus), not `speak` or `POST /synthesize` (WAV). Telegram voice notes require OGG.
- **Fallback engine never engages**: `ELEVENLABS_API_KEY` is unset in the container env; `GET /healthz` will report `elevenlabs: {available: false}`.
