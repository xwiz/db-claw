"""Spider/BIRD → Stage 1 (linker) training pair generator.

The linker is a cross-encoder over ``(NL, schema_item)`` pairs. To train
it we need labelled examples drawn from real benchmark questions, plus
hard negatives (similar-looking columns that aren't actually relevant).

This module reads:

  - ``tables.json`` — Spider's per-DB schema dump. Each DB lists its
    tables and columns in a stable, language-agnostic format. BIRD uses
    the same shape with one extra ``column_descriptions`` field that we
    happily ignore.
  - ``dev.json`` / ``train.json`` — the question + gold-SQL corpus.

For every ``(question, gold_sql)`` it parses the gold SQL with sqlglot
to identify which tables and columns are actually referenced, then
emits:

  - **Positives**: every referenced table → relevance 1.0; every
    referenced column → relevance 1.0.
  - **Hard negatives**: same column name on a different table in the
    same DB, capped at ``hard_negatives_per_positive`` per query so the
    label distribution doesn't collapse.
  - **Easy negatives** (optional): random irrelevant tables/columns
    from the same DB, useful for early epochs where the student hasn't
    yet learned the obvious "wrong DB" filter.

Why parse with sqlglot rather than Spider's structured ``sql`` field?
Because BIRD's gold SQL is a raw string — its structured form is
inconsistent across releases. sqlglot handles both uniformly, plus
gives us free dialect parsing (Spider is SQLite, BIRD is SQLite +
Postgres-flavoured queries). One parser, two corpora, zero divergence.

The output JSONL records match the schema consumed by
:func:`semsql_train.trainers.linker.build_dataset`, so the generator
output drops straight into the existing training pipeline.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import expressions as exp

__all__ = [
    "TableSchema",
    "DbSchemas",
    "SpiderLinkerConfig",
    "load_tables_json",
    "extract_referenced_items",
    "generate_linker_pairs_from_spider",
]


# ---------------------------------------------------------------------------
# Schema model — flat enough to drop into JSONL records, no torch needed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSchema:
    """One table in a Spider/BIRD DB."""

    db_id: str
    table: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class DbSchemas:
    """All tables across every DB in a Spider/BIRD release."""

    by_db: dict[str, tuple[TableSchema, ...]]

    def tables(self, db_id: str) -> tuple[TableSchema, ...]:
        return self.by_db.get(db_id, ())

    def column_to_table_index(self, db_id: str) -> dict[str, list[str]]:
        """``column_name → [table, table, …]`` for one DB.

        Used to pick hard negatives: a referenced column ``email`` in
        DB ``orders`` should pair against every *other* table in
        ``orders`` that also has an ``email`` column.
        """
        idx: dict[str, list[str]] = {}
        for t in self.tables(db_id):
            for c in t.columns:
                idx.setdefault(c.lower(), []).append(t.table)
        return idx


@dataclass(frozen=True)
class SpiderLinkerConfig:
    """Knobs for the Spider-derived linker corpus."""

    hard_negatives_per_positive: int = 3
    """Per question, how many same-name-different-table negatives to emit."""

    easy_negatives_per_positive: int = 2
    """Per question, how many random-irrelevant-column negatives to emit."""

    include_table_negatives: bool = True
    """Also emit table-level hard negatives (different table, similar name)."""

    seed: int = 0xC0DECAFE
    """Sampled-once for the easy-negatives pick. Deterministic."""

    drop_db_ids: frozenset[str] = field(default_factory=frozenset)
    """Skip these DBs entirely — useful when a DB has known broken gold SQL."""


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_tables_json(path: Path) -> DbSchemas:
    """Read Spider/BIRD ``tables.json`` into a :class:`DbSchemas`.

    The format is a JSON array of dicts; every dict describes one DB:

        {
          "db_id": "school",
          "table_names_original": ["district", "school"],
          "column_names_original": [[-1, "*"], [0, "district_id"], ...]
        }

    ``column_names_original`` is a list of ``[table_index, column_name]``
    pairs. ``table_index = -1`` is the literal ``*`` placeholder which
    we exclude.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON array, got {type(raw).__name__}")

    by_db: dict[str, tuple[TableSchema, ...]] = {}
    for entry in raw:
        db_id = entry["db_id"]
        table_names = entry["table_names_original"]
        col_names: list[list[Any]] = entry["column_names_original"]
        per_table: list[list[str]] = [[] for _ in table_names]
        for tbl_idx, col_name in col_names:
            if tbl_idx < 0:
                continue
            per_table[tbl_idx].append(col_name)
        by_db[db_id] = tuple(
            TableSchema(db_id=db_id, table=tn, columns=tuple(cs))
            for tn, cs in zip(table_names, per_table, strict=True)
        )
    return DbSchemas(by_db=by_db)


