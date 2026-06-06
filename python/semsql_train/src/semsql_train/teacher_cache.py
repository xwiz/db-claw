"""Teacher-output cache builder — gold SQL → NatSQL skeletons.

Why this exists
---------------

the Stage 2 training contract §4.1 calls for a teacher fine-tune (M2) producing
sequence-level KD targets. On the laptop recipe (the local training rationale)
we skip M2 entirely: Spider 1.0 + BIRD ship gold SQL, and the gold SQL
*is* the answer the teacher would converge to. So we transpile gold SQL
→ NatSQL deterministically and treat the result as the teacher's
one-best output.

This module is the offline cache builder. It reads Spider / BIRD JSON
manifests, walks each gold SQL through sqlglot, and emits training
records in the shape that
:func:`semsql_train.trainers.skeleton.build_dataset` validates:

    {
      "stage": 2,
      "nl":             "<question text>",
      "ranked_schema":  [{"kind": "entity", "target": "...", "score": 1.0}, ...],
      "natsql_skeleton":"SELECT @field1 FROM @entity1 WHERE @field2 = @val1",
      "slot_map":       {"@entity1": "users", "@field1": "users.email", ...}
    }

NatSQL v0.2 subset constraints (single FROM, no JOIN, no HAVING, no
CTEs) are enforced at conversion time — out-of-subset rows are skipped
with the reason recorded in the run summary so the operator sees the
retention rate without grepping. Per RESDSQL's NatSQL paper retention
is ~94 % on Spider 1.0 dev / ~76 % on BIRD dev.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

__all__ = [
    "ConversionStats",
    "build_teacher_cache",
    "build_teacher_cache_from_nstext2sql_jsonl",
    "build_teacher_cache_from_omnisql",
    "build_teacher_cache_from_omnisql_bird_json",
    "build_teacher_cache_from_sqale",
    "convert_one",
]

# NatSQL v0.3 subset:
# - Up to 3 INNER JOIN chain (was 1 in v0.2)
# - HAVING with aggregate predicate (was rejected in v0.2)
# - Subqueries / CTEs / set ops / OUTER JOINs still rejected
_MAX_JOINS = 3
_NON_CANONICAL_CHARS = re.compile(r"[^a-z0-9]+")


def _canonicalize_name(raw: str) -> str:
    """Mirror the runtime DB extractor's canonical snake-case naming."""
    canonical = _NON_CANONICAL_CHARS.sub("_", raw.strip().lower()).strip("_")
    canonical = re.sub(r"_+", "_", canonical)
    if not canonical:
        return "_"
    if canonical[0].isdigit():
        canonical = f"_{canonical}"
    return canonical[:63]


def _canonicalize_field_target(target: str) -> str:
    if "." not in target:
        return _canonicalize_name(target)
    entity, field = target.split(".", 1)
    return f"{_canonicalize_name(entity)}.{_canonicalize_name(field)}"


@dataclass
class ConversionStats:
    """Aggregate counts from one cache build run."""

    total: int = 0
    converted: int = 0
    skipped_join: int = 0
    skipped_subquery: int = 0
    skipped_having: int = 0
    skipped_set_op: int = 0
    skipped_cte: int = 0
    skipped_parse_error: int = 0
    skipped_other: int = 0
    skip_reasons: list[str] = field(default_factory=list)

    @property
    def retention(self) -> float:
        return (self.converted / self.total) if self.total else 0.0


def build_teacher_cache(
    spider_manifest: Path | None,
    bird_manifest: Path | None,
    out_jsonl: Path,
    *,
    dialect: str = "sqlite",
    keep_skip_reasons: int = 50,
) -> ConversionStats:
    """Build ``out_jsonl`` from one or both source manifests.

    Each manifest is the canonical Spider/BIRD ``dev.json`` (a JSON list
    of dicts with keys ``db_id``, ``question``, ``query``/``SQL``).

    Returns a :class:`ConversionStats` summarising how many rows landed
    versus how many were skipped, broken down by reason. ``skip_reasons``
    keeps the first ``keep_skip_reasons`` skip messages verbatim so the
    operator can debug a regression without re-running the build.
    """
    stats = ConversionStats()
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    with out_jsonl.open("w", encoding="utf-8") as fh:
        for manifest in (spider_manifest, bird_manifest):
            if manifest is None:
                continue
            entries = json.loads(manifest.read_text(encoding="utf-8"))
            for entry in entries:
                stats.total += 1
                gold_sql = (
                    entry.get("query") or entry.get("SQL") or entry.get("sql")
                )
                question = entry.get("question") or entry.get("instruction")
                db_id = entry.get("db_id") or entry.get("db")
                if not gold_sql or not question:
                    stats.skipped_other += 1
                    if len(stats.skip_reasons) < keep_skip_reasons:
                        stats.skip_reasons.append(
                            f"missing question/sql in entry for db_id={db_id!r}"
                        )
                    continue
                try:
                    record = convert_one(question, gold_sql, db_id, dialect=dialect)
                except _ConversionSkip as e:
                    _bucket_skip(stats, e)
                    continue
                except Exception as e:
                    stats.skipped_parse_error += 1
                    if len(stats.skip_reasons) < keep_skip_reasons:
                        stats.skip_reasons.append(
                            f"parse {db_id!r}: {type(e).__name__}: {e}"
                        )
                    continue
                fh.write(json.dumps(record, sort_keys=True))
                fh.write("\n")
                stats.converted += 1
    return stats


