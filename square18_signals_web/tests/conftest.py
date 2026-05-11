"""Shared test bootstrap for import paths.

Allows running pytest from workspace root without manual PYTHONPATH.
"""
from __future__ import annotations

import sys
from pathlib import Path


_TESTS_DIR = Path(__file__).resolve().parent
_WEB_ROOT = _TESTS_DIR.parent
_LIB_SRC = _WEB_ROOT.parent / "square18_signals" / "src"

for _p in (str(_WEB_ROOT), str(_LIB_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
