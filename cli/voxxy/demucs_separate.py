# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["demucs==4.0.1", "torch==2.8.0", "torchaudio==2.8.0", "soundfile", "numpy<2.3"]
# ///
"""Demucs vocal-stem separator for a source recording. NOT imported by voxxy: run by ``voxxy.vocals``.

    uv run --script demucs_separate.py SOURCE OUT.wav [--model htdemucs] [--device auto|cuda|cpu] [--shifts 1]

Decodes ``SOURCE`` via ffmpeg at the Demucs model's sample rate and channel count, runs
``demucs.apply.apply_model`` on GPU when available (automatically falling back to CPU on
CUDA OOM), extracts the ``vocals`` stem, prevents clipping, and writes ``OUT.wav`` via
``soundfile`` (bypassing torchaudio's save backend). Kept as a standalone PEP 723 script,
like ``ecapa_embed.py``, so ``voxxy`` itself stays light and uv caches the torch/demucs
environment once.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import numpy as np


def decode_stereo(path: str, samplerate: int, channels: int) -> np.ndarray:
    proc = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-i", path,
            "-vn", "-ac", str(channels), "-ar", str(samplerate), "-f", "f32le", "-",
        ],
        capture_output=True,
        check=True,
    )
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    if channels > 1:
        frames = len(raw) // channels
        return raw[: frames * channels].reshape(frames, channels).T.copy()
    return raw[None, :].copy()


def pick_device(requested: str, torch_mod) -> str:
    if requested in ("cpu", "cuda"):
        if requested == "cuda" and not torch_mod.cuda.is_available():
            return "cpu"
        return requested
    return "cuda" if torch_mod.cuda.is_available() else "cpu"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("out")
    ap.add_argument("--model", default="htdemucs")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--shifts", type=int, default=1)
    args = ap.parse_args()

    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    threads = int(os.environ.get("VOXXY_DEMUCS_THREADS") or min(8, os.cpu_count() or 4))
    torch.set_num_threads(threads)

    model = get_model(args.model)
    model.cpu()
    model.eval()

    if "vocals" not in model.sources:
        print(f"model '{args.model}' has no 'vocals' stem (sources: {model.sources})", file=sys.stderr)
        return 2

    wav_np = decode_stereo(args.source, model.samplerate, model.audio_channels)
    if wav_np.shape[1] == 0:
        print(f"decoded 0 samples from {args.source}", file=sys.stderr)
        return 1

    wav = torch.from_numpy(wav_np)
    ref = wav.mean(0)
    ref_mean = ref.mean()
    ref_std = ref.std() + 1e-8
    wav = (wav - ref_mean) / ref_std

    device = pick_device(args.device, torch)
    workers = 0 if device == "cuda" else min(4, threads)
    try:
        with torch.no_grad():
            sources = apply_model(
                model, wav[None], device=device, shifts=args.shifts,
                split=True, overlap=0.25, progress=False, num_workers=workers,
            )[0]
    except RuntimeError as exc:
        if device == "cuda" and "out of memory" in str(exc).lower():
            torch.cuda.empty_cache()
            device = "cpu"
            with torch.no_grad():
                sources = apply_model(
                    model, wav[None], device="cpu", shifts=args.shifts,
                    split=True, overlap=0.25, progress=False, num_workers=min(4, threads),
                )[0]
        else:
            raise

    vocals = sources[model.sources.index("vocals")].cpu()
    vocals = vocals * ref_std + ref_mean
    # Rescale if peak exceeds 1.0 to avoid clipping while preserving dynamics.
    peak = float(vocals.abs().max())
    if peak > 1.0:
        vocals = vocals / max(1.01 * peak, 1.0)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), vocals.numpy().T, model.samplerate, subtype="PCM_16")
    dur = vocals.shape[-1] / model.samplerate
    print(f"isolated vocals ({dur:.1f}s, model={args.model}, device={device}) -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
