#!/usr/bin/env python3
"""Live smoke test for per-request references, fail-closed routing and X-Vox-Voice-Source.

Stdlib only; run it against any deployment:

    python3 scripts/smoke_voice_source.py [--url https://vox.delo.sh] [--voice house-f1]

Checks, in order (exit 1 on the first failure):

  1. GET /healthz answers and a voxcpm engine is ready.
  2. A stock synthesis (no voice) is 200 with X-Vox-Voice-Source: stock.
  3. A registered voice (--voice, skipped when empty) is 200 with ``registered``.
  4. A synthesis with a reference clip (the stock output of step 2, re-used as the
     clip) and require_reference is 200 with ``reference`` from voxcpm.
  5. require_reference with a bad clip (stereo) is 422 + X-Vox-Error: invalid_reference,
     never a fallback to a stock voice.
  6. require_reference with an unconfigured engine allowlist is 503, not a fallback.
  7. /healthz keeps answering while a long synthesis runs (the silent-fallback regression).

No text or audio is printed, only statuses, headers and timings.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import wave


def call(url: str, path: str, body: dict | None = None, timeout: float = 240.0):
    data = None if body is None else json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if os.environ.get("VOX_API_KEY"):
        headers["X-API-Key"] = os.environ["VOX_API_KEY"]
    req = urllib.request.Request(url.rstrip("/") + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def stereo_clip(seconds: float = 1.0, rate: int = 16_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<hh", 800, -800) * int(seconds * rate))
    return buf.getvalue()


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("VOX_URL", "https://vox.delo.sh"))
    ap.add_argument("--voice", default="house-f1", help="registered voice to test ('' to skip)")
    ap.add_argument("--long-text-sentences", type=int, default=12)
    args = ap.parse_args()
    url = args.url

    status, _, body = call(url, "/healthz", timeout=10)
    health = json.loads(body) if status == 200 else {}
    voxcpm = next((e for e in health.get("engines", []) if e.get("name") == "voxcpm"), {})
    check(status == 200 and bool(voxcpm.get("ready")), "healthz: voxcpm ready", f"status={status}")

    status, hdr, stock_wav = call(url, "/synthesize", {"text": "This is a short smoke test of the voice service."})
    check(status == 200, "stock synthesis", f"status={status} engine={hdr.get('x-vox-engine')}")
    check(hdr.get("x-vox-voice-source") == "stock", "stock -> X-Vox-Voice-Source: stock",
          hdr.get("x-vox-voice-source", "(missing)"))

    if args.voice:
        status, hdr, _ = call(url, "/synthesize", {"text": "A registered voice, speaking.", "voice": args.voice})
        check(status == 200, f"registered voice {args.voice}", f"status={status} engine={hdr.get('x-vox-engine')}")
        check(hdr.get("x-vox-voice-source") == "registered", "registered -> X-Vox-Voice-Source: registered",
              hdr.get("x-vox-voice-source", "(missing)"))

    clip_b64 = base64.b64encode(stock_wav).decode()
    status, hdr, _ = call(url, "/synthesize", {
        "text": "And this sentence is spoken in the reference voice.",
        "reference_audio_b64": clip_b64,
        "require_reference": True,
        "engines": ["voxcpm"],
    })
    check(status == 200, "reference synthesis", f"status={status}")
    check(hdr.get("x-vox-voice-source") == "reference" and hdr.get("x-vox-engine") == "voxcpm",
          "reference -> X-Vox-Voice-Source: reference from voxcpm",
          f"source={hdr.get('x-vox-voice-source')} engine={hdr.get('x-vox-engine')}")

    status, hdr, _ = call(url, "/synthesize", {
        "text": "This must not be spoken.",
        "reference_audio_b64": base64.b64encode(stereo_clip()).decode(),
        "require_reference": True,
    })
    check(status == 422 and hdr.get("x-vox-error") == "invalid_reference",
          "require_reference + bad clip -> 422 invalid_reference (no fallback)",
          f"status={status} x-vox-error={hdr.get('x-vox-error')}")

    status, hdr, _ = call(url, "/synthesize", {
        "text": "This must not be spoken either.",
        "reference_audio_b64": clip_b64,
        "require_reference": True,
        "engines": ["no-such-engine"],
    })
    check(status == 503 and "x-vox-voice-source" not in hdr,
          "require_reference + unconfigured engine -> 503 (no fallback)",
          f"status={status} x-vox-error={hdr.get('x-vox-error')}")

    # A long synthesis in the background; /healthz must stay fast the whole time.
    long_text = " ".join(
        f"Sentence number {i} keeps the engine busy for a while." for i in range(args.long_text_sentences)
    )
    result: dict = {}

    def long_job() -> None:
        started = time.monotonic()
        result["status"], result["hdr"], _ = call(url, "/synthesize", {"text": long_text, "engines": ["voxcpm"]})
        result["took"] = time.monotonic() - started

    worker = threading.Thread(target=long_job)
    worker.start()
    time.sleep(1.0)
    worst = 0.0
    probes = 0
    while worker.is_alive():
        started = time.monotonic()
        status, _, body = call(url, "/healthz", timeout=10)
        worst = max(worst, time.monotonic() - started)
        probes += 1
        check(status == 200, "healthz during synthesis", f"status={status}")
        time.sleep(0.5)
    worker.join()
    check(result.get("status") == 200 and result["hdr"].get("x-vox-engine") == "voxcpm",
          "long synthesis stayed on voxcpm (no silent ElevenLabs fallback)",
          f"status={result.get('status')} engine={result['hdr'].get('x-vox-engine')} took={result['took']:.1f}s")
    check(worst < 1.5, "healthz stayed responsive during synthesis", f"worst={worst:.2f}s over {probes} probes")
    print("smoke: all checks passed")


if __name__ == "__main__":
    main()
