# voxxy-engine-vibevoice

GPU-bound sidecar that wraps [`microsoft/VibeVoice-1.5B`](https://huggingface.co/microsoft/VibeVoice-1.5B)
behind the engine RPC contract (`POST /v1/synthesize`, `GET /healthz` — shape defined in
`engine/contract.py`, a vendor-copy of `app/engine_contract.py`). This is the community
fork of the upstream model; we use it because its zero-shot voice cloning quality on
short reference clips is stronger than VoxCPM2 for expressive English speech, and because
its bf16 memory footprint fits comfortably alongside an LLM on a 24 GB card.

## Watermark disclosure

Every sample produced by this engine carries both an **audible** and an **imperceptible**
watermark baked into the model weights. This is a responsible-AI measure from the upstream
authors and cannot be disabled at inference time. See the
[VibeVoice paper (arxiv:2508.19205)](https://arxiv.org/abs/2508.19205) for details. Surface
this fact to downstream users if your product routes VibeVoice output to humans who might
mistake it for unwatermarked audio.

## Resource footprint

- **VRAM:** ~7.5 GB in bf16 (model + processor + acoustic decoder).
- **Image size:** ~15 GB (CUDA base + transformers + torch cu124).
- **Cold start:** ~20 s to first-ready on a warm HF cache; +30–60 s if weights must download.

## Environment knobs (engine-side)

| Var | Default | Notes |
|-----|---------|-------|
| `VOX_VIBEVOICE_ATTN` | `sdpa` | Attention impl. `sdpa` is PyTorch-native, works out of the box. Switch to `flash_attention_2` only after rebuilding the image with `flash-attn` pinned. |
| `VOX_REF_AUDIO_MAX_SECONDS` | `10` | Reference clip auto-trim. Quality degrades past ~10 s (trained on short clips). Lower is safe; raise only if you know what you're doing. |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` | Fragmentation guard on shared GPUs. |
| `HF_HOME` | `/cache/huggingface` | Bind-mounted from the host so weights survive rebuilds. |

## Input handling

- **Plain text** — the engine wrapper auto-promotes to `Speaker 1: <text>` before handing
  it to the transformers processor, which rejects unlabeled input. Callers don't need to
  pre-format anything.
- **Labeled multi-speaker** — if the incoming `text` already starts with a `Speaker N:`
  label, it's passed through untouched. (v1 still treats this as single-voice synthesis;
  true multi-speaker dialog is future work.)
- **Reference audio** — received inline as base64 WAV in the `/v1/synthesize` body,
  decoded to a tempfile, resampled to 24 kHz mono via `librosa` inside the engine, trimmed
  to `VOX_REF_AUDIO_MAX_SECONDS`. Temp files are cleaned up in a finally block.

## Known limits

- Reference clip quality degrades above ~10 s. The auto-trim is not a suggestion.
- `flash_attn` is not bundled. Enabling `flash_attention_2` requires rebuilding the image
  with the package pinned in `pyproject.toml` and a CUDA toolkit present at build time.
- `prompt_text` (voxcpm's Ultimate Cloning hint) is ignored; VibeVoice does not use
  transcript-guided cloning.
- No streaming in v1; full-utterance generation only. VibeVoice-Realtime is a future ADR.
- The embedded watermark cannot be removed. Do not attempt to.

## Development

```bash
cd engines/vibevoice
uv sync --frozen
uv run uvicorn engine.main:app --port 18003 --reload
# healthcheck once model is loaded:
curl -fsS http://localhost:18003/healthz | jq .
```

Build the container directly: `docker build -t voxxy-engine-vibevoice engines/vibevoice`.
Or via mise: `mise run build:vibevoice`.
