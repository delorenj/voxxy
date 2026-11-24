"""Where the consent check's model files live, and how they get there.

The consent gate needs two models, both CPU:

- a speaker-embedding model (NeMo TitaNet-large, sherpa-onnx format, 101 MB), one pinned file;
- an ASR model (faster-whisper ``base.en``, int8), a pinned Hugging Face snapshot.

Both live under ``VOX_MODELS_DIR`` (default ``/data/models``, a compose volume, ``./models`` on the
host). A file that is not there is downloaded the first time it is needed, the TitaNet file from a
pinned URL and checked against its sha256 (a mismatch deletes the download and fails). So a plain
image rebuild works: the volume keeps the files, and an empty volume fills itself on first use.
``python -m app.models`` (``mise run models:fetch``) does the same download ahead of time.

Everything is overridable, so the spike that calibrates the models can swap them without a rebuild:

  VOX_MODELS_DIR            where model files are kept (default /data/models)
  VOX_SPEAKER_MODEL_FILE    embedding model file name inside VOX_MODELS_DIR (default nemo_en_titanet_large.onnx)
  VOX_SPEAKER_MODEL_URL     where to download it from (default: the pinned sherpa-onnx release asset)
  VOX_SPEAKER_MODEL_SHA256  expected sha256 (default: the pinned file's; "" turns the check off)
  VOX_ASR_MODEL             faster-whisper model: a size name such as base.en (default) or a directory
  VOX_ASR_REPO / VOX_ASR_REVISION  the Hugging Face repo and revision a size name resolves to
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger("vox.models")

# TitaNet-large, chosen by the G5 calibration spike (voxxy-g5-spike.md): at the same genuine-refusal rate it
# refuses 33% of strangers on phone pairs where TitaNet-small refuses 20%, and it is the model Wax already
# uses. 101,405,493 bytes; the sha256 below was checked against the release asset.
DEFAULT_SPEAKER_MODEL_FILE = "nemo_en_titanet_large.onnx"
DEFAULT_SPEAKER_MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/nemo_en_titanet_large.onnx"
)
DEFAULT_SPEAKER_MODEL_SHA256 = "d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77"

DEFAULT_ASR_MODEL = "base.en"
DEFAULT_ASR_REPO = "Systran/faster-whisper-base.en"
DEFAULT_ASR_REVISION = "3d3d5dee26484f91867d81cb899cfcf72b96be6c"
ASR_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")


class ModelUnavailable(RuntimeError):
    """A model file is missing and could not be fetched, or failed its checksum."""


def models_dir() -> Path:
    return Path(os.environ.get("VOX_MODELS_DIR", "/data/models"))


def speaker_model_path() -> Path:
    return models_dir() / os.environ.get("VOX_SPEAKER_MODEL_FILE", DEFAULT_SPEAKER_MODEL_FILE)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_speaker_model() -> Path:
    """The embedding model file, downloaded and verified when it is not already in the volume."""
    path = speaker_model_path()
    expected = os.environ.get("VOX_SPEAKER_MODEL_SHA256", DEFAULT_SPEAKER_MODEL_SHA256).strip().lower()
    if path.is_file():
        return path
    url = os.environ.get("VOX_SPEAKER_MODEL_URL", DEFAULT_SPEAKER_MODEL_URL).strip()
    if not url:
        raise ModelUnavailable(f"speaker model {path} is missing and VOX_SPEAKER_MODEL_URL is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("downloading speaker model %s -> %s", url, path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned https URL
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                out.write(block)
        if expected and sha256_of(tmp) != expected:
            raise ModelUnavailable(f"speaker model from {url} does not match the pinned sha256")
        tmp.replace(path)
    except ModelUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - network, disk, anything: the check is unavailable
        raise ModelUnavailable(f"could not download the speaker model: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    return path


def ensure_asr_model() -> str:
    """What to hand ``faster_whisper.WhisperModel``: a local directory, fetched into the volume if needed.

    ``VOX_ASR_MODEL`` may be a directory (used as is) or a model name; a name is downloaded from
    ``VOX_ASR_REPO`` at ``VOX_ASR_REVISION`` into ``<models dir>/whisper-<name>``.
    """
    name = os.environ.get("VOX_ASR_MODEL", DEFAULT_ASR_MODEL).strip() or DEFAULT_ASR_MODEL
    if Path(name).is_dir():
        return name
    target = models_dir() / f"whisper-{name}"
    if all((target / f).is_file() for f in ("config.json", "model.bin")):
        return str(target)
    repo = os.environ.get("VOX_ASR_REPO", DEFAULT_ASR_REPO).strip()
    revision = os.environ.get("VOX_ASR_REVISION", DEFAULT_ASR_REVISION).strip() or None
    try:
        from huggingface_hub import snapshot_download

        target.mkdir(parents=True, exist_ok=True)
        logger.info("downloading ASR model %s@%s -> %s", repo, revision, target)
        snapshot_download(repo, revision=revision, local_dir=str(target), allow_patterns=list(ASR_FILES))
    except Exception as exc:  # noqa: BLE001
        raise ModelUnavailable(f"could not download the ASR model: {exc}") from exc
    if not (target / "model.bin").is_file():
        raise ModelUnavailable(f"the ASR model download is incomplete: {target}")
    return str(target)


def fetch_all() -> dict[str, str]:
    return {"speaker_model": str(ensure_speaker_model()), "asr_model": ensure_asr_model()}


if __name__ == "__main__":  # pragma: no cover - `mise run models:fetch`
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for label, where in fetch_all().items():
        print(f"{label}: {where}")
