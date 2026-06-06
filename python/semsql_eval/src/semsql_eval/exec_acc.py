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
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ExecResult", "exec_eq", "exec_results_eq", "execute"]

Row = tuple[object, ...]
Rows = tuple[Row, ...]


@dataclass(frozen=True)
class ExecResult:
    """Result of one SQL execution."""

    rows: Rows
    column_count: int
    error: str | None = None
    timed_out: bool = False

    @property
    def is_error(self) -> bool:
        return self.error is not None


def execute(
    db_path: str | Path,
    sql: str,
    *,
    timeout_seconds: float | None = 30.0,
) -> ExecResult:
    """Run ``sql`` read-only against ``db_path``. Errors are captured, not raised."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    timed_out = False
    deadline = (
        time.monotonic() + timeout_seconds
        if timeout_seconds is not None and timeout_seconds > 0
        else None
    )

    def abort_if_timed_out() -> int:
        nonlocal timed_out
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            return 1
        return 0

    try:
        if deadline is not None:
            conn.set_progress_handler(abort_if_timed_out, 1000)
        cur = conn.execute(sql)
        rows = tuple(tuple(row) for row in cur.fetchall())
        col_count = len(cur.description) if cur.description else 0
        return ExecResult(rows=rows, column_count=col_count)
    except (sqlite3.Error, Exception) as e:
        error = "sqlite execution timed out" if timed_out else str(e)
        return ExecResult(rows=(), column_count=0, error=error, timed_out=timed_out)
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def exec_eq(
    db_path: str | Path,
    gold_sql: str,
    pred_sql: str,
    *,
    timeout_seconds: float | None = 30.0,
) -> bool:
    """Return True iff ``pred_sql`` is exec-equivalent to ``gold_sql``.

    Order-sensitivity follows Spider's convention: if gold has ``ORDER BY``
    we compare ordered, else we compare as multisets.
    """
    gold = execute(db_path, gold_sql, timeout_seconds=timeout_seconds)
    pred = execute(db_path, pred_sql, timeout_seconds=timeout_seconds)
    return exec_results_eq(gold_sql, gold, pred)


def exec_results_eq(gold_sql: str, gold: ExecResult, pred: ExecResult) -> bool:
    """Compare two already-executed SQL results using Spider semantics."""
    if gold.is_error or pred.is_error:
        return False
    if gold.column_count != pred.column_count:
        return False
    order_sensitive = "order by" in gold_sql.lower()
    if order_sensitive:
        return gold.rows == pred.rows
    return _multiset_eq(gold.rows, pred.rows)


def _multiset_eq(a: Rows, b: Rows) -> bool:
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
