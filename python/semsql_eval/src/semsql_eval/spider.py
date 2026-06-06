"""Spider 1.0 / Spider 2.0 / BIRD harness.

Suites this module supports:

- ``spider`` — Spider 1.0 dev split. Reads the official ``dev.json``
  format and the per-database SQLite files at ``database/<db_id>/<db_id>.sqlite``.
- ``bird`` — BIRD dev split, same shape (different field names handled).
- ``spider2`` — Spider 2.0-lite. Reported transparently (we do not
  expect to be competitive at tiny-cascade size).

The harness is intentionally minimal: load the corpus, run a
predicting callable, score with :func:`semsql_eval.exec_acc.exec_eq`,
return per-suite metrics. Datasets are *not* bundled with this repo —
caller points at a downloaded copy on disk.

Usage::

    from semsql_eval.spider import SpiderSuite, evaluate
    suite = SpiderSuite.load(Path("data/spider/dev.json"), Path("data/spider/database"))
    summary = evaluate(suite, predict)
    print(summary.exec_acc)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .exec_acc import exec_eq

__all__ = [
    "EvalSummary",
    "Example",
    "SpiderSuite",
    "SuiteName",
    "evaluate",
]

SuiteName = Literal["spider", "spider2", "bird"]


@dataclass(frozen=True)
class Example:
    """One Spider/BIRD evaluation example."""

    db_id: str
    question: str
    gold_sql: str
    db_path: Path
    """Resolved path to the SQLite file for this example's DB."""


@dataclass(frozen=True)
class SpiderSuite:
    """A loaded suite — corpus of examples sharing one root directory of DBs."""

    name: SuiteName
    examples: tuple[Example, ...]

    @classmethod
    def load(cls, manifest: Path, db_root: Path, name: SuiteName = "spider") -> SpiderSuite:
        """Load a Spider/BIRD-style ``dev.json`` (or ``train.json``).

        Field-name normalisation across the three supported suites:

        ============ ================== ====================== ============================
        suite        question field     gold-SQL field         db-id field
        ============ ================== ====================== ============================
        spider 1.0   ``question``       ``query``              ``db_id``
        bird         ``question``       ``SQL``                ``db_id``
        spider 2.0-  ``instruction``    ``query`` (when        ``db`` (dataset name)
        lite                            inline) or via the
                                        ``gold_sql`` field
        ============ ================== ====================== ============================

        Spider 2.0-lite often stores gold SQL in an external file
        (`evaluation_suite/gold/exec_result/<instance_id>.csv`); we
        do *not* resolve those paths here. Manifests that omit an
        inline gold field for an entry surface an explicit error so
        the operator routes the run through a Spider 2.0-lite-aware
        loader outside this Spider/BIRD eval path.
        """
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{manifest}: expected a JSON array, got {type(raw).__name__}")

        examples: list[Example] = []
        for entry in raw:
            db_id = entry.get("db_id") or entry.get("db")
            if not db_id:
                raise ValueError(
                    f"{manifest}: entry missing db_id/db field: {entry!r}"
                )
            question = (
                entry.get("question")
                or entry.get("instruction")
                or entry.get("nl")
            )
            if not question:
                raise ValueError(
                    f"{manifest}: entry for {db_id!r} missing question/instruction/nl field"
                )
            gold = (
                entry.get("query")
                or entry.get("SQL")
                or entry.get("sql")
                or entry.get("gold_sql")
            )
            if not gold:
                raise ValueError(
                    f"{manifest}: entry for {db_id!r} missing query/SQL/sql/gold_sql field "
                    "(Spider 2.0-lite manifests with external gold files are not yet supported)"
                )
            db_path = db_root / db_id / f"{db_id}.sqlite"
            examples.append(
                Example(db_id=db_id, question=question, gold_sql=gold, db_path=db_path)
            )
        return cls(name=name, examples=tuple(examples))


@dataclass
class EvalSummary:
    """Aggregate results from one evaluation run."""

    suite: SuiteName
    total: int = 0
    correct: int = 0
    errored: int = 0
    """Predictions that raised or returned a SQL string the database refused."""
    bailed: int = 0
    """Predictions that returned the cascade's bail sentinel (the cascade
    couldn't pin every slot; Stage 0 + intent library weren't enough,
    and either the model stages aren't wired or they abstained). Tracked
    separately from ``wrong`` so the user can see the Stage 0 / Stage 1+
    coverage gap directly without digging through a per-example log."""

    @property
    def exec_acc(self) -> float:
        return (self.correct / self.total) if self.total else 0.0

    @property
    def error_rate(self) -> float:
        return (self.errored / self.total) if self.total else 0.0

    @property
    def bail_rate(self) -> float:
        return (self.bailed / self.total) if self.total else 0.0

    @property
    def wrong(self) -> int:
        """Predictions that ran and returned the wrong answer (NOT
        bailed, NOT errored). ``correct + wrong + bailed + errored ==
        total`` always holds."""
        return self.total - self.correct - self.bailed - self.errored


# Default cascade-bail sentinel — see `cascade_runner.make_cascade_predictor`.
DEFAULT_BAIL_SENTINEL = "SELECT 1"


def evaluate(
    suite: SpiderSuite,
    predict: Callable[[Example], str],
    *,
    on_error: Callable[[Example, BaseException], None] | None = None,
    examples: Iterable[Example] | None = None,
    bail_sentinel: str = DEFAULT_BAIL_SENTINEL,
) -> EvalSummary:
    """Run ``predict`` over every example and score exec-acc.

    ``predict`` receives an :class:`Example` and returns a SQL string. If
    the predictor raises, the example is counted as an error and (if
    provided) ``on_error`` is invoked. We do not propagate predictor
    exceptions — single-example failures must not abort the harness.

    Predictions equal to ``bail_sentinel`` are counted in
    :attr:`EvalSummary.bailed` rather than ``wrong`` so the caller can
    distinguish "cascade couldn't pin every slot" from "cascade
    committed and was wrong". A bail still contributes to ``total``
    and is NOT credited as ``correct`` even if the gold also happens
    to evaluate to the sentinel.
    """
    summary = EvalSummary(suite=suite.name)
    sentinel_norm = _normalise_for_bail(bail_sentinel)
    for ex in examples or suite.examples:
        summary.total += 1
        try:
            pred_sql = predict(ex)
        except BaseException as e:
            summary.errored += 1
            if on_error is not None:
                on_error(ex, e)
            continue
        if _normalise_for_bail(pred_sql) == sentinel_norm:
            summary.bailed += 1
            continue
        if exec_eq(ex.db_path, ex.gold_sql, pred_sql):
            summary.correct += 1
    return summary


def _normalise_for_bail(sql: str) -> str:
    """Whitespace + case + trailing-`;` insensitive normalisation. The
    cascade currently emits the sentinel without a semicolon, but a
    future change to add one (or a wrapping framework that appends
    `;`) shouldn't silently flip every bail into a `wrong` count."""
    return " ".join(sql.split()).strip().rstrip(";").strip().lower()