def build_teacher_cache_from_sqale(
    out_jsonl: Path,
    *,
    dataset_id: str = "trl-lab/SQaLe-text-to-SQL-dataset",
    split: str = "train",
    dialect: str = "sqlite",
    max_rows: int | None = None,
    keep_skip_reasons: int = 50,
    log_every: int = 5000,
    parquet_glob: str | None = None,
) -> ConversionStats:
    """Stream SQaLe and build the Stage 2 skeleton corpus.

    SQaLe schema (per ``trl-lab/SQaLe-text-to-SQL-dataset``):

        {schema: <DDL str>, question: <str>, query: <str>, num_joins: int,
         num_tables: int, number_of_columns: int, token_count: dict}

    We don't need the DDL for skeleton training — the table/column
    references are extracted from the SQL itself by sqlglot, just like
    the Spider path. ``num_joins`` is used for an early skip on rows
    that exceed _MAX_JOINS without paying the parse cost.

    Streams via ``datasets.load_dataset(streaming=True)`` so the 500k+
    rows never need to fit in RAM.
    """
    try:
        import datasets
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "SQaLe ingest requires `pip install datasets`."
        ) from e

    stats = ConversionStats()
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    # Iterate either local parquet shards (offline, robust) or HF stream.
    if parquet_glob is not None:
        import glob

        import pyarrow.parquet as pq

        files = sorted(glob.glob(parquet_glob, recursive=True))
        if not files:
            raise RuntimeError(f"no parquet files matched glob: {parquet_glob}")

        def _iter_local() -> Iterator[dict]:
            for fp in files:
                table = pq.read_table(fp)
                for row in table.to_pylist():
                    yield row

        ds = _iter_local()
    else:
        ds = datasets.load_dataset(dataset_id, split=split, streaming=True)
    with out_jsonl.open("w", encoding="utf-8") as fh:
        for i, entry in enumerate(ds):
            if max_rows is not None and i >= max_rows:
                break
            stats.total += 1

            # Cheap pre-filter: skip if the row already exceeds JOIN cap.
            if entry.get("num_joins", 0) > _MAX_JOINS:
                stats.skipped_join += 1
                continue

            gold_sql = entry.get("query")
            question = entry.get("question")
            if not gold_sql or not question:
                stats.skipped_other += 1
                continue

            # Use a stable per-row id as the db_id (no other handle in SQaLe).
            db_id = f"sqale_{i}"
            try:
                record = convert_one(question, gold_sql, db_id, dialect=dialect)
            except _ConversionSkip as e:
                _bucket_skip(stats, e)
                continue
            except Exception as e:
                stats.skipped_parse_error += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"parse {db_id}: {type(e).__name__}: {e}"
                    )
                continue
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
            stats.converted += 1

            if log_every and (stats.total % log_every == 0):
                print(
                    f"[sqale] total={stats.total} converted={stats.converted} "
                    f"retention={stats.retention:.1%}",
                    flush=True,
                )

    return stats


