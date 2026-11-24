#!/usr/bin/env python3
"""Check a built self-hosted bundle against its manifest: nothing forbidden, nothing broken.

    python3 scripts/bundle/check.py dist/bundle/voxxy-selfhost-0.1.0 --manifest bundle/selfhost/manifest.toml
           [--json] [--skip cli-smoke,compose,shellcheck]

Checks, each reported as ok / warn / fail:

- index      BUNDLE.json lists every file with a matching sha256, and nothing else is present
- paths      no forbidden path (personas, the web-search clone flow, consent data, .env, ...)
- audio      every audio file is a house voice; voices.tsv names exactly those clips
- text       no forbidden string (homelab hosts, vault references, secret shapes) and no fleet voice
             or persona name, in any text file or inside the CLI wheel
- wheel      the CLI wheel has no forbidden module, no dropped dependency, and the edition module says self
- cli-smoke  the wheel installs into a fresh venv; its help lists the commands it must and must not offer
- compose    `docker compose config` accepts compose.yml
- shell      install.sh parses (bash -n), and shellcheck is clean when it is installed

Fleet voice slugs and persona ids come from the repository (manifest ``forbid.fleet_*``), so a
voice added to the fleet tomorrow is forbidden in the bundle without editing this file.
Exit codes: 0 clean, 2 warnings only, 3 at least one failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".opus", ".aup3", ".aup4"}
TEXT_LIMIT = 4 * 1024 * 1024


@dataclass
class Report:
    results: list[dict] = field(default_factory=list)

    def add(self, check: str, state: str, detail: str) -> None:
        self.results.append({"check": check, "state": state, "detail": detail})

    @property
    def exit(self) -> int:
        states = {r["state"] for r in self.results}
        return 3 if "fail" in states else 2 if "warn" in states else 0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def repo_root(start: Path) -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True, text=True, check=True)
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return start


def fleet_names(root: Path, forbid: dict) -> tuple[set[str], set[str]]:
    """(slugs forbidden as audio file stems, slugs forbidden as text) from the repo's fleet voices and personas."""
    keep = forbid.get("fleet_voice_keep_prefix", "house-")
    files: set[str] = set()
    for d in forbid.get("fleet_voice_dirs", []):
        for p in (root / d).glob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES and not p.stem.startswith(keep):
                files.add(p.stem.lower())
    for d in forbid.get("fleet_persona_dirs", []):
        for p in (root / d).glob("*.md"):
            files.add(p.stem.lower())
    allow = {s.lower() for s in forbid.get("fleet_slug_text_allow", [])}
    return files, {s for s in files if s not in allow and len(s) >= 4}


def name_pattern(slugs: set[str]) -> re.Pattern | None:
    if not slugs:
        return None
    alts = sorted((r"[-_ ]".join(re.escape(part) for part in s.split("-")) for s in slugs), key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])", re.IGNORECASE)


def iter_text(path: Path) -> str | None:
    if path.stat().st_size > TEXT_LIMIT:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_text(label: str, text: str, patterns: list[tuple[str, re.Pattern]], hits: list[str]) -> None:
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pat in patterns:
            m = pat.search(line)
            if m:
                hits.append(f"{label}:{lineno}: {name} ({m.group(0)!r})")


def forbidden_path(rel: str, entries: list[str]) -> str | None:
    parts = rel.split("/")
    for entry in entries:
        if "/" in entry:
            if rel == entry or rel.endswith("/" + entry) or f"/{entry}/" in f"/{rel}/":
                return entry
        elif entry in parts:
            return entry
    return None


