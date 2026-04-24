"""Typed exceptions + documented exit codes.

Exit codes (matches spec §AC9):
  0 — success
  1 — generic failure
  2 — degraded state / bad invocation (wrong flag combo)
  3 — unreachable (network / server)
  4 — not found (voice / engine / project)
  5 — validation (bad slug, missing --name in non-interactive mode)
"""

from __future__ import annotations

# Re-exports for a single import surface in downstream modules that don't
# want to grep through foundation files.
from voxxy.config import ProjectNotFound
from voxxy.client import (
    VoxError,
    VoxNotFound,
    VoxUnreachable,
    VoxValidationError,
    VoxServerError,
)
from voxxy.docker import DockerError
from voxxy.audio import AudioProbeError, FfmpegMissing


class VoxxyError(Exception):
    """Base class for CLI-level errors that don't fit the foundation types."""


EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_DEGRADED = 2
EXIT_UNREACHABLE = 3
EXIT_NOT_FOUND = 4
EXIT_VALIDATION = 5


__all__ = [
    "VoxxyError",
    "ProjectNotFound",
    "VoxError",
    "VoxNotFound",
    "VoxUnreachable",
    "VoxValidationError",
    "VoxServerError",
    "DockerError",
    "AudioProbeError",
    "FfmpegMissing",
    "EXIT_OK",
    "EXIT_GENERIC",
    "EXIT_DEGRADED",
    "EXIT_UNREACHABLE",
    "EXIT_NOT_FOUND",
    "EXIT_VALIDATION",
]