def build_teacher_cache_from_omnisql(
    out_jsonl: Path,
    *,
    dataset_id: str = "RUCKBReasoning/OmniSQL-synthetic-data",
    split: str = "train",
    dialect: str = "sqlite",
    max_rows: int | None = None,
    keep_skip_reasons: int = 50,
    log_every: int = 5000,
    parquet_glob: str | None = None,
    question_key: str = "question",
    sql_key: str = "sql",
    db_id_key: str = "db_id",
) -> ConversionStats:
    """Stream OmniSQL and build the Stage 2 skeleton corpus.

    OmniSQL (RUCKBReasoning) is a large synthetic NL→SQL corpus aligned
    with BIRD's dialect/style — Phase C of the completion plan ingests
    it as the in-distribution top-up to Spider+SQaLe. Schema (typical):

        {db_id: <str>, question: <str>, sql: <str>, schema: <str>, ...}

    Column names are exposed as ``question_key`` / ``sql_key`` /
    ``db_id_key`` so we can adapt to OmniSQL variants without code
    edits if upstream renames anything.

    Same skip semantics as the Spider/SQaLe paths — out-of-v0.3 rows
    (>3 JOINs, OUTER JOIN, subquery, CTE, set op) bucket in
    :class:`ConversionStats` and the run summary surfaces retention.

    Streams via ``datasets.load_dataset(streaming=True)`` so the full
    corpus never needs to fit in RAM. Pass ``parquet_glob`` for
    offline ingest from a local mirror.
    """
    try:
        import datasets
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "OmniSQL ingest requires `pip install datasets`."
        ) from e

    stats = ConversionStats()
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    if parquet_glob is not None:
        import glob

        import pyarrow.parquet as pq

        files = sorted(glob.glob(parquet_glob, recursive=True))
        if not files:
            raise RuntimeError(f"no parquet files matched glob: {parquet_glob}")

        def _iter_local() -> Iterator[dict]:
            for fp in files:
                table = pq.read_table(fp)
                for row in table.to_pylist():
                    yield row

        ds = _iter_local()
    else:
        ds = datasets.load_dataset(dataset_id, split=split, streaming=True)

    with out_jsonl.open("w", encoding="utf-8") as fh:
        for i, entry in enumerate(ds):
            if max_rows is not None and i >= max_rows:
                break
            stats.total += 1

            gold_sql = entry.get(sql_key) or entry.get("query")
            question = entry.get(question_key) or entry.get("instruction")
            db_id = entry.get(db_id_key) or f"omnisql_{i}"
            if not gold_sql or not question:
                stats.skipped_other += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"missing {question_key!r}/{sql_key!r} in entry "
                        f"db_id={db_id!r}"
                    )
                continue

            try:
                record = convert_one(question, gold_sql, db_id, dialect=dialect)
            except _ConversionSkip as e:
                _bucket_skip(stats, e)
                continue
            except Exception as e:
                stats.skipped_parse_error += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"parse {db_id}: {type(e).__name__}: {e}"
                    )
                continue
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
            stats.converted += 1

            if log_every and (stats.total % log_every == 0):
                print(
                    f"[omnisql] total={stats.total} converted={stats.converted} "
                    f"retention={stats.retention:.1%}",
                    flush=True,
                )

    return stats


_OMNISQL_QUESTION_RX = re.compile(
    r"Question:\s*\n(.*?)\n\s*Instructions:", re.DOTALL
)
_OMNISQL_SQL_RX = re.compile(
    r"```sql\s*\n?(?:--[^\n]*\n)?(.*?)```", re.DOTALL
)


def _parse_omnisql_bird_row(entry: dict) -> tuple[str, str] | None:
    """Pull (question, sql) from one ``xxxbrem/OmniSQL-BIRD`` row.

    Each row has ``input_seq`` (schema DDL + ``Question:\\n...`` block +
    ``Instructions:`` footer) and ``output_seq`` (reasoning chain ending
    in a ```sql ... ``` block). Returns ``None`` when either side is
    unparseable so the caller can bucket it under ``skipped_other``
    rather than raise.
    """
    inp = entry.get("input_seq") or ""
    out = entry.get("output_seq") or ""
    q_match = _OMNISQL_QUESTION_RX.search(inp)
    if not q_match:
        return None
    question = q_match.group(1).strip()
    s_match = _OMNISQL_SQL_RX.search(out)
    if not s_match:
        return None
    sql = s_match.group(1).strip().rstrip(";").strip()
    if not question or not sql:
        return None
    return question, sql


def build_teacher_cache_from_omnisql_bird_json(
    in_json: Path,
    out_jsonl: Path,
    *,
    dialect: str = "sqlite",
    max_rows: int | None = None,
    keep_skip_reasons: int = 50,
    log_every: int = 2000,
) -> ConversionStats:
    """Ingest ``xxxbrem/OmniSQL-BIRD/train_bird.json`` shape.

    The format isn't a flat ``{question, sql}`` list — each row is a
    chat-style ``(input_seq, output_seq)`` pair where the SQL is wrapped
    in a ```sql ... ``` block at the end of the assistant response and
    the question lives between ``Question:`` and ``Instructions:``
    markers in the prompt. We extract both with regexes and feed
    ``(question, sql)`` to :func:`convert_one`.

    This is the BIRD-aligned synthetic in-distribution top-up the Phase
    C plan calls for — substantially smaller than the full 22 GB
    ``seeklhy/OmniSQL-datasets`` snapshot and already filtered to
    BIRD-style reasoning queries.
    """
    stats = ConversionStats()
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    raw = json.loads(in_json.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError(
            f"OmniSQL-BIRD json {in_json} must be a list of objects"
        )

    with out_jsonl.open("w", encoding="utf-8") as fh:
        for i, entry in enumerate(raw):
            if max_rows is not None and i >= max_rows:
                break
            stats.total += 1

            parsed = _parse_omnisql_bird_row(entry)
            if parsed is None:
                stats.skipped_other += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"omnisql parse {i}: missing question/sql block"
                    )
                continue
            question, gold_sql = parsed
            db_id = f"omnisql_bird_{i}"

            try:
                record = convert_one(question, gold_sql, db_id, dialect=dialect)
            except _ConversionSkip as e:
                _bucket_skip(stats, e)
                continue
            except Exception as e:
                stats.skipped_parse_error += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"parse {db_id}: {type(e).__name__}: {e}"
                    )
                continue

            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
            stats.converted += 1

            if log_every and (stats.total % log_every == 0):
                print(
                    f"[omnisql-bird] total={stats.total} "
                    f"converted={stats.converted} "
                    f"retention={stats.retention:.1%}",
                    flush=True,
                )

    return stats


