"""Unit tests for the AOSP PRODUCT_OUT launcher.

Insert ``src/`` so tests import the same modules as ``python3 src/launcher.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_src_str = str(_SRC)
if _src_str not in sys.path:
    sys.path.insert(0, _src_str)
