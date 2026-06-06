"""Per-stage data generator surface.

Walks a SemanticGraph and emits training records for each cascade stage:

- :func:`generate_linker_pairs`  — Stage 1 ``(NL, schema_item, label)`` triples.
- :func:`generate_skeleton_pairs` — Stage 2 ``(NL + ranked_schema, NatSQL_skeleton)``.
- :func:`generate_slot_pairs`     — Stage 3 ``(NL + skeleton + candidates, correct)``.
- :func:`generate_e2e_pairs`      — full NL → NatSQL pairs (eval-only).

Generation is deterministic given a seed. Templates + paraphrase rules are
the only source of variation; we never invoke an LLM in this loop.

Records are returned as plain dicts so the writer can serialise to either
JSONL (default, hackable) or Parquet (production, columnar). The protobuf
shape lives in ``schemas/training_pair.proto`` — the dict keys mirror the
proto field names so the serialiser can do a 1:1 mapping.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from semsql_rewriter.graph_reader import Field, GraphSnapshot, load_graph

from .paraphrase import ParaphraseConfig, paraphrase
from .templates import TemplateContext, expand

__all__ = [
    "GeneratorConfig",
    "generate_e2e_pairs",
    "generate_linker_pairs",
    "generate_skeleton_pairs",
    "generate_slot_pairs",
]


@dataclass(frozen=True)
class GeneratorConfig:
    """Knobs for the combinatorial expansion."""

    paraphrase_variants: int = 4
    paraphrase_categories: frozenset[str] = frozenset({"verb", "subject"})
    include_hard_negatives: bool = True
    seed: int = 0xCAFEF00D
    """Global seed; per-NL seeds are derived deterministically so re-runs
    produce stable training sets."""


# ---------------------------------------------------------------------------
# entry points: from a graph file
# ---------------------------------------------------------------------------


def generate_linker_pairs(graph_path: str, cfg: GeneratorConfig) -> Iterator[dict]:
    yield from _generate_linker(load_graph(graph_path), cfg)


def generate_skeleton_pairs(graph_path: str, cfg: GeneratorConfig) -> Iterator[dict]:
    yield from _generate_skeleton(load_graph(graph_path), cfg)


def generate_slot_pairs(graph_path: str, cfg: GeneratorConfig) -> Iterator[dict]:
    yield from _generate_slot(load_graph(graph_path), cfg)


def generate_e2e_pairs(graph_path: str, cfg: GeneratorConfig) -> Iterator[dict]:
    yield from _generate_e2e(load_graph(graph_path), cfg)


# ---------------------------------------------------------------------------
# core walker — yields concrete (NL, NatSQL, ctx) tuples used by every stage
# ---------------------------------------------------------------------------


def _walk(graph: GraphSnapshot) -> Iterator[tuple[str, str, TemplateContext]]:
    """Cartesian walk over (entity × field × operator × value × enum × ...)."""
    for entity in graph.entities:
        plural = entity.plural_label or entity.canonical_name
        canonical_e = entity.canonical_name

        # 1. Bare fetch / count
        ctx = TemplateContext(verb="show", entity=plural, entity_canonical=canonical_e)
        yield from ((nl, sql, ctx) for _, nl, sql in expand(ctx))
        for agg in ("COUNT",):
            ctx_c = TemplateContext(
                verb="show", entity=plural, entity_canonical=canonical_e, aggregate=agg
            )
            yield from ((nl, sql, ctx_c) for _, nl, sql in expand(ctx_c))

        for fld in graph.fields_for(canonical_e):
            field_label = fld.display_label or fld.name
            field_canonical = f"{canonical_e}.{fld.name}"

            # 2. Filters
            for op, val in _filter_value_grid(fld):
                ctx_f = TemplateContext(
                    verb="show",
                    entity=plural,
                    entity_canonical=canonical_e,
                    field=field_label,
                    field_canonical=field_canonical,
                    operator=op,
                    value=val,
                )
                yield from ((nl, sql, ctx_f) for _, nl, sql in expand(ctx_f))

            # 3. Enum filters
            if fld.enum_canonical is not None:
                enum = graph.enum(fld.enum_canonical)
                if enum is not None:
                    for raw, label in enum.values.items():
                        ctx_e = TemplateContext(
                            verb="show",
                            entity=plural,
                            entity_canonical=canonical_e,
                            field=field_label,
                            field_canonical=field_canonical,
                            enum_label=label.lower(),
                            enum_raw_value=raw,
                        )
                        yield from ((nl, sql, ctx_e) for _, nl, sql in expand(ctx_e))

            # 4. Aggregates
            if fld.type.lower() in {"integer", "bigint", "decimal", "float"}:
                for agg in ("SUM", "AVG", "MIN", "MAX"):
                    ctx_a = TemplateContext(
                        verb="show",
                        entity=plural,
                        entity_canonical=canonical_e,
                        field=field_label,
                        field_canonical=field_canonical,
                        aggregate=agg,
                    )
                    yield from ((nl, sql, ctx_a) for _, nl, sql in expand(ctx_a))

            # 5. ORDER BY + LIMIT (top-N pattern; useful for intent hints)
            for order_dir in ("DESC", "ASC"):
                ctx_o = TemplateContext(
                    verb="show",
                    entity=plural,
                    entity_canonical=canonical_e,
                    field=field_label,
                    field_canonical=field_canonical,
                    limit=10,
                    order_dir=order_dir,
                )
                yield from ((nl, sql, ctx_o) for _, nl, sql in expand(ctx_o))


def _filter_value_grid(fld: Field) -> Iterator[tuple[str, str]]:
    """Operator/value tuples appropriate for the field's type."""
    t = fld.type.lower()
    if t in {"integer", "bigint", "decimal", "float"}:
        for op in ("=", ">", "<"):
            yield op, ":n"
    elif t in {"date", "timestamp"}:
        for op in ("=", ">", "<"):
            yield op, ":d"
    elif t in {"string", "text"}:
        yield "=", ":s"
    elif t == "boolean":
        for v in (":b", "true", "false"):
            yield "=", v


