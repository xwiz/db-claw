"""Execution-accuracy scorer.

Two SQL strings are *exec-accurate* iff, when run against the same SQLite
database, they return result sets that compare equal modulo:

- Row order, when the gold SQL has no ``ORDER BY``.
- Column order is preserved (Spider's exec-acc convention).
- ``NULL`` equality.

We use sqlite3 from the stdlib so the harness runs without DuckDB or any
ML framework installed. DuckDB is only needed for cross-dialect
differential rendering, which lives elsewhere.

The scorer is dialect-agnostic at the comparison level: caller is
responsible for producing SQL the database can execute. For Spider/BIRD
we assume SQLite source-of-truth.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ExecResult", "exec_eq", "execute"]


@dataclass(frozen=True)
class ExecResult:
    """Result of one SQL execution."""

    rows: tuple[tuple[object, ...], ...]
    column_count: int
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


def execute(db_path: str | Path, sql: str) -> ExecResult:
    """Run ``sql`` read-only against ``db_path``. Errors are captured, not raised."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.execute(sql)
        rows = tuple(tuple(row) for row in cur.fetchall())
        col_count = len(cur.description) if cur.description else 0
        return ExecResult(rows=rows, column_count=col_count)
    except (sqlite3.Error, Exception) as e:  # noqa: BLE001 — captured by design
        return ExecResult(rows=(), column_count=0, error=str(e))
    finally:
        conn.close()


def exec_eq(db_path: str | Path, gold_sql: str, pred_sql: str) -> bool:
    """Return True iff ``pred_sql`` is exec-equivalent to ``gold_sql``.

    Order-sensitivity follows Spider's convention: if gold has ``ORDER BY``
    we compare ordered, else we compare as multisets.
    """
    gold = execute(db_path, gold_sql)
    pred = execute(db_path, pred_sql)
    if gold.is_error or pred.is_error:
        return False
    if gold.column_count != pred.column_count:
        return False
    order_sensitive = "order by" in gold_sql.lower()
    if order_sensitive:
        return gold.rows == pred.rows
    return _multiset_eq(gold.rows, pred.rows)


def _multiset_eq(a: tuple, b: tuple) -> bool:
    if len(a) != len(b):
        return False
    # Python tuples of values are hashable iff every element is hashable;
    # SQL results may contain bytes/None/etc — all hashable. If a wild
    # value type sneaks in we fall back to a sorted-list comparison via
    # repr (slow but correct).
    try:
        from collections import Counter

        return Counter(a) == Counter(b)
    except TypeError:
        return sorted(map(repr, a)) == sorted(map(repr, b))