def check(stage: Path, manifest_path: Path, skip: set[str]) -> Report:
    rep = Report()
    manifest = tomllib.loads(manifest_path.read_text())
    root = repo_root(manifest_path.resolve().parent)
    forbid = manifest.get("forbid", {})
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    rels = [p.relative_to(stage).as_posix() for p in files]

    # index
    index_path = stage / "BUNDLE.json"
    if not index_path.is_file():
        rep.add("index", "fail", "BUNDLE.json is missing")
        index = {"files": []}
    else:
        index = json.loads(index_path.read_text())
        listed = {f["path"]: f for f in index.get("files", [])}
        present = set(rels) - {"BUNDLE.json"}
        bad = [p for p in sorted(present & set(listed)) if sha256(stage / p) != listed[p]["sha256"]]
        extra, missing = sorted(present - set(listed)), sorted(set(listed) - present)
        if bad or extra or missing:
            rep.add("index", "fail", f"changed {bad[:5]}, unlisted {extra[:5]}, missing {missing[:5]}")
        else:
            rep.add("index", "ok", f"{len(listed)} files match BUNDLE.json")
        if index.get("dirty"):
            rep.add("index", "warn", f"built from a working tree with uncommitted changes ({index.get('commit', '')[:12]})")

    # paths
    path_hits = [f"{r} ({e})" for r in rels if (e := forbidden_path(r, forbid.get("paths", [])))]
    rep.add("paths", "fail" if path_hits else "ok", "; ".join(path_hits[:10]) or "no forbidden paths")

    # audio
    file_slugs, text_slugs = fleet_names(root, forbid)
    keep = forbid.get("fleet_voice_keep_prefix", "house-")
    audio = [p for p in files if p.suffix.lower() in AUDIO_SUFFIXES]
    stray = [p.relative_to(stage).as_posix() for p in audio if not p.stem.startswith(keep) or p.stem.lower() in file_slugs]
    tsv = stage / manifest.get("voices", {}).get("dest", "voices") / "voices.tsv"
    listed_voices = []
    if tsv.is_file():
        listed_voices = [ln.split("\t")[0] for ln in tsv.read_text().splitlines() if ln and not ln.startswith("#")]
    shipped = sorted(p.stem for p in audio)
    if stray:
        rep.add("audio", "fail", f"audio that is not a house voice: {stray}")
    elif sorted(listed_voices) != shipped:
        rep.add("audio", "fail", f"voices.tsv lists {sorted(listed_voices)} but the bundle has {shipped}")
    else:
        rep.add("audio", "ok", f"house voices only: {', '.join(shipped)}")

    # text (bundle files and the wheel's members)
    patterns = [(f"forbidden {s!r}", re.compile(s, re.IGNORECASE)) for s in forbid.get("strings", [])]
    names = name_pattern(text_slugs)
    if names:
        patterns.append(("fleet voice or persona name", names))
    hits: list[str] = []
    wheels = [p for p in files if p.suffix == ".whl"]
    for path, rel in zip(files, rels):
        if path.suffix == ".whl" or path.suffix.lower() in AUDIO_SUFFIXES:
            continue
        text = iter_text(path)
        if text is not None:
            scan_text(rel, text, patterns, hits)
    for whl in wheels:
        with zipfile.ZipFile(whl) as z:
            for member in z.namelist():
                try:
                    text = z.read(member).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                scan_text(f"{whl.name}!{member}", text, patterns, hits)
    rep.add("text", "fail" if hits else "ok",
            ("; ".join(hits[:15]) + (f" (+{len(hits) - 15} more)" if len(hits) > 15 else "")) if hits
            else f"{len(files)} files and {len(wheels)} wheel(s) scanned, {len(text_slugs)} fleet names checked")

    # wheel
    cli = manifest.get("cli")
    if cli:
        if len(wheels) != 1:
            rep.add("wheel", "fail", f"expected one CLI wheel, found {len(wheels)}")
        else:
            with zipfile.ZipFile(wheels[0]) as z:
                members = z.namelist()
                bad = [m for m in members if forbidden_path(m, forbid.get("paths", []))]
                bad += [m for m in members for p in cli.get("prune", []) if p.endswith(".py") and m.endswith(p)]
                meta = next((z.read(m).decode() for m in members if m.endswith(".dist-info/METADATA")), "")
                deps = [d for d in cli.get("drop_dependencies", [])
                        if re.search(rf"^Requires-Dist: {re.escape(d)}\b", meta, re.MULTILINE | re.IGNORECASE)]
                bundle_mod = next((z.read(m).decode() for m in members if m.endswith("/_bundle.py")), "")
            want = cli.get("bundle_module", {}).get("EDITION")
            problems = []
            if bad:
                problems.append(f"forbidden modules {sorted(set(bad))}")
            if deps:
                problems.append(f"still requires {deps}")
            if want and f"EDITION = {want!r}" not in bundle_mod:
                problems.append(f"_bundle.py does not say EDITION = {want!r}")
            rep.add("wheel", "fail" if problems else "ok", "; ".join(problems) or f"{wheels[0].name}: edition {want}")

    # cli-smoke
    if cli and len(wheels) == 1 and "cli-smoke" not in skip:
        rep.results.append(cli_smoke(wheels[0], manifest))

    # compose
    compose = stage / "compose.yml"
    if "compose" not in skip and compose.is_file():
        if shutil.which("docker"):
            env = {**os.environ, "POSTGRES_PASSWORD": "check", "VOX_API_KEY": "check"}
            done = subprocess.run(["docker", "compose", "-f", str(compose), "config", "-q"], cwd=stage, env=env,
                                  capture_output=True, text=True)
            rep.add("compose", "ok" if done.returncode == 0 else "fail",
                    "docker compose config accepts compose.yml" if done.returncode == 0 else done.stderr.strip()[:300])
        else:
            rep.add("compose", "warn", "docker not installed here; compose.yml not validated")

    # shell
    install = stage / "install.sh"
    if install.is_file():
        done = subprocess.run(["bash", "-n", str(install)], capture_output=True, text=True)
        if done.returncode != 0:
            rep.add("shell", "fail", done.stderr.strip()[:300])
        elif not os.access(install, os.X_OK):
            rep.add("shell", "fail", "install.sh is not executable")
        elif "shellcheck" not in skip and shutil.which("shellcheck"):
            sc = subprocess.run(["shellcheck", "-S", "warning", str(install)], capture_output=True, text=True)
            rep.add("shell", "ok" if sc.returncode == 0 else "warn",
                    "install.sh parses; shellcheck clean" if sc.returncode == 0 else sc.stdout.strip()[:400])
        else:
            rep.add("shell", "ok", "install.sh parses (shellcheck not run)")
    return rep


