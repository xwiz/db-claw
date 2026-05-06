"""pytest entry — adds local src + sibling rewriter src to sys.path.

The training pipeline depends on the rewriter's graph reader for loading
entities/fields/enums. Once we wire ``uv sync`` in CI both packages will
be installed editable and this conftest becomes a no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).parent
_REWRITER_SRC = _HERE.parent / "semsql_rewriter" / "src"
_OWN_SRC = _HERE / "src"
_TESTS = _HERE  # so `tests.fixtures.make_graph` resolves from inside tests modules

for p in (_OWN_SRC, _REWRITER_SRC, _TESTS):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
