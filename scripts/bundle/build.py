#!/usr/bin/env python3
"""Build a self-hosted bundle from a bundle manifest (``bundle/<edition>/manifest.toml``).

    python3 scripts/bundle/build.py bundle/selfhost/manifest.toml [--out DIR] [--no-check]

Writes ``<out_dir>/<name>-<version>/`` (the unpacked bundle), ``<name>-<version>.tar.gz``
and its ``.sha256``, then runs ``check.py`` on the result and exits with its code
(0 clean, 2 warnings, 3 a forbidden or broken bundle). Nothing outside ``out_dir`` is
written, and the repository is only read.

The manifest format is product-agnostic (see the Voxxy manifest's comments): copies,
text rewrites, a house-voices step and a pruned CLI wheel. Any self-hosted product can
carry its own manifest and reuse this script and ``check.py``.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def repo_root(start: Path) -> Path:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, capture_output=True,
                             text=True, check=True)
        return Path(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return start


def git_info(root: Path) -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


class Excluder:
    def __init__(self, manifest: dict):
        ex = manifest.get("exclude", {})
        self.names = set(ex.get("names", []))
        self.suffixes = tuple(ex.get("suffixes", []))

    def __call__(self, path: Path) -> bool:
        return path.name in self.names or path.name.endswith(self.suffixes)


def copy_tree(src: Path, dest: Path, excluded: Excluder, prune: list[str] | None = None, base: Path | None = None) -> None:
    """Copy a file or a directory tree, skipping excluded names and pruned relative paths."""
    base = base or src
    prune = prune or []
    if excluded(src):
        return
    rel = src.relative_to(base).as_posix() if src != base else ""
    if rel and any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in prune):
        return
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            copy_tree(child, dest / child.name, excluded, prune, base)
    elif src.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def apply_rewrites(stage: Path, rewrites: list[dict]) -> list[str]:
    done = []
    for rule in rewrites:
        for path in sorted(stage.rglob("*")):
            rel = path.relative_to(stage).as_posix()
            if not path.is_file() or not (fnmatch.fnmatch(rel, rule["glob"]) or fnmatch.fnmatch(path.name, rule["glob"])):
                continue
            text = path.read_text()
            if rule["old"] in text:
                path.write_text(text.replace(rule["old"], rule["new"]))
                done.append(f"{rel}: {rule['old']} -> {rule['new']}")
    return done


def build_voices(root: Path, stage: Path, spec: dict) -> list[str]:
    """Copy each recipe voice's clip after checking its recorded sha256; write voices.tsv for install.sh."""
    recipe_doc = tomllib.loads((root / spec["recipe"]).read_text())
    recipe = recipe_doc["recipe"]
    prompt = f'{recipe["transcript_en"]} {recipe["transcript_es"]}'
    dest = stage / spec["dest"]
    dest.mkdir(parents=True, exist_ok=True)
    rows, names = ["# name\tdisplay_name\ttags\tprompt_text (written by scripts/bundle/build.py)"], []
    for voice in recipe_doc["voice"]:
        clip = root / spec["clips_dir"] / f'{voice["name"]}.wav'
        if not clip.is_file():
            raise SystemExit(f"house voice clip missing: {clip}")
        got = sha256(clip)
        if got != voice["sha256"]:
            raise SystemExit(f'{clip}: sha256 {got} does not match the recipe ({voice["sha256"]}); regenerate it')
        shutil.copy2(clip, dest / clip.name)
        tags = ",".join([*recipe["tags"], voice["gender"]])
        for field in (voice["name"], voice["display_name"], tags, prompt):
            if "\t" in field or "\n" in field:
                raise SystemExit(f"tab or newline in a voices.tsv field for {voice['name']}")
        rows.append("\t".join([voice["name"], voice["display_name"], tags, prompt]))
        names.append(voice["name"])
    (dest / "voices.tsv").write_text("\n".join(rows) + "\n")
    return names


def bundle_module_source(values: dict) -> str:
    lines = ['"""Edition defaults for this copy of the CLI: the self-hosted bundle (written by the bundle build)."""', ""]
    for key, value in values.items():
        lines.append(f"{key} = {value!r}")
    if "FLEET" not in values:
        lines.append('FLEET = EDITION == "fleet"')
    return "\n".join(lines) + "\n"


def drop_dependencies(pyproject: Path, names: list[str]) -> list[str]:
    """Remove dependency lines naming any of ``names`` (e.g. yt-dlp) from a pyproject's lists."""
    dropped, out = [], []
    for line in pyproject.read_text().splitlines(keepends=True):
        stripped = line.strip()
        m = re.match(r'^"([A-Za-z0-9_.\-]+)', stripped)
        if m and m.group(1).lower() in {n.lower() for n in names}:
            dropped.append(stripped.rstrip(","))
            continue
        out.append(line)
    pyproject.write_text("".join(out))
    return dropped


