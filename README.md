# vox — universal TTS service

VoxCPM2 wrapped as FastAPI + FastMCP, with a postgres-backed voice profile store.
One service, three consumer integrations:

- **OpenClaw / Hermes** via MCP (tool name: `vox:speak`)
- **Node-RED** via the `node-red-contrib-vox` package
- **Anything else** via HTTP `POST /synthesize`

## Layout

```
.
├── app/                    FastAPI + FastMCP service
├── voices/                 WAV blobs (bind-mounted into container)
├── node-red-contrib-vox/   custom Node-RED node
├── init.sql                schema for host postgres db `vox`
├── Dockerfile              nvidia/cuda base, uv deps
├── compose.yml             service on `proxy` network, Traefik route
└── .env.example
```

## Prereqs

- Host postgres with db `vox`, user `$DEFAULT_USERNAME` (schema already applied via `init.sql`).
- Docker nvidia runtime (for GPU passthrough). `docker run --rm --gpus all nvidia/cuda:12.4.1-base nvidia-smi` should work.
- Traefik stack running on the `proxy` docker network (standard setup here).
- `VoxCPM2` weights cached in `~/.cache/huggingface` (auto, or pre-download).

## Build + run

```bash
cd ~/docker/stacks/utils/vox
docker compose up -d --build
docker logs -f vox
```

Healthcheck: `curl http://vox.delo.sh/healthz`

## Contract

### HTTP

```bash
# Voice design (no reference)
curl -X POST https://vox.delo.sh/synthesize \
  -H 'content-type: application/json' \
  -d '{"text":"Hello world"}' \
  -o /tmp/out.wav

# Clone with the seeded rick voice
curl -X POST https://vox.delo.sh/synthesize \
  -H 'content-type: application/json' \
  -d '{"text":"Morty, we gotta go","voice":"rick","cfg":2.0,"steps":10}' \
  -o /tmp/rick.wav

# List voices
curl https://vox.delo.sh/voices

# Upload a new voice (auto-trimmed to 30s)
curl -X POST https://vox.delo.sh/voices \
  -F name=alice -F display_name="Alice" \
  -F tags="female,english" \
  -F audio=@/path/to/alice.ogg
```

### MCP (Hermes / OpenClaw)

```bash
# Register the server once. IMPORTANT: trailing slash on the URL.
# Without it, FastAPI 307-redirects to /mcp/ and HTTPX drops the POST body.
hermes mcp add vox --url https://vox.delo.sh/mcp/

# Verify
hermes mcp list
hermes tools list | grep vox

# Agent now has `vox:speak(text, voice)` available.
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
| `VOX_VOICES_DIR`              | `/data/voices` | where WAVs live in-container |
| `VOX_REF_AUDIO_MAX_SECONDS`   | `30` | reference audio cap (VRAM guard) |
| `VOX_MAX_LEN`                 | `2048` | max generation token length |
| `VOX_OPTIMIZE`                | `0` | `1` enables torch.compile (more VRAM) |
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
