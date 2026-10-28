# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy<2.3", "speechbrain", "torch==2.5.1", "torchaudio==2.5.1", "setuptools<81"]
# [tool.uv]
# index-url = "https://download.pytorch.org/whl/cpu"
# extra-index-url = ["https://pypi.org/simple"]
# index-strategy = "unsafe-best-match"
# ///
"""ECAPA speaker embeddings for a source recording. NOT imported by voxxy: run by ``voxxy.speakergate``.

    uv run --script ecapa_embed.py SOURCE OUT.npz [--win 2.0] [--hop 1.0]

Writes ``starts`` (window start seconds), ``emb`` (L2-normalised, one 192-d row per window),
``rms`` (window loudness, so callers can ignore silence) and ``win``. It is a separate PEP 723
script, like the vox-tts scoring harness, so the CLI itself never depends on torch (a CPU torch
wheel is ~200 MB and `voxxy health` must stay light); uv builds this environment once and
caches it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import numpy as np

SR = 16000


def decode(path: str) -> np.ndarray:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("out")
    ap.add_argument("--win", type=float, default=2.0)
    ap.add_argument("--hop", type=float, default=1.0)
    args = ap.parse_args()

    import torch
    from speechbrain.inference.speaker import EncoderClassifier

    torch.set_num_threads(int(os.environ.get("VOXXY_ECAPA_THREADS") or min(8, os.cpu_count() or 4)))
    y = decode(args.source)
    n_win = int(args.win * SR)
    total = len(y) / SR
    starts = np.arange(0.0, max(total - args.win, 0.0) + 1e-9, args.hop)
    segs = [y[int(s * SR):int(s * SR) + n_win] for s in starts]
    rms = np.array([float(np.sqrt(np.mean(s ** 2))) if len(s) else 0.0 for s in segs])
    segs = [np.pad(s, (0, n_win - len(s))) for s in segs]
    ecapa = EncoderClassifier.from_hparams(
        "speechbrain/spkrec-ecapa-voxceleb",
        savedir=os.path.expanduser("~/.cache/voxxy/ecapa"),
        run_opts={"device": "cpu"},
    )
    out = []
    for i in range(0, len(segs), 32):
        batch = torch.tensor(np.stack(segs[i:i + 32]))
        with torch.no_grad():
            out.append(ecapa.encode_batch(batch).squeeze(1).numpy())
    emb = np.concatenate(out) if out else np.zeros((0, 192), dtype=np.float32)
    if len(emb):
        emb = emb / np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9)
    np.savez(args.out, starts=starts, emb=emb, rms=rms, win=args.win)
    print(f"{len(starts)} windows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