_NSTEXT2SQL_QUESTION_RX = re.compile(
    r"--\s*Using valid SQLite[^\n]*\n+((?:--\s*[^\n]*\n+)+)\s*$",
    re.IGNORECASE,
)


def _parse_nstext2sql_row(entry: dict) -> tuple[str, str] | None:
    """Pull (question, sql) from one NSText2SQL row.

    Each row's ``instruction`` is ``<DDL>\n\n-- Using valid SQLite, …\n
    -- <question text>``. We grep the trailing comment block; the SQL
    is the ``output`` field verbatim.
    """
    instr = entry.get("instruction") or ""
    sql = (entry.get("output") or "").strip().rstrip(";").strip()
    if not instr or not sql:
        return None
    m = _NSTEXT2SQL_QUESTION_RX.search(instr)
    if not m:
        return None
    block = m.group(1)
    # Strip the leading `-- ` from each comment line and join.
    lines: list[str] = []
    for raw in block.splitlines():
        s = raw.strip()
        if s.startswith("--"):
            s = s.lstrip("-").strip()
        if s:
            lines.append(s)
    question = " ".join(lines).strip()
    if not question:
        return None
    return question, sql


def build_teacher_cache_from_nstext2sql_jsonl(
    in_jsonl: Path,
    out_jsonl: Path,
    *,
    dialect: str = "sqlite",
    max_rows: int | None = None,
    keep_skip_reasons: int = 50,
    log_every: int = 5000,
) -> ConversionStats:
    """Ingest ``NumbersStation/NSText2SQL/train.jsonl``.

    Each row is ``{instruction: <DDL + question comments>, output: <SQL>,
    source: <origin>}``. We extract the question from the trailing ``--``
    comment block and feed ``(question, sql)`` to :func:`convert_one`.
    Source includes WikiSQL, sede, sql-create-context, and other public
    text-to-SQL corpora — useful breadth for the v0.3 stack.
    """
    stats = ConversionStats()
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    with in_jsonl.open("r", encoding="utf-8") as fin, out_jsonl.open(
        "w", encoding="utf-8"
    ) as fh:
        for i, line in enumerate(fin):
            if max_rows is not None and i >= max_rows:
                break
            stats.total += 1

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                stats.skipped_parse_error += 1
                continue

            parsed = _parse_nstext2sql_row(entry)
            if parsed is None:
                stats.skipped_other += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"nstext2sql parse {i}: missing question/sql"
                    )
                continue
            question, gold_sql = parsed
            db_id = f"nstext2sql_{entry.get('source', 'unknown')}_{i}"

            try:
                record = convert_one(question, gold_sql, db_id, dialect=dialect)
            except _ConversionSkip as e:
                _bucket_skip(stats, e)
                continue
            except Exception as e:
                stats.skipped_parse_error += 1
                if len(stats.skip_reasons) < keep_skip_reasons:
                    stats.skip_reasons.append(
                        f"parse {db_id}: {type(e).__name__}: {e}"
                    )
                continue

            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")
            stats.converted += 1

            if log_every and (stats.total % log_every == 0):
                print(
                    f"[nstext2sql] total={stats.total} "
                    f"converted={stats.converted} "
                    f"retention={stats.retention:.1%}",
                    flush=True,
                )

    return stats