# ---------------------------------------------------------------------------
# stage 1 — linker
# ---------------------------------------------------------------------------


def _generate_linker(graph: GraphSnapshot, cfg: GeneratorConfig) -> Iterator[dict]:
    pcfg = ParaphraseConfig(
        enabled=cfg.paraphrase_categories, max_variants=cfg.paraphrase_variants
    )
    for nl, sql, ctx in _walk(graph):
        for variant in paraphrase(nl, pcfg):
            # Positive: the entity used in the SQL is relevant.
            yield _linker_record(variant, "entity", ctx.entity_canonical, 1.0)
            # Positive: the field used in the SQL is relevant (if any).
            if ctx.field_canonical:
                yield _linker_record(variant, "field", ctx.field_canonical, 1.0)
            # Hard negatives: same column name on a different entity.
            if cfg.include_hard_negatives and ctx.field_canonical:
                fld_seg = ctx.field_canonical.split(".", 1)[1]
                for other in graph.entities:
                    if other.canonical_name == ctx.entity_canonical:
                        continue
                    twin = next(
                        (
                            f
                            for f in graph.fields_for(other.canonical_name)
                            if f.name == fld_seg
                        ),
                        None,
                    )
                    if twin is not None:
                        yield _linker_record(
                            variant, "field", f"{other.canonical_name}.{twin.name}", 0.0
                        )


def _linker_record(nl: str, kind: str, target: str, label: float) -> dict:
    return {
        "stage": 1,
        "nl": nl,
        "candidate_kind": kind,
        "candidate_target": target,
        "relevance_label": label,
        "is_hard_negative": label == 0.0,
    }


# ---------------------------------------------------------------------------
# stage 2 — skeleton
# ---------------------------------------------------------------------------


def _generate_skeleton(graph: GraphSnapshot, cfg: GeneratorConfig) -> Iterator[dict]:
    pcfg = ParaphraseConfig(
        enabled=cfg.paraphrase_categories, max_variants=cfg.paraphrase_variants
    )
    for nl, sql, ctx in _walk(graph):
        skeleton, slot_map = _to_skeleton(sql, ctx)
        for variant in paraphrase(nl, pcfg):
            yield {
                "stage": 2,
                "nl": variant,
                "ranked_schema": _ranked_schema(graph, ctx),
                "natsql_skeleton": skeleton,
                "slot_map": slot_map,
            }


