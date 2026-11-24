"""The self-hosted bundle build and its check (scripts/bundle/, bundle/selfhost/manifest.toml).

Builds the real bundle into a temp dir, then plants each kind of forbidden content in a
copy and proves the check fails on it. The fresh-venv CLI smoke and docker compose
validation are left to `mise run bundle:check` (they need uv's network cache and docker).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "bundle" / "selfhost" / "manifest.toml"
SKIP = {"cli-smoke", "compose"}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"bundle_{name}", ROOT / "scripts" / "bundle" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


build = _load("build")
check = _load("check")

pytestmark = pytest.mark.skipif(
    not all((ROOT / "voices" / f"house-{v}.wav").is_file() for v in ("f1", "f2", "m1", "m2"))
    or shutil.which("uv") is None,
    reason="needs the house voice clips in voices/ and uv",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("bundle")
    stage, tarball = build.build(MANIFEST, out)
    return stage, tarball


def _states(rep) -> dict[str, str]:
    worst: dict[str, str] = {}
    rank = {"ok": 0, "warn": 1, "fail": 2}
    for r in rep.results:
        if rank[r["state"]] >= rank.get(worst.get(r["check"], "ok"), 0):
            worst[r["check"]] = r["state"]
    return worst


def _copy(stage: Path, tmp_path: Path) -> Path:
    dst = tmp_path / stage.name
    shutil.copytree(stage, dst)
    return dst


def _reindex(stage: Path) -> None:
    meta = json.loads((stage / "BUNDLE.json").read_text())
    meta.pop("files")
    build.write_index(stage, meta)


def test_the_real_bundle_passes(built):
    stage, tarball = built
    states = _states(check.check(stage, MANIFEST, SKIP))
    assert {k: v for k, v in states.items() if v == "fail"} == {}
    assert tarball.is_file() and (tarball.parent / f"{tarball.name}.sha256").is_file()


def test_contents_follow_the_pricing_doc(built):
    stage, _ = built
    rels = {p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()}
    for must in ("install.sh", "compose.yml", "README.md", "VERSION", "core/Dockerfile", "core/app/main.py",
                 "db/000_init.sql", "db/004_consent.sql", "engines/voxcpm/Dockerfile", "voices/voices.tsv"):
        assert must in rels, must
    assert sorted(p for p in rels if p.endswith(".wav")) == [f"voices/house-{v}.wav" for v in ("f1", "f2", "m1", "m2")]
    assert not any("vibevoice" in p for p in rels)
    wheel = next(stage.glob("cli/*.whl"))
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        bundle_mod = z.read("voxxy/_bundle.py").decode()
    assert "voxxy/commands/voice_clip.py" in names
    assert not {"voxxy/clone.py", "voxxy/commands/voice_clone.py", "voxxy/commands/persona.py"} & set(names)
    assert "EDITION = 'self'" in bundle_mod and "DEFAULT_URL = 'http://127.0.0.1:8000'" in bundle_mod
    assert "uv:latest" not in (stage / "engines" / "voxcpm" / "Dockerfile").read_text()


def test_tarball_is_reproducible(built, tmp_path):
    stage, tarball = built
    again = tmp_path / "again.tar.gz"
    build.make_tarball(stage, again, 0)
    first = tmp_path / "first.tar.gz"
    build.make_tarball(stage, first, 0)
    assert again.read_bytes() == first.read_bytes()


@pytest.mark.parametrize("plant, failing", [
    (lambda s: (s / "voices" / "rick.wav").write_bytes(b"RIFF"), "audio"),
    (lambda s: (s / "core" / "personas").mkdir() or (s / "core" / "personas" / "x.md").write_text("x"), "paths"),
    (lambda s: (s / "README.md").write_text("served from vox.delo.sh"), "text"),
    (lambda s: (s / "README.md").write_text("try the Peter Griffin voice"), "text"),
    (lambda s: (s / "README.md").write_text("key: sk-" + "a" * 32), "text"),
])
def test_the_check_catches_forbidden_content(built, tmp_path, plant, failing):
    stage = _copy(built[0], tmp_path)
    plant(stage)
    _reindex(stage)
    assert _states(check.check(stage, MANIFEST, SKIP)).get(failing) == "fail"


def test_an_edited_file_breaks_the_index(built, tmp_path):
    stage = _copy(built[0], tmp_path)
    (stage / "install.sh").write_text((stage / "install.sh").read_text() + "\n# edited\n")
    assert _states(check.check(stage, MANIFEST, SKIP))["index"] == "fail"


def test_name_pattern_matches_whole_names_with_any_separator():
    pat = check.name_pattern({"peter-griffin", "rick"})
    assert pat.search("Peter Griffin says") and pat.search("peter_griffin") and pat.search("voice rick.")
    assert not pat.search("trickle") and not pat.search("bricks")


def test_wrong_house_clip_hash_stops_the_build(tmp_path, monkeypatch):
    recipe = (ROOT / "house-voices" / "voices.toml").read_text()
    bad = tmp_path / "voices.toml"
    bad.write_text(recipe.replace('sha256 = "1ce9', 'sha256 = "0000', 1))
    with pytest.raises(SystemExit, match="does not match the recipe"):
        build.build_voices(ROOT, tmp_path / "stage", {"recipe": str(bad), "clips_dir": "voices", "dest": "voices"})
