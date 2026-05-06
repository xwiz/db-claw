"""NL → NatSQL template registry.

Each template knows how to expand against a small typed context (the entity
plus a field/operator/value tuple) into a ``(NL, NatSQL)`` pair. Templates
are pure data — adding a new intent means adding a new entry, not writing
new Python.

We deliberately do NOT use Jinja2 here. The substitution surface is small
enough that a tiny tokenless ``str.format`` is faster, has no shell-escape
class of bugs, and produces ASTs we can hash for dedup without parsing.

The ``Template`` shape

    Template(
        intent="filter_eq",
        nl="{verb} {entity} where {field} is {value}",
        natsql="SELECT * FROM {entity} WHERE {field} = {value}",
        applicable=lambda ctx: ctx.operator == "=" and ctx.value is not None,
    )

The generator iterates the cartesian product of all (entity × field ×
operator × value) tuples valid against a SemanticGraph and applies every
template whose ``applicable`` predicate accepts the tuple. Empty
expansions are dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "TemplateContext",
    "Template",
    "TEMPLATES",
    "expand",
]


@dataclass(frozen=True)
class TemplateContext:
    """Context passed to every template's ``applicable`` and rendering."""

    verb: str
    entity: str          # plural label, e.g. "students"
    entity_canonical: str  # canonical name, e.g. "users"
    field: Optional[str] = None       # display label, e.g. "joined date"
    field_canonical: Optional[str] = None  # canonical, e.g. "users.created_at"
    operator: Optional[str] = None    # one of '=' '<' '>' '<=' '>=' '!='
    value: Optional[str] = None       # rendered literal or :param
    aggregate: Optional[str] = None   # COUNT/SUM/AVG/MIN/MAX
    enum_label: Optional[str] = None  # for filter_enum
    enum_raw_value: Optional[str] = None
    limit: Optional[int] = None
    order_dir: Optional[str] = None   # 'ASC' / 'DESC'


@dataclass(frozen=True)
class Template:
    """One NL→NatSQL template."""

    intent: str
    nl: str
    natsql: str
    applicable: Callable[[TemplateContext], bool]

    def render(self, ctx: TemplateContext) -> tuple[str, str]:
        return (self.nl.format(**_fields(ctx)), self.natsql.format(**_fields(ctx)))


def _fields(ctx: TemplateContext) -> dict[str, object]:
    return {
        "verb": ctx.verb,
        "entity": ctx.entity,
        "entity_canonical": ctx.entity_canonical,
        "field": ctx.field or "",
        "field_canonical": ctx.field_canonical or "",
        "operator": ctx.operator or "",
        "value": ctx.value if ctx.value is not None else "",
        "aggregate": ctx.aggregate or "",
        "enum_label": ctx.enum_label or "",
        "enum_raw_value": ctx.enum_raw_value or "",
        "limit": ctx.limit if ctx.limit is not None else "",
        "order_dir": ctx.order_dir or "",
    }


# ---------------------------------------------------------------------------
# template registry
# ---------------------------------------------------------------------------


def _is_filter(ctx: TemplateContext) -> bool:
    return ctx.operator is not None and ctx.value is not None and ctx.field is not None


TEMPLATES: tuple[Template, ...] = (
    # FETCH ---------------------------------------------------------------
    Template(
        intent="fetch_all",
        nl="{verb} {entity}",
        natsql="SELECT * FROM {entity_canonical}",
        applicable=lambda ctx: ctx.field is None and ctx.aggregate is None,
    ),
    # COUNT ---------------------------------------------------------------
    Template(
        intent="count_all",
        nl="{verb} {entity} count",
        natsql="SELECT COUNT(*) FROM {entity_canonical}",
        applicable=lambda ctx: ctx.field is None and ctx.aggregate == "COUNT",
    ),
    Template(
        intent="count_all_nl_alt",
        nl="how many {entity}",
        natsql="SELECT COUNT(*) FROM {entity_canonical}",
        applicable=lambda ctx: ctx.field is None and ctx.aggregate == "COUNT",
    ),
    # FILTER eq -----------------------------------------------------------
    Template(
        intent="filter_eq",
        nl="{verb} {entity} where {field} is {value}",
        natsql="SELECT * FROM {entity_canonical} WHERE {field_canonical} = {value}",
        applicable=lambda ctx: _is_filter(ctx) and ctx.operator == "=",
    ),
    Template(
        intent="filter_eq_nl_alt",
        nl="{entity} with {field} {value}",
        natsql="SELECT * FROM {entity_canonical} WHERE {field_canonical} = {value}",
        applicable=lambda ctx: _is_filter(ctx) and ctx.operator == "=",
    ),
    # FILTER comparison ---------------------------------------------------
    Template(
        intent="filter_gt",
        nl="{verb} {entity} where {field} over {value}",
        natsql="SELECT * FROM {entity_canonical} WHERE {field_canonical} > {value}",
        applicable=lambda ctx: _is_filter(ctx) and ctx.operator == ">",
    ),
    Template(
        intent="filter_lt",
        nl="{verb} {entity} where {field} under {value}",
        natsql="SELECT * FROM {entity_canonical} WHERE {field_canonical} < {value}",
        applicable=lambda ctx: _is_filter(ctx) and ctx.operator == "<",
    ),
    # FILTER enum-aware ---------------------------------------------------
    Template(
        intent="filter_enum_subject",
        nl="{enum_label} {entity}",
        natsql="SELECT * FROM {entity_canonical} WHERE {field_canonical} = {enum_raw_value}",
        applicable=lambda ctx: ctx.enum_label is not None and ctx.enum_raw_value is not None,
    ),
    Template(
        intent="filter_enum_who_are",
        nl="{entity} who are {enum_label}",
        natsql="SELECT * FROM {entity_canonical} WHERE {field_canonical} = {enum_raw_value}",
        applicable=lambda ctx: ctx.enum_label is not None and ctx.enum_raw_value is not None,
    ),
    # AGGREGATE numeric ---------------------------------------------------
    Template(
        intent="aggregate_field",
        nl="{aggregate} of {field} for {entity}",
        natsql="SELECT {aggregate}({field_canonical}) FROM {entity_canonical}",
        applicable=lambda ctx: (
            ctx.aggregate in {"SUM", "AVG", "MIN", "MAX"} and ctx.field is not None
        ),
    ),
    # ORDER BY + LIMIT ----------------------------------------------------
    Template(
        intent="top_n",
        nl="top {limit} {entity} by {field}",
        natsql=(
            "SELECT * FROM {entity_canonical} "
            "ORDER BY {field_canonical} {order_dir} LIMIT {limit}"
        ),
        applicable=lambda ctx: (
            ctx.limit is not None and ctx.order_dir is not None and ctx.field is not None
        ),
    ),
)


def expand(ctx: TemplateContext) -> list[tuple[str, str, str]]:
    """Return ``(intent, nl, natsql)`` for every template that applies to
    ``ctx``."""
    out: list[tuple[str, str, str]] = []
    for t in TEMPLATES:
        if not t.applicable(ctx):
            continue
        nl, sql = t.render(ctx)
        out.append((t.intent, nl, sql))
    return out
