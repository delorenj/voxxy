#!/usr/bin/env python3
"""Regenerate and register the house voices from voices.toml (stdlib only).

    python3 house-voices/build.py generate            # synthesize clips into house-voices/out/, check sha256
    python3 house-voices/build.py register            # upload out/<name>.wav as a voice (create or replace)
    python3 house-voices/build.py all                 # generate, then register
    python3 house-voices/build.py check               # GET /voices: are all four listed and tagged?

Options: --url (default $VOX_URL or https://vox.delo.sh), --out DIR, names... to limit
to some voices. $VOX_API_KEY is sent as X-API-Key when set.

Exit status: 0 ok, 1 an error, 2 a generated clip's sha256 differs from voices.toml.
A hash mismatch means the engine build, GPU stack or model weights changed: the clip
is a valid voice of the same description, just not the identical waveform. Listen to
it and run evaluate.py before registering it, then update the sha256 in voices.toml.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_manifest() -> tuple[dict, list[dict]]:
    with (HERE / "voices.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data["recipe"], data["voice"]


def headers(extra: dict | None = None) -> dict:
    h = dict(extra or {})
    if os.environ.get("VOX_API_KEY"):
        h["X-API-Key"] = os.environ["VOX_API_KEY"]
    return h


def request(url: str, path: str, *, body: bytes | None = None, content_type: str | None = None,
            timeout: float = 300.0) -> tuple[int, dict, bytes]:
    extra = {"content-type": content_type} if content_type else {}
    req = urllib.request.Request(url.rstrip("/") + path, data=body, headers=headers(extra))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def transcript(recipe: dict) -> str:
    return f'{recipe["transcript_en"]} {recipe["transcript_es"]}'


def generate(url: str, recipe: dict, voice: dict, out: Path) -> str:
    body = {
        "text": f'({voice["description"]}){transcript(recipe)}',
        "engines": [recipe["engine"]],
        "cfg": recipe["cfg"],
        "steps": recipe["steps"],
        "seed": voice["seed"],
    }
    status, hdr, data = request(url, "/synthesize", body=json.dumps(body).encode(),
                                content_type="application/json")
    if status != 200:
        raise SystemExit(f'{voice["name"]}: /synthesize returned {status}: {data[:200]!r}')
    if hdr.get("x-vox-engine") != recipe["engine"]:
        raise SystemExit(f'{voice["name"]}: served by {hdr.get("x-vox-engine")}, not {recipe["engine"]}')
    out.mkdir(parents=True, exist_ok=True)
    (out / f'{voice["name"]}.wav').write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def multipart(fields: dict[str, str], file_field: str, filename: str, payload: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
         "Content-Type: audio/wav\r\n\r\n").encode() + payload + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def register(url: str, recipe: dict, voice: dict, out: Path) -> dict:
    wav = (out / f'{voice["name"]}.wav').read_bytes()
    tags = [*recipe["tags"], voice["gender"]]
    body, ctype = multipart(
        {
            "name": voice["name"],
            "display_name": voice["display_name"],
            "prompt_text": transcript(recipe),
            "tags": ",".join(tags),
        },
        "audio", f'{voice["name"]}.wav', wav,
    )
    status, _, data = request(url, "/voices", body=body, content_type=ctype)
    if status != 200:
        raise SystemExit(f'{voice["name"]}: POST /voices returned {status}: {data[:200]!r}')
    return json.loads(data)


def check(url: str, recipe: dict, voices: list[dict]) -> bool:
    status, _, data = request(url, "/voices", timeout=30)
    if status != 200:
        raise SystemExit(f"GET /voices returned {status}")
    listed = json.loads(data)
    listed = listed if isinstance(listed, list) else listed.get("voices", [])
    by_name = {v["name"]: v for v in listed}
    ok = True
    for voice in voices:
        row = by_name.get(voice["name"])
        missing = [t for t in ("house", "clinical_ok") if not row or t not in (row.get("tags") or [])]
        good = row is not None and not missing and row.get("prompt_text") == transcript(recipe)
        ok &= good
        print(f'{"ok  " if good else "FAIL"} {voice["name"]}', "" if good else f"missing={missing or 'voice'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["generate", "register", "all", "check"])
    ap.add_argument("names", nargs="*", help="limit to these voices (default: all four)")
    ap.add_argument("--url", default=os.environ.get("VOX_URL", "https://vox.delo.sh"))
    ap.add_argument("--out", type=Path, default=HERE / "out")
    args = ap.parse_args()

    recipe, voices = load_manifest()
    if args.names:
        unknown = set(args.names) - {v["name"] for v in voices}
        if unknown:
            raise SystemExit(f"unknown voices: {sorted(unknown)}")
        voices = [v for v in voices if v["name"] in args.names]

    if args.command == "check":
        return 0 if check(args.url, recipe, voices) else 1

    drift = False
    if args.command in ("generate", "all"):
        for voice in voices:
            digest = generate(args.url, recipe, voice, args.out)
            expected = voice.get("sha256", "")
            state = "new (no sha256 in voices.toml)" if not expected else ("identical" if digest == expected else "DIFFERS")
            drift |= bool(expected) and digest != expected
            print(f'generated {voice["name"]}: sha256={digest} {state}')
    if args.command in ("register", "all"):
        if drift:
            print("not registering: a clip differs from voices.toml (see the docstring)", file=sys.stderr)
            return 2
        for voice in voices:
            row = register(args.url, recipe, voice, args.out)
            print(f'registered {row["name"]}: tags={row.get("tags")} duration_s={row.get("duration_s")}')
    return 2 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
