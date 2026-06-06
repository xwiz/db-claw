"""Targeted v0.3 generator — produces examples that exercise the failure
modes the BIRD smoke surfaced.

The earlier v0.2 BIRD diagnostics surfaced these dominant failure
buckets:

  * **Missing JOIN** (71/100). NatSQL v0.2 couldn't model multi-table
    queries; v0.3 lifts that. We need training rows that exercise
    1-, 2-, and 3-INNER-JOIN chains so Stage 2 learns to emit
    ``INNER JOIN ... ON ...`` blocks rather than over-fitting to the
    single-FROM forms in the v0.2 generator.

  * **Missing arithmetic / CAST** (20/100). BIRD eligibility ratios
    require ``CAST(a AS REAL) / b``-style expressions. The v0.3 AST
    represents these as ``SelectItem::Expr(raw)``; the generator
    emits canonical raw-SQL templates so the trainer sees the shape.

  * **Missing HAVING** (1/100). Aggregate predicates over
    ``GROUP BY``. v0.3 supports them; we add explicit templates.

  * **Wrong @val slot filling** (~100/100). Out-of-distribution
    entity names like ``'Alameda'`` and numeric literals like ``400``
    score below stop-words like ``'are'``. We can't fix this here —
    it's a Stage 3 retraining problem — but we *can* surface FK info
    in ``ranked_schema`` so Stage 2 conditions on join structure even
    on single-entity templates.

The output is the same JSONL shape as :func:`generators.generate_skeleton_pairs`
so the existing trainer ingest path round-trips unchanged. Use this in
addition to (not instead of) the template + teacher-cache corpora — it
fills a specific gap, not the whole training distribution.

Example:

    python -m semsql_train generate-targeted-v3 \\
        --graph target/spider_graphs/california_schools.semsql \\
        --paraphrase-variants 4 \\
        --out data/skeleton_train_v3_targeted.jsonl
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from semsql_rewriter.graph_reader import GraphSnapshot, Relationship, load_graph

from .paraphrase import ParaphraseConfig, paraphrase

__all__ = ["TargetedGeneratorConfig", "generate_targeted_v3_pairs"]


@dataclass(frozen=True)
class TargetedGeneratorConfig:
    """Knobs for targeted v0.3 generation."""

    paraphrase_variants: int = 4
    paraphrase_categories: frozenset[str] = frozenset({"verb", "subject"})
    seed: int = 0xCAFEF00D
    """Per-graph deterministic seed."""

    max_join_chain: int = 3
    """Cap on JOIN-chain length (NatSQL v0.3 limit)."""

    include_having: bool = True
    """Emit HAVING-bearing templates (one per filterable numeric field)."""

    include_arithmetic: bool = True
    """Emit ``CAST(a AS REAL) / b`` ratio templates per numeric pair."""

    rows_per_template: int = 1
    """How many literal-bound concretisations per template. Keeping at 1
    by default — paraphrase variants supply the NL diversity, and the
    @val slots are placeholders so concrete values don't matter for the
    skeleton-generator's training objective."""


def generate_targeted_v3_pairs(
    graph_path: str | Path, cfg: TargetedGeneratorConfig
) -> Iterator[dict]:
    """Yield Stage-2 training records targeting v0.3 forms."""
    graph = load_graph(graph_path)
    rng = random.Random(cfg.seed)
    pcfg = ParaphraseConfig(
        enabled=cfg.paraphrase_categories, max_variants=cfg.paraphrase_variants
    )
    yield from _emit_join_templates(graph, cfg, rng, pcfg)
    if cfg.include_having:
        yield from _emit_having_templates(graph, cfg, rng, pcfg)
    if cfg.include_arithmetic:
        yield from _emit_arithmetic_templates(graph, cfg, rng, pcfg)


# ---------------------------------------------------------------------------
# JOIN chain templates
# ---------------------------------------------------------------------------