def convert_one(
    question: str,
    gold_sql: str,
    db_id: str | None,
    *,
    dialect: str = "sqlite",
) -> dict:
    """Convert one ``(question, gold_sql)`` pair to a teacher-cache record.

    Raises :class:`_ConversionSkip` (private) when the gold SQL is
    outside the NatSQL v0.2 subset. Callers in the build loop bucket
    these into :class:`ConversionStats`; downstream callers should treat
    them as "skip this row, keep going."
    """
    parsed = sqlglot.parse_one(gold_sql, read=dialect)
    if parsed is None:
        raise _ConversionSkip("parse_error", "sqlglot returned None")

    _check_v02_subset(parsed)

    select_node = parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    if select_node is None:
        raise _ConversionSkip("other", "no SELECT in parsed AST")

    builder = _SkeletonBuilder()
    skeleton = builder.build(select_node)

    return {
        "stage": 2,
        "nl": question,
        "db_id": db_id,
        "ranked_schema": builder.ranked_schema(),
        "natsql_skeleton": skeleton,
        "slot_map": builder.slot_map(),
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


class _ConversionSkip(Exception):
    """Row was outside NatSQL v0.2; logged but not re-raised."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def _bucket_skip(stats: ConversionStats, e: _ConversionSkip) -> None:
    bucket_attr = f"skipped_{e.kind}"
    if hasattr(stats, bucket_attr):
        setattr(stats, bucket_attr, getattr(stats, bucket_attr) + 1)
    else:
        stats.skipped_other += 1
    if len(stats.skip_reasons) < 50:
        stats.skip_reasons.append(str(e))


def _check_v02_subset(node: exp.Expression) -> None:
    """NatSQL v0.3 subset check (kept legacy name for callers).

    v0.3 conversion subset:
      - Up to ``_MAX_JOINS`` (=3) INNER JOIN chain (was 1).
      - HAVING clause when predicate references aggregate (was rejected).
      - OUTER / CROSS / FULL JOINs still rejected.
      - Subqueries / CTEs / set ops still rejected.
    """
    if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
        raise _ConversionSkip("set_op", f"{type(node).__name__} not supported")
    if isinstance(node, exp.With):
        raise _ConversionSkip("cte", "WITH/CTE not supported")
    select = node if isinstance(node, exp.Select) else node.find(exp.Select)
    if select is None:
        raise _ConversionSkip("other", "no SELECT")
    joins = select.args.get("joins") or []
    if len(joins) > _MAX_JOINS:
        raise _ConversionSkip(
            "join", f"{len(joins)} JOIN(s) exceed NatSQL v0.3 limit ({_MAX_JOINS})"
        )
    for j in joins:
        # sqlglot: side="LEFT"/"RIGHT"/"FULL", kind="CROSS"/"INNER"/etc.
        join_side = (j.args.get("side") or "").upper()
        join_kind = (j.args.get("kind") or "").upper()
        if join_side in ("LEFT", "RIGHT", "FULL") or join_kind == "CROSS":
            raise _ConversionSkip(
                "join", f"{join_side or join_kind} JOIN not supported in v0.3"
            )
    # Subqueries inside the SELECT scope (in FROM, projection, WHERE).
    # Walking expressions in HAVING is fine — those are aggregate refs,
    # not nested SELECTs. We still reject any Subquery/Select under the
    # main SELECT.
    for sub in select.walk():
        if sub is select:
            continue
        if isinstance(sub, (exp.Subquery, exp.Select)) and sub is not select:
            raise _ConversionSkip("subquery", "subquery not supported in v0.3")


class _SkeletonBuilder:
    """Walks a sqlglot Select and rewrites it as a NatSQL skeleton with
    placeholder slots, recording the canonical slot map as it goes."""

    def __init__(self) -> None:
        self._entity_idx: dict[str, str] = {}
        self._field_idx: dict[str, str] = {}
        self._val_idx: list[tuple[str, str]] = []
        self._next_entity = 1
        self._next_field = 1
        self._next_val = 1
        self._concrete_entity: str | None = None
        # Alias map: SQL alias → canonical table name (handles T1/T2 aliases).
        self._alias_map: dict[str, str] = {}
        # FK edges captured from JOIN ON clauses, in first-seen order. Each
        # entry is the rendered equality "entity_a.field = entity_b.field"
        # with aliases already resolved to canonical entity names.
        self._fk_edges: list[str] = []

    def ranked_schema(self) -> list[dict]:
        """Return the (kind, target) ranked-schema list the trainer expects.

        Score is 1.0 for everything — Stage 1's job at training time is
        to learn to rank, but for the teacher cache we hand it the gold
        ranking. Score gradient comes from the negative-mining Stage 1
        trainer, not from this cache.
        """
        out: list[dict] = []
        # Entity always first (NatSQL has at most one in v0.2).
        for entity in self._entity_idx:
            out.append({"kind": "entity", "target": entity, "score": 1.0})
        for field in self._field_idx:
            out.append({"kind": "field", "target": field, "score": 1.0})
        # FK edges (v0.3) — Phase C addition. Encoder input renders these
        # as "FK: a.id = b.a_id" lines so Stage 2 sees join structure.
        for edge in self._fk_edges:
            out.append({"kind": "fk", "target": edge, "score": 1.0})
        return out

    def slot_map(self) -> dict[str, str]:
        m: dict[str, str] = {}
        for canonical, slot in self._entity_idx.items():
            m[slot] = canonical
        for canonical, slot in self._field_idx.items():
            m[slot] = canonical
        for slot, raw in self._val_idx:
            m[slot] = raw
        return m

    def build(self, select: exp.Select) -> str:
        # FROM: primary table. sqlglot's arg key is `from_` in newer
        # versions and `from` in older ones — accept either.
        from_clause = (select.args.get("from") or select.args.get("from_"))
        if from_clause is None:
            raise _ConversionSkip("other", "SELECT with no FROM")
        # `from_clause.this` is the primary source; multi-table `FROM a, b`
        # appears as additional `expressions`. Reject either as out-of-v0.2.
        extra_sources = from_clause.args.get("expressions") or []
        if extra_sources:
            raise _ConversionSkip(
                "join", f"comma-join with {len(extra_sources)} extra sources"
            )
        from_src = from_clause.this
        if not isinstance(from_src, exp.Table):
            raise _ConversionSkip(
                "other", f"FROM expects Table, got {type(from_src).__name__}"
            )
        entity_raw = from_src.name or ""
        if not entity_raw.strip():
            raise _ConversionSkip("other", "FROM table has no name")
        entity = _canonicalize_name(entity_raw)
        # Register alias → canonical name (e.g. T1 → students).
        alias = (from_src.alias or "").lower()
        if alias and alias != entity:
            self._alias_map[alias] = entity
        self._concrete_entity = entity
        entity_slot = self._slot_for_entity(entity)

        # JOINs (up to _MAX_JOINS = 3 in v0.3): emit each as a literal
        # ``INNER JOIN @entityN ON @entityN.fieldX = @entityM.fieldY``
        # slot so Stage 2 learns to produce JOIN syntax. Without this,
        # the skeleton drops JOINs and the model never sees multi-table
        # gold templates — the dominant join-underemission failure in BIRD
        # smoke).
        joins = select.args.get("joins") or []
        join_clauses_sql: list[str] = []
        for j in joins:
            join_src = j.this
            joined_slot: str | None = None
            if isinstance(join_src, exp.Table):
                joined_raw = join_src.name or ""
                joined_name = _canonicalize_name(joined_raw) if joined_raw.strip() else ""
                joined_alias = (join_src.alias or "").lower()
                if joined_name:
                    joined_slot = self._slot_for_entity(joined_name)
                    if joined_alias and joined_alias != joined_name:
                        self._alias_map[joined_alias] = joined_name
            on_expr = j.args.get("on")
            if on_expr is not None:
                edge = self._render_fk_edge(on_expr)
                if edge and edge not in self._fk_edges:
                    self._fk_edges.append(edge)
                # Render the ON predicate as slot-based fields so the
                # placeholders match the rest of the skeleton's slot map.
                on_sql = self._render_on_clause_slots(on_expr)
                if joined_slot and on_sql:
                    join_clauses_sql.append(
                        f" INNER JOIN {joined_slot} ON {on_sql}"
                    )

        # SELECT items (strip DISTINCT flag — NatSQL v0.2 doesn't represent it)
        projection = select.expressions or []
        select_parts: list[str] = []
        for item in projection:
            # SELECT DISTINCT col → SELECT col
            if isinstance(item, exp.Distinct):
                for inner in (item.expressions or []):
                    select_parts.append(self._render_projection(inner))
                continue
            select_parts.append(self._render_projection(item))
        select_sql = ", ".join(select_parts) if select_parts else "*"

        # WHERE
        where_node = select.args.get("where")
        where_sql = ""
        if where_node is not None:
            cond_sql = self._render_condition(where_node.this)
            where_sql = f" WHERE {cond_sql}"

        # GROUP BY
        group_node = select.args.get("group")
        group_sql = ""
        if group_node is not None:
            parts = [self._render_field(g) for g in group_node.expressions or []]
            if parts:
                group_sql = " GROUP BY " + ", ".join(parts)

        # HAVING (v0.3): predicate references aggregate over GROUP BY.
        # We render the same condition tree used for WHERE, but the leaf
        # field on the LHS is typically a COUNT/SUM/etc. which the
        # projection renderer already handles via _render_projection.
        having_node = select.args.get("having")
        having_sql = ""
        if having_node is not None:
            having_inner = having_node.this if hasattr(having_node, "this") else having_node
            try:
                having_sql = " HAVING " + self._render_having(having_inner)
            except _ConversionSkip:
                # Fall through — the v0.3 subset check let us in but the
                # specific predicate shape isn't supported. Re-raise.
                raise

        # ORDER BY (single key only in v0.2)
        order_node = select.args.get("order")
        order_sql = ""
        if order_node is not None and order_node.expressions:
            ord_first = order_node.expressions[0]
            ord_field = self._render_field(ord_first.this)
            direction = " DESC" if ord_first.args.get("desc") else ""
            order_sql = f" ORDER BY {ord_field}{direction}"

        # LIMIT / OFFSET
        limit_sql = ""
        limit_node = select.args.get("limit")
        if limit_node is not None and limit_node.expression is not None:
            try:
                limit_sql = f" LIMIT {int(limit_node.expression.name)}"
            except (TypeError, ValueError):
                limit_sql = f" LIMIT {limit_node.expression.sql()}"
        offset_sql = ""
        offset_node = select.args.get("offset")
        if offset_node is not None and offset_node.expression is not None:
            try:
                offset_sql = f" OFFSET {int(offset_node.expression.name)}"
            except (TypeError, ValueError):
                offset_sql = f" OFFSET {offset_node.expression.sql()}"

        joins_sql = "".join(join_clauses_sql)
        return (
            f"SELECT {select_sql} FROM {entity_slot}{joins_sql}"
            f"{where_sql}{group_sql}{having_sql}{order_sql}{limit_sql}{offset_sql}"
        )

    # ---- rendering helpers --------------------------------------------

    def _render_projection(self, item: exp.Expression) -> str:
        if isinstance(item, exp.Star):
            return "*"
        # AGG(field) or AGG(*)
        if isinstance(item, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            agg_name = type(item).__name__.upper()
            inner = item.this
            if isinstance(inner, exp.Star) or inner is None:
                return f"{agg_name}(*)"
            # COUNT(DISTINCT col) — strip DISTINCT, treat as COUNT(col)
            if isinstance(inner, exp.Distinct):
                inner = (inner.expressions or [None])[0]
                if inner is None:
                    return f"{agg_name}(*)"
            return f"{agg_name}({self._render_field(inner)})"
        # Aliased column or bare column
        if isinstance(item, exp.Alias):
            inner = item.this
            return self._render_projection(inner)
        return self._render_field(item)

    def _render_field(self, expr: exp.Expression) -> str:
        if isinstance(expr, exp.Column):
            col = _canonicalize_name(expr.name or "")
            tbl_raw = expr.table or self._concrete_entity or ""
            tbl_key = tbl_raw.lower()
            # Resolve SQL alias (T1, T2, …) → canonical table name.
            tbl_part = self._alias_map.get(
                tbl_key, _canonicalize_name(tbl_raw) if tbl_raw else ""
            )
            qualified = f"{tbl_part}.{col}" if tbl_part else col
            return self._slot_for_field(qualified)
        if isinstance(expr, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
            return self._render_projection(expr)
        # Bare identifier without table prefix
        if isinstance(expr, exp.Identifier):
            col = _canonicalize_name(expr.name)
            entity = self._concrete_entity or ""
            entity = self._alias_map.get(entity, entity)
            qualified = f"{entity}.{col}" if entity else col
            return self._slot_for_field(qualified)
        # Anything else — render as SQL and let the validator reject
        # downstream rather than silently encode an unsupported shape.
        raise _ConversionSkip(
            "other", f"unsupported field expression {type(expr).__name__}"
        )

    def _render_on_clause_slots(self, on_expr: exp.Expression) -> str | None:
        """Render a JOIN ON expression with @fieldN slot placeholders.

        Walks the simplest case: ``column = column`` (and ``Paren``
        wrappers). Multi-conjunct ANDs render the first equality only —
        consistent with the FK-edge extraction path. Returns ``None``
        for predicates we can't model so the caller skips emission of
        the JOIN clause rather than producing a malformed skeleton.
        """
        node = on_expr
        if isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            return self._render_on_clause_slots(node.this) or self._render_on_clause_slots(
                node.expression
            )
        if not isinstance(node, exp.EQ):
            return None
        lhs = self._render_field(node.this)
        rhs = self._render_field(node.expression)
        if lhs is None or rhs is None:
            return None
        return f"{lhs} = {rhs}"

    def _render_fk_edge(self, on_expr: exp.Expression) -> str | None:
        """Render a JOIN ON expression as a canonical FK edge string.

        Returns ``"entity_a.field = entity_b.field"`` (canonical, alias-
        resolved, lower-case) or ``None`` if the ON predicate is not a
        simple column-equality. Multi-condition ANDs return only the
        first equality — typical FK-style joins are a single equality;
        downstream Stage 2 doesn't need every conjunct.
        """
        node = on_expr
        if isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            # Walk down the left spine until we find an equality.
            left_edge = self._render_fk_edge(node.this)
            if left_edge is not None:
                return left_edge
            return self._render_fk_edge(node.expression)
        if not isinstance(node, exp.EQ):
            return None
        lhs = self._render_canonical_column(node.this)
        rhs = self._render_canonical_column(node.expression)
        if lhs is None or rhs is None:
            return None
        return f"{lhs} = {rhs}"

    def _render_canonical_column(self, expr: exp.Expression) -> str | None:
        if not isinstance(expr, exp.Column):
            return None
        col = _canonicalize_name(expr.name or "")
        if not col:
            return None
        tbl_raw = expr.table or ""
        tbl_key = tbl_raw.lower()
        tbl = self._alias_map.get(
            tbl_key, _canonicalize_name(tbl_raw) if tbl_raw else ""
        )
        if not tbl:
            return None
        return f"{tbl}.{col}"

    def _render_having(self, expr: exp.Expression) -> str:
        """Render a HAVING predicate. Same shape as _render_condition,
        but the LHS may be an aggregate (COUNT/SUM/AVG/MIN/MAX) referring
        to a column already in scope. We delegate the aggregate render to
        _render_projection so the placeholder reuses @fieldN slots from
        the SELECT list when the same column appears.
        """
        if isinstance(expr, exp.And):
            return f"{self._render_having(expr.this)} AND {self._render_having(expr.expression)}"
        if isinstance(expr, exp.Or):
            return f"{self._render_having(expr.this)} OR {self._render_having(expr.expression)}"
        if isinstance(expr, exp.Paren):
            return f"({self._render_having(expr.this)})"
        if isinstance(
            expr, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
        ):
            op_map = {
                exp.EQ: "=", exp.NEQ: "!=",
                exp.GT: ">", exp.GTE: ">=",
                exp.LT: "<", exp.LTE: "<=",
            }
            op = op_map[type(expr)]
            lhs = expr.this
            if isinstance(lhs, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
                lhs_sql = self._render_projection(lhs)
            else:
                lhs_sql = self._render_field(lhs)
            return f"{lhs_sql} {op} {self._slot_for_value(expr.expression)}"
        # Fall back to plain condition rendering for non-aggregate predicates.
        return self._render_condition(expr)

    def _render_condition(self, expr: exp.Expression) -> str:
        # AND-tree: render leaves joined by AND (NatSQL doesn't carry OR
        # in the v0.2 subset, but sqlglot's `or` would surface here too —
        # we don't filter explicitly because the natsql parser will reject
        # downstream).
        if isinstance(expr, exp.And):
            return f"{self._render_condition(expr.this)} AND {self._render_condition(expr.expression)}"
        if isinstance(expr, exp.Or):
            return f"{self._render_condition(expr.this)} OR {self._render_condition(expr.expression)}"
        if isinstance(expr, exp.Paren):
            return f"({self._render_condition(expr.this)})"
        if isinstance(
            expr,
            (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE),
        ):
            op_map = {
                exp.EQ: "=",
                exp.NEQ: "!=",
                exp.GT: ">",
                exp.GTE: ">=",
                exp.LT: "<",
                exp.LTE: "<=",
            }
            op = op_map[type(expr)]
            return (
                f"{self._render_field(expr.this)} {op} "
                f"{self._slot_for_value(expr.expression)}"
            )
        if isinstance(expr, exp.Like):
            return (
                f"{self._render_field(expr.this)} LIKE "
                f"{self._slot_for_value(expr.expression)}"
            )
        if isinstance(expr, exp.In):
            field_sql = self._render_field(expr.this)
            slots = [
                self._slot_for_value(v) for v in (expr.expressions or [])
            ]
            return f"{field_sql} IN ({', '.join(slots)})"
        if isinstance(expr, exp.Between):
            f = self._render_field(expr.this)
            lo = self._slot_for_value(expr.args["low"])
            hi = self._slot_for_value(expr.args["high"])
            return f"{f} BETWEEN {lo} AND {hi}"
        if isinstance(expr, exp.Is):
            inner = expr.expression
            null_keyword = "IS NULL" if isinstance(inner, exp.Null) else "IS NOT NULL"
            return f"{self._render_field(expr.this)} {null_keyword}"
        if isinstance(expr, exp.Not):
            return f"NOT {self._render_condition(expr.this)}"
        raise _ConversionSkip(
            "other", f"unsupported condition {type(expr).__name__}"
        )

    # ---- slot allocators ----------------------------------------------

    def _slot_for_entity(self, name: str) -> str:
        name = _canonicalize_name(name)
        if name not in self._entity_idx:
            self._entity_idx[name] = f"@entity{self._next_entity}"
            self._next_entity += 1
        return self._entity_idx[name]

    def _slot_for_field(self, qualified: str) -> str:
        qualified = _canonicalize_field_target(qualified)
        if qualified not in self._field_idx:
            self._field_idx[qualified] = f"@field{self._next_field}"
            self._next_field += 1
        return self._field_idx[qualified]

    def _slot_for_value(self, expr: exp.Expression) -> str:
        slot = f"@val{self._next_val}"
        self._next_val += 1
        # Render the literal verbatim so the slot_map preserves the gold
        # value. Stage 3's slot filler picks this up at decode time.
        raw = expr.sql() if hasattr(expr, "sql") else str(expr)
        self._val_idx.append((slot, raw))
        return slot
