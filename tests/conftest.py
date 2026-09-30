"""Pytest configuration: make src/ and repo root importable."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
for p in (str(REPO_ROOT), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)