def _emit_join_templates(
    graph: GraphSnapshot,
    cfg: TargetedGeneratorConfig,
    rng: random.Random,
    pcfg: ParaphraseConfig,
) -> Iterator[dict]:
    """One INNER JOIN per FK edge plus 2- and 3-chain combinations."""
    edges = list(graph.relationships)
    if not edges:
        return

    by_from: dict[str, list[Relationship]] = {}
    by_to: dict[str, list[Relationship]] = {}
    for e in edges:
        by_from.setdefault(e.from_entity, []).append(e)
        by_to.setdefault(e.to_entity, []).append(e)

    # 1-JOIN templates: SELECT a.x, b.y FROM a INNER JOIN b ON ...
    for edge in edges:
        for variant in _join_n(graph, [edge], cfg, rng, pcfg):
            yield variant

    # 2-JOIN chains: a → b → c (via shared b).
    for edge_ab in edges:
        for edge_bc in by_from.get(edge_ab.to_entity, []):
            if edge_bc.to_entity == edge_ab.from_entity:
                continue  # avoid trivial cycles
            chain = [edge_ab, edge_bc]
            for variant in _join_n(graph, chain, cfg, rng, pcfg):
                yield variant
            if cfg.max_join_chain >= 3:
                # 3-JOIN chains: a → b → c → d.
                for edge_cd in by_from.get(edge_bc.to_entity, []):
                    if edge_cd.to_entity in (
                        edge_ab.from_entity,
                        edge_bc.from_entity,
                    ):
                        continue
                    for variant in _join_n(
                        graph, [edge_ab, edge_bc, edge_cd], cfg, rng, pcfg
                    ):
                        yield variant


def _join_n(
    graph: GraphSnapshot,
    chain: list[Relationship],
    cfg: TargetedGeneratorConfig,
    rng: random.Random,
    pcfg: ParaphraseConfig,
) -> Iterator[dict]:
    """Render an INNER-JOIN chain of length ``len(chain)`` as a row."""
    primary = chain[0].from_entity
    select_field = _pick_field(graph, primary, rng, exclude_id=True)
    if select_field is None:
        return

    natsql = (
        "SELECT @field1 FROM @entity1"
    )
    slot_map: dict[str, str] = {
        "@entity1": primary,
        "@field1": f"{primary}.{select_field}",
    }
    ranked: list[dict] = [
        {"kind": "entity", "target": primary, "score": 1.0},
        {"kind": "field", "target": f"{primary}.{select_field}", "score": 1.0},
    ]
    for i, edge in enumerate(chain, start=2):
        natsql += (
            f" INNER JOIN @entity{i} "
            f"ON @entity{i}.{edge.to_field} = @entity{i - 1}.{edge.from_field}"
        )
        if edge.to_entity == primary:
            # Self-join — skip; would collide on @entity tags.
            return
        slot_map[f"@entity{i}"] = edge.to_entity
        # Add the joined entity + its FK fields to ranked_schema.
        ranked.append({"kind": "entity", "target": edge.to_entity, "score": 1.0})
        ranked.append(
            {"kind": "field", "target": f"{edge.to_entity}.{edge.to_field}", "score": 1.0}
        )
        ranked.append(
            {
                "kind": "field",
                "target": f"{edge.from_entity}.{edge.from_field}",
                "score": 1.0,
            }
        )
        ranked.append(
            {
                "kind": "fk",
                "target": (
                    f"{edge.from_entity}.{edge.from_field} = "
                    f"{edge.to_entity}.{edge.to_field}"
                ),
                "score": 1.0,
            }
        )

    nl = _make_join_nl(primary, select_field, [c.to_entity for c in chain])
    for variant in paraphrase(nl, pcfg):
        yield {
            "stage": 2,
            "nl": variant,
            "ranked_schema": ranked,
            "natsql_skeleton": natsql,
            "slot_map": slot_map,
        }


def _make_join_nl(primary: str, field: str, joined: list[str]) -> str:
    pretty = _humanise(field)
    if len(joined) == 1:
        return f"show {pretty} of {primary} joined with {joined[0]}"
    chain = " then ".join(joined)
    return f"show {pretty} of {primary} joined with {chain}"


# ---------------------------------------------------------------------------
# HAVING templates
# ---------------------------------------------------------------------------