# ---------------------------------------------------------------------------
# Gold-SQL → referenced (table, column) extraction via sqlglot
# ---------------------------------------------------------------------------


def extract_referenced_items(
    gold_sql: str,
    schema: tuple[TableSchema, ...],
    *,
    dialect: str = "sqlite",
) -> tuple[set[str], set[str]]:
    """Return ``(referenced_tables, referenced_columns)`` from gold SQL.

    Columns are returned as ``"table.column"`` lower-cased. Table-name
    resolution policy:

      - If the column is qualified (``users.email``), use the qualifier.
      - Otherwise, search the schema for a table that has the column;
        ambiguity is resolved by picking *all* matches (over-approx so
        the linker isn't penalised for ambiguous gold SQL).

    Parsing failures don't raise — they return empty sets. That makes
    the generator skip-tolerant instead of crashing on a single bad
    record (Spider has a handful of malformed gold queries).
    """
    try:
        tree = sqlglot.parse_one(gold_sql, dialect=dialect)
    except Exception:
        return set(), set()
    if tree is None:
        return set(), set()

    by_lower = {t.table.lower(): t for t in schema}
    col_index: dict[str, list[str]] = {}
    for t in schema:
        for c in t.columns:
            col_index.setdefault(c.lower(), []).append(t.table.lower())

    tables: set[str] = set()
    columns: set[str] = set()

    # Tables: every Table node, including those inside CTEs / subqueries.
    for tnode in tree.find_all(exp.Table):
        name = (tnode.name or "").lower()
        if name and name in by_lower:
            tables.add(name)

    # Columns: explicit Column nodes plus star expansion.
    for cnode in tree.find_all(exp.Column):
        col_name = (cnode.name or "").lower()
        if not col_name or col_name == "*":
            continue
        qual = (cnode.table or "").lower() if cnode.table else None
        if qual and qual in by_lower:
            columns.add(f"{qual}.{col_name}")
            tables.add(qual)
        else:
            for t in col_index.get(col_name, []):
                columns.add(f"{t}.{col_name}")
                tables.add(t)

    return tables, columns


# ---------------------------------------------------------------------------
# Linker-pair generator
# ---------------------------------------------------------------------------


def generate_linker_pairs_from_spider(
    questions_path: Path,
    tables_path: Path,
    cfg: SpiderLinkerConfig,
    *,
    dialect: str = "sqlite",
) -> Iterator[dict]:
    """Stream linker training records from a Spider/BIRD release.

    Order is deterministic given ``cfg.seed`` — re-runs produce
    byte-identical JSONL. This is essential for the distillation loop's
    "did the data change between runs?" sanity check.

    Skip rules: questions whose DB is unknown (manifest mismatch),
    questions where sqlglot can't parse the gold SQL at all (count
    surfaced via the ``stats`` side channel — see
    :func:`generate_linker_pairs_from_spider_with_stats`).
    """
    yield from generate_linker_pairs_from_spider_with_stats(
        questions_path, tables_path, cfg, dialect=dialect
    )[0]


