"""Make ``import app`` work under plain ``pytest tests`` (not just ``python -m pytest``)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