def build_cli(root: Path, stage: Path, spec: dict, excluded: Excluder) -> Path:
    project = root / spec["project"]
    with tempfile.TemporaryDirectory(prefix="voxxy-bundle-cli-") as tmp:
        work = Path(tmp) / "cli"
        copy_tree(project, work, excluded, prune=spec.get("prune", []))
        pkg_dirs = [p for p in work.iterdir() if p.is_dir() and (p / "_bundle.py").is_file()]
        if len(pkg_dirs) != 1:
            raise SystemExit(f"expected one package with _bundle.py under {project}, found {len(pkg_dirs)}")
        (pkg_dirs[0] / "_bundle.py").write_text(bundle_module_source(spec.get("bundle_module", {})))
        drop_dependencies(work / "pyproject.toml", spec.get("drop_dependencies", []))
        (work / "uv.lock").unlink(missing_ok=True)
        dest = stage / spec["dest"]
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dest), str(work)], check=True,
                       capture_output=True, text=True)
    wheels = sorted(dest.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel in {dest}, found {len(wheels)}")
    for junk in dest.iterdir():  # uv build may leave a .gitignore in the out dir
        if junk.suffix != ".whl":
            junk.unlink()
    return wheels[0]


def write_index(stage: Path, meta: dict) -> None:
    files = []
    for path in sorted(stage.rglob("*")):
        if path.is_file() and path.name != "BUNDLE.json":
            files.append({"path": path.relative_to(stage).as_posix(), "bytes": path.stat().st_size, "sha256": sha256(path)})
    (stage / "BUNDLE.json").write_text(json.dumps({**meta, "files": files}, indent=2) + "\n")


def make_tarball(stage: Path, tarball: Path, mtime: int) -> None:
    """A reproducible tar.gz: sorted entries, fixed owner and mtime."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in [stage, *sorted(stage.rglob("*"))]:
            info = tar.gettarinfo(str(path), arcname=(Path(stage.name) / path.relative_to(stage)).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = mtime
            if path.is_file():
                with path.open("rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)
    with tarball.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=mtime, filename="") as gz:
        gz.write(buf.getvalue())


def build(manifest_path: Path, out: Path | None = None, *, wheel: bool = True) -> tuple[Path, Path]:
    manifest = tomllib.loads(manifest_path.read_text())
    root = repo_root(manifest_path.resolve().parent)
    b = manifest["bundle"]
    label = f'{b["name"]}-{b["version"]}'
    out_dir = (out or root / b["out_dir"]).resolve()
    stage = out_dir / label
    if stage.exists():
        if out_dir not in stage.resolve().parents:
            raise SystemExit(f"refusing to clear {stage}: not under {out_dir}")
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    excluded = Excluder(manifest)

    for item in manifest.get("copy", []):
        src = root / item["src"]
        if not src.exists():
            raise SystemExit(f"manifest copies {item['src']}, which does not exist")
        dest = stage / item["dest"]
        copy_tree(src, dest, excluded)
        if "mode" in item:
            dest.chmod(item["mode"])
    rewrites = apply_rewrites(stage, manifest.get("rewrite", []))
    voices = build_voices(root, stage, manifest["voices"]) if "voices" in manifest else []
    wheel_path = build_cli(root, stage, manifest["cli"], excluded) if wheel and "cli" in manifest else None
    (stage / "VERSION").write_text(b["version"] + "\n")

    info = git_info(root)
    stamp = int(subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=root, capture_output=True, text=True)
                .stdout.strip() or 0)
    write_index(stage, {
        "name": b["name"], "product": b.get("product"), "edition": b.get("edition"), "version": b["version"],
        "commit": info["commit"], "dirty": info["dirty"],
        "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "voices": voices, "cli_wheel": wheel_path.name if wheel_path else None, "rewrites": rewrites,
    })
    tarball = out_dir / f"{label}.tar.gz"
    make_tarball(stage, tarball, stamp)
    (out_dir / f"{label}.tar.gz.sha256").write_text(f"{sha256(tarball)}  {tarball.name}\n")
    return stage, tarball


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--out", type=Path, help="Output directory (default: the manifest's out_dir).")
    ap.add_argument("--no-check", action="store_true", help="Skip check.py.")
    ap.add_argument("--check-args", default="", help="Extra arguments for check.py, e.g. '--skip cli-smoke'.")
    args = ap.parse_args(argv)
    stage, tarball = build(args.manifest, args.out)
    print(f"bundle: {stage}")
    print(f"tarball: {tarball} ({tarball.stat().st_size / 1e6:.1f} MB)")
    if args.no_check:
        return 0
    cmd = [sys.executable, str(HERE / "check.py"), str(stage), "--manifest", str(args.manifest), *args.check_args.split()]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    sys.exit(main())
