"""Lightweight proof harness for a schema/value-atlas query binder.

This module does not generate SQL. It answers the cheaper question we need
before investing in a full QueryFrame compiler:

*Can a DB-aware atlas recover the gold evidence from random NL examples without
looking at the gold SQL until scoring time?*

The harness uses gold SQL only as an evaluator. Candidate mentions, value
lookups, table/field candidates, and join reachability are derived from the
question text plus the SQLite database.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import sqlglot
from sqlglot import exp

from .spider import Example, SpiderSuite

__all__ = [
    "BinderProbeReport",
    "render_binder_probe_markdown",
    "run_binder_probe",
]


STOP_MENTIONS = {
    "A",
    "An",
    "And",
    "Among",
    "For",
    "From",
    "Give",
    "How",
    "Identify",
    "In",
    "List",
    "Name",
    "Of",
    "Please",
    "Rank",
    "State",
    "The",
    "What",
    "When",
    "Where",
    "Which",
    "Who",
}


@dataclass(frozen=True)
class ColumnInfo:
    table: str
    name: str
    sql_type: str
    pk: bool = False

    @property
    def canonical(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True)
class ValueHit:
    mention: str
    field: str
    db_value: str | None = None


@dataclass(frozen=True)
class JoinEdge:
    left_table: str
    left_column: str
    right_table: str
    right_column: str


@dataclass(frozen=True)
class GoldBinding:
    field: str
    value: str
    operator: str


@dataclass
class DbAtlas:
    db_id: str
    db_path: Path
    tables: set[str]
    columns: dict[str, ColumnInfo]
    table_columns: dict[str, list[ColumnInfo]]
    table_graph: dict[str, set[str]]
    join_edges: dict[tuple[str, str], list[JoinEdge]]
    _value_cache: dict[str, list[ValueHit]] = field(default_factory=dict)
    _value_index: dict[str, list[tuple[str, str]]] | None = None

    @classmethod
    def load(cls, db_id: str, db_path: Path) -> DbAtlas:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            table_columns: dict[str, list[ColumnInfo]] = {}
            columns: dict[str, ColumnInfo] = {}
            primary_by_table: dict[str, set[str]] = {}
            for table in sorted(tables):
                infos: list[ColumnInfo] = []
                primary_by_table[table] = set()
                for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})"):
                    name = str(row[1])
                    col = ColumnInfo(
                        table=table,
                        name=name,
                        sql_type=str(row[2] or ""),
                        pk=bool(row[5]),
                    )
                    infos.append(col)
                    columns[col.canonical.lower()] = col
                    if col.pk:
                        primary_by_table[table].add(name.lower())
                table_columns[table] = infos

            graph: dict[str, set[str]] = {table: set() for table in tables}
            join_edges: dict[tuple[str, str], list[JoinEdge]] = {}
            for table in sorted(tables):
                for row in conn.execute(f"PRAGMA foreign_key_list({_quote_ident(table)})"):
                    other = str(row[2])
                    if other in tables:
                        _add_join_edge(
                            graph,
                            join_edges,
                            left_table=table,
                            left_column=str(row[3]),
                            right_table=other,
                            right_column=str(row[4]),
                        )

            _add_inferred_relationships(
                tables,
                table_columns,
                primary_by_table,
                graph,
                join_edges,
            )
            return cls(
                db_id=db_id,
                db_path=db_path,
                tables=tables,
                columns=columns,
                table_columns=table_columns,
                table_graph=graph,
                join_edges=join_edges,
            )
        finally:
            conn.close()

    def candidate_tables(self, question: str, mentions: list[str]) -> set[str]:
        question_tokens = set(_tokens(question))
        mention_tokens = set()
        for mention in mentions:
            mention_tokens.update(_tokens(mention))
        tokens = question_tokens | mention_tokens
        out: set[str] = set()
        for table in self.tables:
            table_tokens = _name_tokens(table)
            if table_tokens & tokens or {_singular(t) for t in table_tokens} & tokens:
                out.add(table)
        return out

    def candidate_fields(self, question: str, mentions: list[str]) -> set[str]:
        tokens = set(_tokens(question))
        for mention in mentions:
            tokens.update(_tokens(mention))
        out: set[str] = set()
        for col in sorted(self.columns.values(), key=lambda item: item.canonical.lower()):
            if _name_tokens(col.name) & tokens:
                out.add(col.canonical.lower())
        return out

    def lookup_values(self, mentions: list[str]) -> list[ValueHit]:
        hits: list[ValueHit] = []
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            if self._value_index is None:
                self._value_index = self._build_value_index(conn)
            for mention in mentions:
                key = _norm_value(mention)
                if not key or len(key) < 2:
                    continue
                if key in self._value_cache:
                    hits.extend(self._value_cache[key])
                    continue
                mention_hits: list[ValueHit] = []
                for field, db_value in self._value_index.get(key, []):
                    mention_hits.append(ValueHit(mention=mention, field=field, db_value=db_value))
                self._value_cache[key] = mention_hits
                hits.extend(mention_hits)
        finally:
            conn.close()
        return hits

    def _build_value_index(self, conn: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
        index: dict[str, list[tuple[str, str]]] = {}
        for col in self.columns.values():
            if not _column_should_index_values(col):
                continue
            table = _quote_ident(col.table)
            column = _quote_ident(col.name)
            sql = (
                f"SELECT DISTINCT CAST({column} AS TEXT) FROM {table} "
                f"WHERE {column} IS NOT NULL LIMIT 20000"
            )
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error:
                continue
            field = col.canonical.lower()
            for (raw,) in rows:
                if raw is None:
                    continue
                value = str(raw).strip()
                if not value or len(value) > 100:
                    continue
                key = _norm_value(value)
                if len(key) < 2:
                    continue
                fields = index.setdefault(key, [])
                item = (field, value)
                if item not in fields:
                    fields.append(item)
        return index

    def has_table_path(self, left: str, right: str, max_hops: int = 4) -> bool:
        return self.shortest_table_path(left, right, max_hops=max_hops) is not None

    def shortest_table_path(
        self,
        left: str,
        right: str,
        *,
        max_hops: int = 4,
    ) -> list[str] | None:
        if left == right:
            return [left]
        if left not in self.table_graph or right not in self.table_graph:
            return None
        todo: deque[tuple[str, int, list[str]]] = deque([(left, 0, [left])])
        seen = {left}
        while todo:
            table, hops, path = todo.popleft()
            if hops >= max_hops:
                continue
            for nxt in self.table_graph.get(table, set()):
                if nxt == right:
                    return [*path, nxt]
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append((nxt, hops + 1, [*path, nxt]))
        return None

    def edge_for(self, left: str, right: str) -> JoinEdge | None:
        edges = self.join_edges.get((left.lower(), right.lower()), [])
        if not edges:
            return None
        return max(edges, key=_join_edge_score)


@dataclass(frozen=True)
class GoldEvidence:
    parse_ok: bool
    tables: set[str]
    columns: set[str]
    predicate_bindings: list[GoldBinding]
    aggregate_ops: set[str]
    order_ops: set[str]


@dataclass(frozen=True)
class ExampleProbe:
    index: int
    db_id: str
    question: str
    current_correct: bool | None
    mentions: list[str]
    candidate_tables: list[str]
    candidate_fields: list[str]
    value_hits: list[dict[str, str]]
    gold_tables: list[str]
    gold_columns: list[str]
    gold_bindings: list[dict[str, str]]
    table_recall: float
    field_recall: float
    literal_mention_recall: float
    value_field_recall: float
    join_path_hit: bool | None
    aggregate_hit: bool | None
    proof_ready: bool


@dataclass(frozen=True)
class BinderProbeReport:
    seed: int
    sample_size: int
    population_size: int
    only_mismatches: bool
    examples: list[ExampleProbe]
    summary: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "seed": self.seed,
                "sample_size": self.sample_size,
                "population_size": self.population_size,
                "only_mismatches": self.only_mismatches,
                "summary": self.summary,
                "examples": [example.__dict__ for example in self.examples],
            },
            indent=2,
            sort_keys=True,
        )


def run_binder_probe(
    *,
    questions_path: Path,
    db_root: Path,
    suite_name: str,
    sample_size: int,
    seed: int,
    report_json: Path | None = None,
    only_mismatches: bool = False,
) -> BinderProbeReport:
    suite = SpiderSuite.load(questions_path, db_root, name=suite_name)  # type: ignore[arg-type]
    current_by_index = _load_current_correct(report_json)
    population = list(enumerate(suite.examples))
    if only_mismatches:
        population = [
            (idx, ex)
            for idx, ex in population
            if current_by_index.get(idx) is False
        ]
    rng = random.Random(seed)
    chosen = rng.sample(population, k=min(sample_size, len(population)))

    atlas_cache: dict[str, DbAtlas] = {}
    probes: list[ExampleProbe] = []
    for index, example in chosen:
        atlas = atlas_cache.get(example.db_id)
        if atlas is None:
            atlas = DbAtlas.load(example.db_id, example.db_path)
            atlas_cache[example.db_id] = atlas
        probes.append(_probe_example(index, example, atlas, current_by_index.get(index)))

    summary = _summarize(probes, population_size=len(population))
    return BinderProbeReport(
        seed=seed,
        sample_size=len(probes),
        population_size=len(population),
        only_mismatches=only_mismatches,
        examples=probes,
        summary=summary,
    )


def render_binder_probe_markdown(report: BinderProbeReport) -> str:
    s = report.summary
    title = "Mismatch Random Probe" if report.only_mismatches else "All-Dev Random Probe"
    lines = [
        f"# Query Binder Atlas Proof: {title}",
        "",
        f"- seed: `{report.seed}`",
        f"- sample_size: `{report.sample_size}`",
        f"- population_size: `{report.population_size}`",
        f"- only_mismatches: `{report.only_mismatches}`",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for key in [
        "current_exec_acc_on_sample",
        "parse_ok_rate",
        "table_recall_avg",
        "field_recall_avg",
        "literal_mention_recall_avg",
        "value_field_recall_avg",
        "join_path_hit_rate",
        "aggregate_hit_rate",
        "proof_ready_rate",
    ]:
        value = s.get(key)
        if isinstance(value, float):
            lines.append(f"| `{key}` | `{value:.2%}` |")
        else:
            lines.append(f"| `{key}` | `{value}` |")
    lines.extend(
        [
            "",
            "## By DB",
            "",
            "| DB | n | proof-ready | value-field recall | table recall |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for db, stats in sorted(s["by_db"].items()):
        lines.append(
            f"| `{db}` | {stats['n']} | {stats['proof_ready_rate']:.2%} "
            f"| {stats['value_field_recall_avg']:.2%} | {stats['table_recall_avg']:.2%} |"
        )
    lines.extend(
        [
            "",
            "## Read",
            "",
            "This is a pre-generation proof, not a SQL execution benchmark. It scores whether",
            "a database atlas can recover the evidence needed by a deterministic QueryFrame",
            "solver: tables, fields, literal-to-field bindings, aggregate cues, and join",
            "reachability. Gold SQL is used only after binding to score the probe.",
            "",
            "A high value-field and join-path rate means the database already contains enough",
            "signal to bind noun/value mentions before SQL generation. Low proof-ready cases",
            "identify where we need synonym lexicons, derived operators, or better schema",
            "role hints before investing in the full compiler path.",
        ]
    )
    return "\n".join(lines) + "\n"


def _probe_example(
    index: int,
    example: Example,
    atlas: DbAtlas,
    current_correct: bool | None,
) -> ExampleProbe:
    mentions = extract_mentions(example.question)
    value_hits = atlas.lookup_values(mentions)
    candidate_tables = atlas.candidate_tables(example.question, mentions) | {
        hit.field.split(".", 1)[0] for hit in value_hits
    }
    candidate_fields = atlas.candidate_fields(example.question, mentions) | {
        hit.field for hit in value_hits
    }
    gold = extract_gold_evidence(example.gold_sql, atlas)

    table_recall = _recall(gold.tables, {table.lower() for table in candidate_tables})
    field_recall = _recall(gold.columns, candidate_fields)
    literal_mention_recall = _literal_mention_recall(gold.predicate_bindings, mentions)
    value_field_recall = _value_field_recall(gold.predicate_bindings, value_hits)
    join_path_hit = _join_path_hit(gold, atlas, value_hits)
    aggregate_hit = _aggregate_hit(example.question, gold)
    proof_ready = (
        gold.parse_ok
        and (table_recall >= 0.5 or not gold.tables)
        and (literal_mention_recall >= 0.5 or not gold.predicate_bindings)
        and (value_field_recall >= 0.5 or not gold.predicate_bindings)
        and (join_path_hit is not False)
        and (aggregate_hit is not False)
    )

    return ExampleProbe(
        index=index,
        db_id=example.db_id,
        question=example.question,
        current_correct=current_correct,
        mentions=mentions,
        candidate_tables=sorted(candidate_tables),
        candidate_fields=sorted(candidate_fields),
        value_hits=[hit.__dict__ for hit in value_hits],
        gold_tables=sorted(gold.tables),
        gold_columns=sorted(gold.columns),
        gold_bindings=[binding.__dict__ for binding in gold.predicate_bindings],
        table_recall=table_recall,
        field_recall=field_recall,
        literal_mention_recall=literal_mention_recall,
        value_field_recall=value_field_recall,
        join_path_hit=join_path_hit,
        aggregate_hit=aggregate_hit,
        proof_ready=proof_ready,
    )


def extract_mentions(question: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    def push(value: str) -> None:
        cleaned = value.strip().strip(".,?!;:\"'`()[]{}")
        if not cleaned or cleaned in STOP_MENTIONS:
            return
        key = _norm_value(cleaned)
        if not key or key in seen:
            return
        seen.add(key)
        out.append(cleaned)

    for match in re.finditer(r'"([^"]+)"|(?<![A-Za-z0-9])\'([^\']+)\'(?![A-Za-z0-9])', question):
        push(match.group(1) or match.group(2) or "")

    tokens = list(re.finditer(r"[A-Za-z][A-Za-z0-9_./-]*|\d+(?:[./-]\d+)*", question))
    i = 0
    connectors = {"of", "the", "and", "in", "for", "de", "la", "to"}
    while i < len(tokens):
        raw = tokens[i].group(0)
        starts_entity = (
            raw[:1].isupper()
            or "_" in raw
            or any(ch.isdigit() for ch in raw)
            or raw.isupper()
        )
        if not starts_entity:
            if len(raw) >= 4 and raw.lower() not in _STOP_WORDS:
                push(raw)
            i += 1
            continue
        parts = [raw]
        j = i + 1
        while j < len(tokens):
            nxt = tokens[j].group(0)
            if (
                nxt[:1].isupper()
                or "_" in nxt
                or any(ch.isdigit() for ch in nxt)
                or nxt.lower() in connectors
            ):
                parts.append(nxt)
                j += 1
                continue
            break
        push(" ".join(parts))
        if len(parts) > 1:
            for part in parts:
                push(part)
        i = max(j, i + 1)

    return out[:24]


def extract_gold_evidence(gold_sql: str, atlas: DbAtlas) -> GoldEvidence:
    try:
        tree = sqlglot.parse_one(gold_sql, read="sqlite")
    except Exception:
        return GoldEvidence(False, set(), set(), [], set(), set())
    if tree is None:
        return GoldEvidence(False, set(), set(), [], set(), set())
    tree = cast(exp.Expression, tree)

    aliases: dict[str, str] = {}
    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        table_name = _resolve_table_name(name, atlas)
        if table_name:
            tables.add(table_name)
            aliases[table.alias_or_name.lower()] = table_name
            aliases[table_name.lower()] = table_name

    columns: set[str] = set()
    for col in tree.find_all(exp.Column):
        resolved = _resolve_column(col, aliases, atlas)
        if resolved:
            columns.add(resolved)

    bindings = _extract_predicate_bindings(tree, aliases, atlas)
    aggregate_ops = {
        node.key.upper()
        for node in tree.walk()
        if isinstance(node, (exp.Avg, exp.Count, exp.Sum, exp.Min, exp.Max))
    }
    if any(isinstance(node, exp.Rank) for node in tree.walk()):
        aggregate_ops.add("RANK")
    order_ops = {"ORDER"} if tree.find(exp.Order) is not None else set()
    if tree.find(exp.Group) is not None:
        order_ops.add("GROUP")
    return GoldEvidence(True, tables, columns, bindings, aggregate_ops, order_ops)


def _extract_predicate_bindings(
    tree: exp.Expression,
    aliases: dict[str, str],
    atlas: DbAtlas,
) -> list[GoldBinding]:
    bindings: list[GoldBinding] = []
    predicate_types = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like)
    for node in tree.find_all(*predicate_types):
        left = node.args.get("this")
        right = node.args.get("expression")
        binding = _binding_from_pair(left, right, node.key, aliases, atlas)
        if binding:
            bindings.append(binding)
            continue
        binding = _binding_from_pair(right, left, node.key, aliases, atlas)
        if binding:
            bindings.append(binding)

    for in_node in tree.find_all(exp.In):
        col = _first_column(in_node.args.get("this"))
        if col is None:
            continue
        field = _resolve_column(col, aliases, atlas)
        if not field:
            continue
        for lit in in_node.expressions:
            if isinstance(lit, exp.Literal):
                bindings.append(GoldBinding(field=field, value=_literal_value(lit), operator="IN"))
    return _dedupe_bindings(bindings)


def _binding_from_pair(
    maybe_col: exp.Expression | None,
    maybe_lit: exp.Expression | None,
    operator: str,
    aliases: dict[str, str],
    atlas: DbAtlas,
) -> GoldBinding | None:
    col = _first_column(maybe_col)
    lit = _first_literal(maybe_lit)
    if col is None or lit is None:
        return None
    value = _literal_value(lit)
    if value in {"%Y", "%m", "%d"}:
        return None
    field = _resolve_column(col, aliases, atlas)
    if not field:
        return None
    return GoldBinding(field=field, value=value, operator=operator.upper())


def _first_column(node: exp.Expression | None) -> exp.Column | None:
    if node is None:
        return None
    if isinstance(node, exp.Column):
        return node
    return next(node.find_all(exp.Column), None)


def _first_literal(node: exp.Expression | None) -> exp.Literal | None:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        return node
    return next(node.find_all(exp.Literal), None)


def _literal_value(lit: exp.Literal) -> str:
    return str(lit.this)


def _resolve_table_name(name: str, atlas: DbAtlas) -> str | None:
    lower = name.lower()
    for table in atlas.tables:
        if table.lower() == lower:
            return table.lower()
    return None


def _resolve_column(col: exp.Column, aliases: dict[str, str], atlas: DbAtlas) -> str | None:
    name = col.name
    table = col.table
    if table:
        resolved_table = aliases.get(table.lower(), table.lower())
        key = f"{resolved_table}.{name}".lower()
        if key in atlas.columns:
            return key
    matches = [
        info.canonical.lower()
        for info in atlas.columns.values()
        if info.name.lower() == name.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _summarize(probes: list[ExampleProbe], *, population_size: int) -> dict[str, Any]:
    n = len(probes)
    current_known = [p for p in probes if p.current_correct is not None]
    by_db: dict[str, dict[str, Any]] = {}
    for db in sorted({p.db_id for p in probes}):
        db_probes = [p for p in probes if p.db_id == db]
        by_db[db] = {
            "n": len(db_probes),
            "proof_ready_rate": _avg_bool([p.proof_ready for p in db_probes]),
            "value_field_recall_avg": _avg([p.value_field_recall for p in db_probes]),
            "table_recall_avg": _avg([p.table_recall for p in db_probes]),
        }
    return {
        "n": n,
        "population_size": population_size,
        "current_exec_acc_on_sample": _avg_bool([p.current_correct for p in current_known])
        if current_known
        else None,
        "parse_ok_rate": _avg_bool([bool(p.gold_tables or p.gold_columns or p.gold_bindings) for p in probes]),
        "table_recall_avg": _avg([p.table_recall for p in probes]),
        "field_recall_avg": _avg([p.field_recall for p in probes]),
        "literal_mention_recall_avg": _avg([p.literal_mention_recall for p in probes]),
        "value_field_recall_avg": _avg([p.value_field_recall for p in probes]),
        "join_path_hit_rate": _avg_bool(
            [p.join_path_hit for p in probes if p.join_path_hit is not None]
        ),
        "aggregate_hit_rate": _avg_bool(
            [p.aggregate_hit for p in probes if p.aggregate_hit is not None]
        ),
        "proof_ready_rate": _avg_bool([p.proof_ready for p in probes]),
        "by_db": by_db,
    }


def _recall(gold: set[str], predicted: set[str]) -> float:
    if not gold:
        return 1.0
    return len(gold & predicted) / len(gold)


def _literal_mention_recall(bindings: list[GoldBinding], mentions: list[str]) -> float:
    if not bindings:
        return 1.0
    mention_norms = {_norm_value(m) for m in mentions}
    hits = 0
    for binding in bindings:
        value = _norm_value(binding.value)
        if value in mention_norms or any(value and value in mention for mention in mention_norms):
            hits += 1
    return hits / len(bindings)


def _value_field_recall(bindings: list[GoldBinding], hits: list[ValueHit]) -> float:
    searchable = [
        binding
        for binding in bindings
        if len(_norm_value(binding.value)) >= 2 and not _norm_value(binding.value).isdigit()
    ]
    if not searchable:
        return 1.0
    hit_pairs = {(_norm_value(hit.mention), hit.field) for hit in hits}
    hit_pairs.update(
        (_norm_value(hit.db_value), hit.field)
        for hit in hits
        if hit.db_value is not None
    )
    matched = 0
    for binding in searchable:
        value = _norm_value(binding.value)
        if (value, binding.field) in hit_pairs:
            matched += 1
    return matched / len(searchable)


def _join_path_hit(
    gold: GoldEvidence,
    atlas: DbAtlas,
    hits: list[ValueHit],
) -> bool | None:
    value_tables = {hit.field.split(".", 1)[0] for hit in hits}
    gold_value_tables = {binding.field.split(".", 1)[0] for binding in gold.predicate_bindings}
    value_tables &= gold_value_tables
    if not value_tables or not gold.tables:
        return None
    source_tables = gold.tables - value_tables
    if not source_tables:
        return True
    return all(
        any(atlas.has_table_path(source, value_table) for source in source_tables)
        for value_table in value_tables
    )


def _aggregate_hit(question: str, gold: GoldEvidence) -> bool | None:
    if not gold.aggregate_ops and not gold.order_ops:
        return None
    q = question.lower()
    detected: set[str] = set()
    if any(needle in q for needle in ["average", "avg", "mean"]):
        detected.add("AVG")
    if any(needle in q for needle in ["how many", "count", "number of"]):
        detected.add("COUNT")
    if any(needle in q for needle in ["total", "sum"]):
        detected.add("SUM")
    if any(needle in q for needle in ["highest", "most", "largest", "maximum", "max"]):
        detected.add("MAX")
        detected.add("ORDER")
    if any(needle in q for needle in ["lowest", "least", "minimum", "min", "earliest", "oldest"]):
        detected.add("MIN")
        detected.add("ORDER")
    if any(needle in q for needle in ["rank", "popularity"]):
        detected.add("RANK")
        detected.add("GROUP")
        detected.add("ORDER")
    target = gold.aggregate_ops | gold.order_ops
    return bool(detected & target)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _avg_bool(values: list[bool | None]) -> float:
    filtered = [v for v in values if v is not None]
    return sum(bool(v) for v in filtered) / len(filtered) if filtered else 0.0


def _load_current_correct(report_json: Path | None) -> dict[int, bool]:
    if report_json is None:
        return {}
    raw = json.loads(report_json.read_text(encoding="utf-8"))
    out: dict[int, bool] = {}
    for example in raw.get("examples", []):
        out[int(example["index"])] = bool(example.get("exec_equal"))
    return out


def _dedupe_bindings(bindings: list[GoldBinding]) -> list[GoldBinding]:
    seen: set[tuple[str, str, str]] = set()
    out: list[GoldBinding] = []
    for binding in bindings:
        key = (binding.field, _norm_value(binding.value), binding.operator)
        if key not in seen:
            seen.add(key)
            out.append(binding)
    return out


def _add_inferred_relationships(
    tables: set[str],
    table_columns: dict[str, list[ColumnInfo]],
    primary_by_table: dict[str, set[str]],
    graph: dict[str, set[str]],
    join_edges: dict[tuple[str, str], list[JoinEdge]],
) -> None:
    table_by_singular = {_singular(table).lower(): table for table in tables}
    for table, columns in table_columns.items():
        for col in columns:
            name = col.name.lower()
            if name == "id" or not name.endswith("id"):
                continue
            stem = name.removesuffix("_id").removesuffix("id").strip("_")
            candidates = {stem, _singular(stem)}
            for candidate in candidates:
                other = table_by_singular.get(candidate)
                if other and other != table:
                    target = _preferred_join_column(other, table_columns, primary_by_table)
                    if target:
                        _add_join_edge(
                            graph,
                            join_edges,
                            left_table=table,
                            left_column=col.name,
                            right_table=other,
                            right_column=target,
                        )
            for other in tables:
                pk_names = primary_by_table.get(other, set()) | {"id", f"{_singular(other).lower()}_id"}
                if name in pk_names and other != table:
                    target = _matching_column(other, name, table_columns) or _preferred_join_column(
                        other, table_columns, primary_by_table
                    )
                    if target:
                        _add_join_edge(
                            graph,
                            join_edges,
                            left_table=table,
                            left_column=col.name,
                            right_table=other,
                            right_column=target,
                        )

    column_index: dict[str, set[str]] = {}
    for table, columns in table_columns.items():
        for col in columns:
            name = col.name.lower()
            if (name != "id" and name.endswith("id")) or name in {
                "uuid",
                "code",
                "setcode",
                "account_id",
                "postid",
            }:
                column_index.setdefault(name, set()).add(table)
    for column_name, related_tables in column_index.items():
        if len(related_tables) > 8:
            continue
        for left in related_tables:
            for right in related_tables:
                if left != right:
                    left_col = _matching_column(left, column_name, table_columns)
                    right_col = _matching_column(right, column_name, table_columns)
                    if left_col and right_col:
                        _add_join_edge(
                            graph,
                            join_edges,
                            left_table=left,
                            left_column=left_col,
                            right_table=right,
                            right_column=right_col,
                        )


def _add_join_edge(
    graph: dict[str, set[str]],
    join_edges: dict[tuple[str, str], list[JoinEdge]],
    *,
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
) -> None:
    if not left_column or not right_column or left_table == right_table:
        return
    graph.setdefault(left_table, set()).add(right_table)
    graph.setdefault(right_table, set()).add(left_table)
    forward = JoinEdge(left_table, left_column, right_table, right_column)
    backward = JoinEdge(right_table, right_column, left_table, left_column)
    _append_edge(join_edges, forward)
    _append_edge(join_edges, backward)


def _append_edge(
    join_edges: dict[tuple[str, str], list[JoinEdge]],
    edge: JoinEdge,
) -> None:
    key = (edge.left_table.lower(), edge.right_table.lower())
    edges = join_edges.setdefault(key, [])
    if edge not in edges:
        edges.append(edge)


def _join_edge_score(edge: JoinEdge) -> int:
    left = edge.left_column.lower()
    right = edge.right_column.lower()
    cols = {left, right}
    score = 0
    if cols == {"setcode", "code"}:
        score += 5
    if "uuid" in cols:
        score += 4
    if any(col in {"mcmid", "mtgoid", "cardkingdomid"} for col in cols):
        score -= 3
    if left == "id" or right == "id":
        score -= 2
    return score


def _preferred_join_column(
    table: str,
    table_columns: dict[str, list[ColumnInfo]],
    primary_by_table: dict[str, set[str]],
) -> str | None:
    primary = primary_by_table.get(table, set())
    for col in table_columns.get(table, []):
        if col.name.lower() in primary:
            return col.name
    preferred = {"id", f"{_singular(table).lower()}_id"}
    for col in table_columns.get(table, []):
        if col.name.lower() in preferred:
            return col.name
    columns = table_columns.get(table, [])
    return columns[0].name if columns else None


def _matching_column(
    table: str,
    column_name: str,
    table_columns: dict[str, list[ColumnInfo]],
) -> str | None:
    for col in table_columns.get(table, []):
        if col.name.lower() == column_name.lower():
            return col.name
    return None


def _column_should_index_values(col: ColumnInfo) -> bool:
    lower_name = col.name.lower()
    lower_type = col.sql_type.lower()
    if any(token in lower_name for token in ["comment", "body", "text", "description"]):
        return False
    if "date" in lower_name:
        return True
    if any(
        token in lower_name
        for token in [
            "name",
            "title",
            "location",
            "type",
            "status",
            "gender",
            "colour",
            "color",
            "language",
            "format",
            "element",
            "rarity",
            "code",
            "uuid",
            "id",
            "label",
        ]
    ):
        return True
    if any(token in lower_type for token in ["char", "text", "clob"]):
        return True
    return lower_type in {"int", "integer", "real", "numeric"}


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _norm_value(value: str) -> str:
    value = value.strip().strip("'\"`")
    value = re.sub(r"\s+", " ", value)
    return value.lower()


def _tokens(value: str) -> list[str]:
    return [
        _singular(token)
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]*|\d+", value.lower())
        if token not in _STOP_WORDS
    ]


def _name_tokens(value: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    spaced = re.sub(r"[^A-Za-z0-9]+", " ", spaced.replace("_", " "))
    return set(_tokens(spaced))


def _singular(value: str) -> str:
    if len(value) > 3 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 4 and value.endswith("oes"):
        return value[:-2]
    if len(value) > 4 and value.endswith(("ches", "shes", "sses", "xes", "zes")):
        return value[:-2]
    if len(value) > 2 and value.endswith("s"):
        return value[:-1]
    return value


_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "by",
    "for",
    "from",
    "has",
    "have",
    "how",
    "in",
    "is",
    "it",
    "list",
    "of",
    "on",
    "or",
    "out",
    "state",
    "the",
    "their",
    "them",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
}
