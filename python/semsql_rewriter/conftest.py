"""pytest entry — adds the src layout to sys.path for in-place testing.

Once we wire `uv sync` in CI this becomes a no-op (the package will be
installed editable). For now this lets `python -m pytest` work straight
out of a fresh checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

src = Path(__file__).parent / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))