def cli_smoke(wheel: Path, manifest: dict) -> dict:
    """Install the wheel into a fresh venv and read its help: required commands present, fleet ones absent."""
    forbid = manifest.get("forbid", {})
    if not shutil.which("uv"):
        return {"check": "cli-smoke", "state": "warn", "detail": "uv not installed here; wheel not smoke-tested"}
    with tempfile.TemporaryDirectory(prefix="voxxy-bundle-smoke-") as tmp:
        venv = Path(tmp) / "venv"
        env = {**os.environ, "HOME": tmp, "XDG_CONFIG_HOME": f"{tmp}/.config", "VIRTUAL_ENV": str(venv)}
        env.pop("VOX_API_KEY", None)
        for cmd in (["uv", "venv", "-q", "--python", "3.12", str(venv)],
                    ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(wheel)]):
            done = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if done.returncode != 0:
                return {"check": "cli-smoke", "state": "fail", "detail": f"{' '.join(cmd[:3])}: {done.stderr.strip()[:300]}"}
        voxxy = venv / "bin" / "voxxy"

        def help_for(group: str) -> str:
            args = [str(voxxy), *group.split(), "--help"] if group else [str(voxxy), "--help"]
            done = subprocess.run(args, env={**env, "COLUMNS": "200"}, capture_output=True, text=True)
            return done.stdout if done.returncode == 0 else ""

        def offers(command: str) -> bool:
            *group, leaf = command.split()
            text = help_for(" ".join(group))
            return re.search(rf"^\s*│?\s*{re.escape(leaf)}\s", text, re.MULTILINE) is not None

        if not help_for(""):
            return {"check": "cli-smoke", "state": "fail", "detail": "voxxy --help failed in a fresh venv"}
        missing = [c for c in forbid.get("cli_commands_present", []) if not offers(c)]
        present = [c for c in forbid.get("cli_commands_absent", []) if offers(c)]
    if missing or present:
        return {"check": "cli-smoke", "state": "fail",
                "detail": f"missing {missing}; should not offer {present}"}
    return {"check": "cli-smoke", "state": "ok",
            "detail": f"fresh venv: offers {forbid.get('cli_commands_present', [])}, not {forbid.get('cli_commands_absent', [])}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip", default="", help="Comma-separated checks to skip: cli-smoke, compose, shellcheck.")
    args = ap.parse_args(argv)
    rep = check(args.bundle.resolve(), args.manifest, {s.strip() for s in args.skip.split(",") if s.strip()})
    if args.json:
        print(json.dumps({"schema": "bundle.check.v1", "bundle": str(args.bundle), "exit": rep.exit,
                          "results": rep.results}, indent=2))
    else:
        for r in rep.results:
            word = {"ok": "\033[32mok\033[0m  ", "warn": "\033[33mwarn\033[0m", "fail": "\033[31mfail\033[0m"}[r["state"]]
            print(f"{word}  {r['check']:<10} {r['detail']}")
    return rep.exit


if __name__ == "__main__":
    sys.exit(main())
