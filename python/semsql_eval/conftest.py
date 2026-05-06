"""pytest entry — adds the src layout to sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
for p in (_HERE / "src",):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
