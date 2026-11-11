#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["faster-whisper", "numpy", "scipy", "soundfile"]
# ///
"""Check that each registered house voice speaks English and Spanish through the live service.

    uv run house-voices/evaluate.py [--url https://vox.delo.sh] [--model small]

For every house voice and language it synthesizes a clinical test sentence with
`{"voice": "house-xx", "language": ..., "engines": ["voxcpm"]}`, checks the answer is
`X-Vox-Voice-Source: registered` from voxcpm, transcribes it with faster-whisper on
the CPU (the GPU is shared with the voice engine), and reports word error rate,
Whisper's detected language and the median pitch. Exit 1 if a voice fails.

Pass marks: detected language equals the requested one, word error rate <= 0.15,
and median pitch in the voice's gender band. The recipe's own selection also used
a TitaNet speaker-embedding check (same voice across languages, distinct between
voices); see README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = Path(__file__).resolve().parent
TEXTS = {
    "en": "Your appointment is confirmed for Thursday afternoon. Please bring your insurance card and a list of your current medications.",
    "es": "Su cita está confirmada para el jueves por la tarde. Por favor traiga su tarjeta del seguro y una lista de sus medicamentos actuales.",
}
F0_BAND = {"female": (160.0, 320.0), "male": (85.0, 185.0)}
MAX_WER = 0.15
SEED = 1234


def post_json(url: str, path: str, body: dict) -> tuple[int, dict, bytes]:
    headers = {"content-type": "application/json"}
    if os.environ.get("VOX_API_KEY"):
        headers["X-API-Key"] = os.environ["VOX_API_KEY"]
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text).split()


def wer(reference: str, hypothesis: str) -> float:
    r, h = words(reference), words(hypothesis)
    row = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, row[0] = row[0], i
        for j in range(1, len(h) + 1):
            cur = min(row[j] + 1, row[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, row[j] = row[j], cur
    return row[len(h)] / max(1, len(r))


def median_f0(samples: np.ndarray, rate: int) -> float:
    """Median of a plain autocorrelation pitch track over voiced 40 ms frames."""
    step, win = int(rate * 0.02), int(rate * 0.04)
    lo, hi = int(rate / 400), int(rate / 70)
    values = []
    for i in range(0, len(samples) - win, step):
        frame = samples[i:i + win]
        if np.sqrt((frame ** 2).mean()) < 0.02:
            continue
        frame = frame - frame.mean()
        ac = np.correlate(frame, frame, "full")[win - 1:]
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] / ac[0] > 0.5:
            values.append(rate / k)
    return float(np.median(values)) if values else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("VOX_URL", "https://vox.delo.sh"))
    ap.add_argument("--model", default="small", help="faster-whisper model size (default: small)")
    args = ap.parse_args()

    import tomllib

    voices = tomllib.loads((HERE / "voices.toml").read_text())["voice"]

    from faster_whisper import WhisperModel

    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=8)
    failed = False
    for voice in voices:
        for lang, text in TEXTS.items():
            status, hdr, wav = post_json(args.url, "/synthesize", {
                "text": text, "voice": voice["name"], "language": lang,
                "engines": ["voxcpm"], "seed": SEED,
            })
            if status != 200:
                print(f'FAIL {voice["name"]} {lang}: HTTP {status}')
                failed = True
                continue
            tmp = HERE / "out" / f'eval-{voice["name"]}-{lang}.wav'
            tmp.parent.mkdir(exist_ok=True)
            tmp.write_bytes(wav)
            audio, rate = sf.read(tmp, dtype="float32")
            if audio.ndim > 1:
                audio = audio.mean(1)
            g = gcd(rate, 16000)
            audio16 = resample_poly(audio, 16000 // g, rate // g).astype("float32")
            segments, info = model.transcribe(audio16, task="transcribe", beam_size=5)
            heard = " ".join(s.text.strip() for s in segments)
            error = wer(text, heard)
            f0 = median_f0(audio, rate)
            band = F0_BAND[voice["gender"]]
            problems = []
            if hdr.get("x-vox-engine") != "voxcpm":
                problems.append(f'engine={hdr.get("x-vox-engine")}')
            if hdr.get("x-vox-voice-source") != "registered":
                problems.append(f'source={hdr.get("x-vox-voice-source")}')
            if info.language != lang:
                problems.append(f"heard as {info.language}")
            if error > MAX_WER:
                problems.append(f"wer={error:.2f}")
            if not band[0] <= f0 <= band[1]:
                problems.append(f"f0={f0:.0f}Hz outside {band}")
            failed |= bool(problems)
            print(f'{"FAIL" if problems else "ok  "} {voice["name"]} {lang} wer={error:.3f} '
                  f'lang={info.language}({info.language_probability:.2f}) f0={f0:.0f}Hz '
                  f'source={hdr.get("x-vox-voice-source")} {" ".join(problems)}')
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