def _emit_having_templates(
    graph: GraphSnapshot,
    cfg: TargetedGeneratorConfig,
    rng: random.Random,
    pcfg: ParaphraseConfig,
) -> Iterator[dict]:
    """One HAVING template per (entity, numeric_field, aggregate) tuple."""
    for entity in graph.entities:
        numeric = [
            f.name
            for f in graph.fields_for(entity.canonical_name)
            if _is_numeric(f.type)
        ]
        group_keys = [
            f.name
            for f in graph.fields_for(entity.canonical_name)
            if not _is_numeric(f.type)
        ]
        if not numeric or not group_keys:
            continue
        gk = rng.choice(group_keys)
        for nf in numeric[:2]:  # cap to keep the corpus tight
            for agg in ("COUNT", "SUM", "AVG"):
                natsql = (
                    f"SELECT @field1, {agg}(@field2) FROM @entity1 "
                    f"GROUP BY @field1 HAVING {agg}(@field2) > @val1"
                )
                slot_map = {
                    "@entity1": entity.canonical_name,
                    "@field1": f"{entity.canonical_name}.{gk}",
                    "@field2": f"{entity.canonical_name}.{nf}",
                    "@val1": "0",
                }
                ranked = [
                    {"kind": "entity", "target": entity.canonical_name, "score": 1.0},
                    {
                        "kind": "field",
                        "target": f"{entity.canonical_name}.{gk}",
                        "score": 1.0,
                    },
                    {
                        "kind": "field",
                        "target": f"{entity.canonical_name}.{nf}",
                        "score": 1.0,
                    },
                ]
                # FK info still helps Stage 2 anchor on the active entity.
                for rel in graph.relationships:
                    if rel.from_entity == entity.canonical_name or rel.to_entity == entity.canonical_name:
                        ranked.append(
                            {
                                "kind": "fk",
                                "target": (
                                    f"{rel.from_entity}.{rel.from_field} = "
                                    f"{rel.to_entity}.{rel.to_field}"
                                ),
                                "score": 1.0,
                            }
                        )
                nl = (
                    f"show {_humanise(gk)} where {agg.lower()} of "
                    f"{_humanise(nf)} is positive"
                )
                for variant in paraphrase(nl, pcfg):
                    yield {
                        "stage": 2,
                        "nl": variant,
                        "ranked_schema": ranked,
                        "natsql_skeleton": natsql,
                        "slot_map": slot_map,
                    }


# ---------------------------------------------------------------------------
# Arithmetic / CAST ratio templates (BIRD-style)
# ---------------------------------------------------------------------------


def _emit_arithmetic_templates(
    graph: GraphSnapshot,
    cfg: TargetedGeneratorConfig,
    rng: random.Random,
    pcfg: ParaphraseConfig,
) -> Iterator[dict]:
    """Emit ``CAST(a AS REAL) / b`` ratio templates per numeric pair."""
    for entity in graph.entities:
        numeric = [
            f.name
            for f in graph.fields_for(entity.canonical_name)
            if _is_numeric(f.type)
        ]
        if len(numeric) < 2:
            continue
        # Pick up to 3 (numerator, denominator) pairs deterministically.
        pairs: list[tuple[str, str]] = []
        for a in numeric:
            for b in numeric:
                if a != b:
                    pairs.append((a, b))
                if len(pairs) >= 3:
                    break
            if len(pairs) >= 3:
                break
        for a, b in pairs:
            natsql = (
                "SELECT CAST(@field1 AS REAL) / @field2 FROM @entity1"
            )
            slot_map = {
                "@entity1": entity.canonical_name,
                "@field1": f"{entity.canonical_name}.{a}",
                "@field2": f"{entity.canonical_name}.{b}",
            }
            ranked = [
                {"kind": "entity", "target": entity.canonical_name, "score": 1.0},
                {
                    "kind": "field",
                    "target": f"{entity.canonical_name}.{a}",
                    "score": 1.0,
                },
                {
                    "kind": "field",
                    "target": f"{entity.canonical_name}.{b}",
                    "score": 1.0,
                },
            ]
            nl = (
                f"compute the ratio of {_humanise(a)} to {_humanise(b)} "
                f"per {entity.canonical_name}"
            )
            for variant in paraphrase(nl, pcfg):
                yield {
                    "stage": 2,
                    "nl": variant,
                    "ranked_schema": ranked,
                    "natsql_skeleton": natsql,
                    "slot_map": slot_map,
                }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_NUMERIC_TYPES = {
    "int", "integer", "bigint", "smallint", "tinyint",
    "real", "float", "double", "decimal", "numeric",
}


def _is_numeric(sql_type: str | None) -> bool:
    if not sql_type:
        return False
    base = sql_type.split("(", 1)[0].strip().lower()
    return base in _NUMERIC_TYPES


def _pick_field(
    graph: GraphSnapshot, entity: str, rng: random.Random, exclude_id: bool = False
) -> str | None:
    fields = list(graph.fields_for(entity))
    if exclude_id:
        fields = [f for f in fields if not f.name.endswith("_id") and f.name != "id"]
    if not fields:
        return None
    return rng.choice(fields).name


def _humanise(field: str) -> str:
    return field.replace("_", " ")
