# voxxy-engine-voxcpm

GPU-bound sidecar that wraps [`openbmb/VoxCPM2`](https://huggingface.co/openbmb/VoxCPM2)
behind the engine RPC contract (`POST /v1/synthesize`, `GET /healthz` — shape defined in
`engine/contract.py`, a vendor-copy of `app/engine_contract.py`). VoxCPM2 is a 2B-parameter
end-to-end neural TTS model with zero-shot voice cloning from a short reference clip; it's
our default primary engine because it's fast (~1-2 s RTF on a 3090), memory-frugal, and
handles longer reference clips (up to 30 s) without quality regression.

## Resource footprint

- **VRAM:** ~5 GB in bf16 (persistent; the model is not evictable).
- **Image size:** ~16 GB (CUDA base + voxcpm + torch cu124).
- **Cold start:** ~10 s to first-ready on a warm HF cache; +60–90 s if the 4.58 GB
  weights must download.

## Environment knobs (engine-side)

| Var | Default | Notes |
|-----|---------|-------|
| `VOX_REF_AUDIO_MAX_SECONDS` | `30` | Reference clip auto-trim (VRAM guard). Oversized reference audio is the #1 cause of OOM here. |
| `VOX_OPTIMIZE` | `0` | `1` enables `torch.compile` for faster RTF at the cost of higher peak VRAM and a warm-up. Enable only on a GPU dedicated to this engine. |
| `VOX_MAX_LEN` | `2048` | Max generation token length. Raise for very long utterances; expect more VRAM. |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` | Fragmentation guard on shared GPUs. |
| `HF_HOME` | `/cache/huggingface` | Bind-mounted from the host so weights survive rebuilds. |

## Reference clip handling

- Received inline as base64 WAV in the `/v1/synthesize` body, decoded to a tempfile.
- Auto-trimmed to `VOX_REF_AUDIO_MAX_SECONDS` (default 30 s) as a VRAM safety net — core
  usually trims on upload, but scp'd voices or bypass paths get caught here.
- Stereo inputs are downmixed to mono.
- No resampling at this layer; voxcpm tolerates any SR the upstream processor accepts.
- Temp files are cleaned up in a finally block on every request.

## Input handling

- **Plain text** — generated via the standard voxcpm `generate(text, ...)` path.
- **`prompt_text` (optional)** — if supplied and the voice row has both `prompt_text` and
  `prompt_wav_path`, generation switches to "Ultimate Cloning" mode, which uses an
  accurate transcript of the reference audio for better prosody matching. Only set this
  when you actually have a matching transcript; wrong transcripts degrade output.

## Known limits

- Single-speaker only. Multi-speaker dialog is not supported.
- No streaming in v1; full-utterance generation only.
- `torch.compile` (`VOX_OPTIMIZE=1`) conflicts with fast iteration: the first call after
  a code reload takes ~30 s while the graph recompiles.
- VRAM contention with ollama on the same card: voxcpm is not evictable. Set
  `OLLAMA_KEEP_ALIVE=0` on the ollama side if you need coexistence.

## Development

```bash
cd engines/voxcpm
uv sync --frozen
uv run uvicorn engine.main:app --port 18002 --reload
# healthcheck once model is loaded:
curl -fsS http://localhost:18002/healthz | jq .
```

Build the container directly: `docker build -t voxxy-engine-voxcpm engines/voxcpm`.
Or via mise: `mise run build:voxcpm`.