def _to_skeleton(sql: str, ctx: TemplateContext) -> tuple[str, dict[str, str]]:
    """Replace concrete identifiers with @entityN/@fieldN/@valN placeholders.

    Substitution order matters: longer strings before shorter ones so e.g.
    ``users.created_at`` doesn't get half-replaced before ``users``.
    """
    skeleton = sql
    slot_map: dict[str, str] = {}

    if ctx.field_canonical:
        skeleton = skeleton.replace(ctx.field_canonical, "@field1")
        slot_map["@field1"] = ctx.field_canonical
    skeleton = skeleton.replace(ctx.entity_canonical, "@entity1")
    slot_map["@entity1"] = ctx.entity_canonical
    if ctx.value is not None and ctx.value != "":
        skeleton = skeleton.replace(ctx.value, "@val1")
        slot_map["@val1"] = ctx.value
    if ctx.enum_raw_value is not None:
        skeleton = skeleton.replace(ctx.enum_raw_value, "@val1")
        slot_map["@val1"] = ctx.enum_raw_value
    return skeleton, slot_map


def _ranked_schema(graph: GraphSnapshot, ctx: TemplateContext) -> list[dict]:
    """Schema slice the linker would have emitted for this query.

    v0.3 / Phase C: includes ``kind == "fk"`` entries for every FK edge
    incident on the active entity. The Stage 2 encoder renders these as
    ``FK: a.id = b.a_id`` lines so the model sees join structure even
    on single-entity templates (matching the teacher-cache rows that
    carry full JOIN ON edges from gold SQL).
    """
    out: list[dict] = []
    out.append({"kind": "entity", "target": ctx.entity_canonical, "score": 1.0})
    if ctx.field_canonical:
        out.append({"kind": "field", "target": ctx.field_canonical, "score": 1.0})
    for rel in graph.relationships:
        if (
            rel.from_entity == ctx.entity_canonical
            or rel.to_entity == ctx.entity_canonical
        ):
            edge = (
                f"{rel.from_entity}.{rel.from_field} = "
                f"{rel.to_entity}.{rel.to_field}"
            )
            out.append({"kind": "fk", "target": edge, "score": 1.0})
    return out


# ---------------------------------------------------------------------------
# stage 3 — slot filler
# ---------------------------------------------------------------------------


def _generate_slot(graph: GraphSnapshot, cfg: GeneratorConfig) -> Iterator[dict]:
    pcfg = ParaphraseConfig(
        enabled=cfg.paraphrase_categories, max_variants=cfg.paraphrase_variants
    )
    for nl, sql, ctx in _walk(graph):
        skeleton, slot_map = _to_skeleton(sql, ctx)
        for variant in paraphrase(nl, pcfg):
            for slot, correct in slot_map.items():
                candidates = _candidates_for(slot, correct, graph, ctx)
                if not candidates:
                    continue
                yield {
                    "stage": 3,
                    "nl": variant,
                    "skeleton": skeleton,
                    "slot_name": slot,
                    "candidates": candidates,
                    "correct_index": candidates.index(correct),
                }


def _candidates_for(
    slot: str, correct: str, graph: GraphSnapshot, ctx: TemplateContext
) -> list[str]:
    """Generate a tight candidate set per slot — correct first, then plausible distractors."""
    if slot == "@entity1":
        opts = [correct] + [
            e.canonical_name for e in graph.entities if e.canonical_name != correct
        ]
        return opts[:5]
    if slot == "@field1":
        opts = [correct]
        # Distractors: same column-segment on other entities.
        seg = correct.split(".", 1)[1]
        for other in graph.entities:
            if other.canonical_name == ctx.entity_canonical:
                continue
            twin = next(
                (
                    f
                    for f in graph.fields_for(other.canonical_name)
                    if f.name == seg
                ),
                None,
            )
            if twin is not None:
                opts.append(f"{other.canonical_name}.{twin.name}")
        # Plus other fields on the same entity for shape.
        for f in graph.fields_for(ctx.entity_canonical):
            cand = f"{ctx.entity_canonical}.{f.name}"
            if cand != correct:
                opts.append(cand)
        return opts[:8]
    if slot == "@val1":
        # Values are typically literals or :param names — pass through.
        return [correct, ":n", ":s", ":d", "1", "0"][:5]
    return []


# ---------------------------------------------------------------------------
# stage e2e — eval-only
# ---------------------------------------------------------------------------


def _generate_e2e(graph: GraphSnapshot, cfg: GeneratorConfig) -> Iterator[dict]:
    for nl, sql, _ctx in _walk(graph):
        yield {
            "stage": "e2e",
            "nl": nl,
            "natsql": sql,
        }