def generate_linker_pairs_from_spider_with_stats(
    questions_path: Path,
    tables_path: Path,
    cfg: SpiderLinkerConfig,
    *,
    dialect: str = "sqlite",
) -> tuple[list[dict], dict[str, int]]:
    """Materialise the corpus *and* return a stats dict for diagnostics.

    Stats keys: ``total_questions``, ``parsed``, ``skipped_unknown_db``,
    ``skipped_unparseable``, ``positives``, ``negatives``. Surfaced so
    a CLI can print "12,514 questions → 187k records (0.7% drop)" and
    callers can fail loudly on regressions.
    """
    schemas = load_tables_json(tables_path)
    raw = json.loads(Path(questions_path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{questions_path}: expected a JSON array")

    rng = random.Random(cfg.seed)
    out: list[dict] = []
    stats = {
        "total_questions": 0,
        "parsed": 0,
        "skipped_unknown_db": 0,
        "skipped_unparseable": 0,
        "positives": 0,
        "negatives": 0,
    }

    for entry in raw:
        stats["total_questions"] += 1
        db_id = entry["db_id"]
        if db_id in cfg.drop_db_ids:
            continue
        question = entry["question"]
        gold = entry.get("query") or entry.get("SQL") or entry.get("sql")
        if not isinstance(gold, str):
            stats["skipped_unparseable"] += 1
            continue
        schema = schemas.tables(db_id)
        if not schema:
            stats["skipped_unknown_db"] += 1
            continue

        tables, columns = extract_referenced_items(gold, schema, dialect=dialect)
        if not tables and not columns:
            stats["skipped_unparseable"] += 1
            continue
        stats["parsed"] += 1

        # Positives.
        for t in tables:
            out.append(_pair(question, "table", t, 1.0, db_id, gold))
            stats["positives"] += 1
        for c in columns:
            out.append(_pair(question, "column", c, 1.0, db_id, gold))
            stats["positives"] += 1

        # Hard negatives: same column-name on a different table in this DB.
        col_to_tables = schemas.column_to_table_index(db_id)
        emitted = 0
        for c in columns:
            seg = c.split(".", 1)[1]
            for other_table in col_to_tables.get(seg, []):
                cand = f"{other_table.lower()}.{seg}"
                if cand == c:
                    continue
                if cand in columns:
                    continue
                out.append(_pair(question, "column", cand, 0.0, db_id, gold, hard=True))
                stats["negatives"] += 1
                emitted += 1
                if emitted >= cfg.hard_negatives_per_positive * max(1, len(columns)):
                    break
            if emitted >= cfg.hard_negatives_per_positive * max(1, len(columns)):
                break

        if cfg.include_table_negatives:
            for t in schema:
                if t.table.lower() not in tables:
                    out.append(
                        _pair(question, "table", t.table.lower(), 0.0, db_id, gold, hard=True)
                    )
                    stats["negatives"] += 1
                    break  # one table negative per query — keep ratio sane

        # Easy negatives: random irrelevant columns from the same DB.
        all_cols = [
            f"{t.table.lower()}.{c.lower()}"
            for t in schema
            for c in t.columns
            if c not in {"*"} and f"{t.table.lower()}.{c.lower()}" not in columns
        ]
        rng.shuffle(all_cols)
        for cand in all_cols[: cfg.easy_negatives_per_positive]:
            out.append(_pair(question, "column", cand, 0.0, db_id, gold, hard=False))
            stats["negatives"] += 1

    return out, stats


def _pair(
    nl: str,
    kind: str,
    target: str,
    label: float,
    db_id: str,
    gold_sql: str,
    *,
    hard: bool = False,
) -> dict:
    """Linker JSONL record. Schema mirrors `trainers.linker.build_dataset`."""
    return {
        "stage": 1,
        "nl": nl,
        # Use the same canonical-kind vocabulary as the SemanticGraph.
        "candidate_kind": "entity" if kind == "table" else "field",
        "candidate_target": target.lower(),
        "relevance_label": label,
        "is_hard_negative": label == 0.0 and hard,
        "db_id": db_id,
        # Keep the gold-SQL hash for de-duplication across paraphrase
        # variants without bloating the record.
        "gold_sql_hash": hashlib.sha1(gold_sql.encode("utf-8")).hexdigest()[:12],
    }


# ---------------------------------------------------------------------------
# Convenience: write JSONL
# ---------------------------------------------------------------------------


def write_pairs_jsonl(records: Iterable[dict], dest: Path) -> int:
    """Serialise to JSONL with a stable key order. Returns count written."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dest.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True))
            fh.write("\n")
            n += 1
    return n
