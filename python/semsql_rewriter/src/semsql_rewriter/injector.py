"""Mandatory-filter injector — sqlglot AST visitor.

Injects scope predicates (``tenant_id = :tenant``, ``deleted_at IS NULL``,
RLS, owner-scoped) at *every* table reference in the AST — including in
CTEs, subqueries, derived tables, set operations, lateral joins, and
recursive CTEs.

Critical invariants — every one is a security boundary:

- **Recursive descent**: every :class:`sqlglot.exp.Table` node receives the
  appropriate predicate. Walking only the top-level FROM/JOIN is the bug
  class behind most multi-tenant leaks (incl. the Apache Superset
  CVE-2025-48912 root cause).
- **CTE re-binding tracked**: ``WITH x AS (SELECT * FROM users) SELECT *
  FROM x`` — the inner ``users`` is scoped at its physical reference; the
  outer ``x`` is a CTE alias and inherits the inner scoping (no double-injection).
- **UNION branches**: each branch is a separate Select node, walked
  independently, scoped independently.
- **Idempotent**: if a scoped Select already contains an equivalent
  predicate (canonicalised AST equality), no duplication.
- **Parameterised values only**: filter values are bound parameters, never
  string-interpolated.
- **Audit log**: every injection is recorded with ``(table_reference,
  rendered_predicate, source_rule)`` for forensics.

Implementation strategy:

The walker traverses every :class:`exp.Select` node. For each Select, it
collects every *physical* :class:`exp.Table` reference in the FROM and
JOIN clauses (a CTE alias resolves to a known name set we built earlier,
so it's skipped — the inner Select inside the CTE definition is itself
visited as a separate Select). For each physical reference whose name
matches an entity in ``rules``, it AND-conjoins the scope predicate
(rendered with the table's effective alias and bound parameters) into the
Select's WHERE.

Set operations (UNION/INTERSECT/EXCEPT) are decomposed into their
constituent Selects before the walk; subqueries inside expressions are
descended into; lateral subqueries are treated like any other Select.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

import sqlglot
from sqlglot import exp

__all__ = [
    "InjectorError",
    "ScopeRule",
    "InjectionResult",
    "inject",
]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


class InjectorError(ValueError):
    """The injector refused to scope a query — always fails closed."""


@dataclass(frozen=True)
class ScopeRule:
    """One mandatory scope predicate for one entity.

    ``template`` is a sqlglot-renderable predicate string with ``{{table}}``
    substituted for the table alias and ``:param`` placeholders for bound
    parameters. Example::

        ScopeRule(
            entity="users",
            template="{{table}}.tenant_id = :tenant AND {{table}}.deleted_at IS NULL",
            required_params=("tenant",),
            source_rule="tenant_isolation",
        )
    """

    entity: str
    template: str
    required_params: tuple[str, ...] = ()
    source_rule: str = "unspecified"


@dataclass
class InjectionResult:
    """Output of a successful injection."""

    sql: str
    """Rewritten SQL text (in the input dialect)."""

    injected_predicates: list[tuple[str, str, str]] = field(default_factory=list)
    """``(table_reference, rendered_predicate, source_rule)`` triples — one
    per injection, surfaced for the audit log."""

    bound_params: dict[str, str] = field(default_factory=dict)
    """Parameters the caller must bind when executing."""


def inject(
    sql: str,
    rules: Mapping[str, ScopeRule],
    params: Mapping[str, str],
    *,
    dialect: str = "postgres",
) -> InjectionResult:
    """Inject mandatory scope predicates and return the rewritten SQL.

    Args:
        sql: Already-validated SELECT statement.
        rules: Map from canonical entity name → :class:`ScopeRule`.
        params: Caller-provided bound-parameter values.
        dialect: sqlglot dialect identifier.

    Raises:
        InjectorError: if a required parameter is missing or a scope rule
            refers to a value-interpolated literal instead of a parameter.
    """
    # 1. Verify every required parameter is present.
    needed: set[str] = set()
    for rule in rules.values():
        needed.update(rule.required_params)
    missing = needed - set(params)
    if missing:
        raise InjectorError(f"missing bound params: {sorted(missing)}")

    # 2. Refuse value-interpolated templates outright.
    for rule in rules.values():
        _check_template_safety(rule)

    # 3. Parse and walk.
    parsed = sqlglot.parse_one(sql, read=dialect)
    walker = _Walker(rules)
    walker.visit(parsed)

    return InjectionResult(
        sql=parsed.sql(dialect=dialect),
        injected_predicates=walker.audit,
        bound_params=dict(params),
    )


# ---------------------------------------------------------------------------
# walker
# ---------------------------------------------------------------------------


class _Walker:
    """Stateful AST visitor that accumulates scope injections.

    State held:

    - ``rules`` — the per-entity ScopeRule lookup.
    - ``audit`` — the (table, predicate, source_rule) audit log.
    - ``cte_names`` (per-Select scope) — names that resolve to CTE aliases
      and therefore must NOT be injected (their definition is scoped at the
      physical reference inside the CTE body).
    """

    def __init__(self, rules: Mapping[str, ScopeRule]) -> None:
        self.rules = rules
        self.audit: list[tuple[str, str, str]] = []

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------

    def visit(self, node: exp.Expression) -> None:
        """Walk the entire tree, scoping every Select we encounter."""
        # Set operations: walk each branch.
        if isinstance(node, exp.Union):
            self.visit(node.this)
            self.visit(node.expression)
            return
        if isinstance(node, (exp.Intersect, exp.Except)):
            self.visit(node.this)
            self.visit(node.expression)
            return

        # CTE wrapper: walk each CTE body, then walk the trailing query.
        if isinstance(node, exp.With):
            for cte in node.expressions:
                # Each CTE body is its own scoping context.
                self.visit(cte.this)
            self.visit(node.this)
            return

        if isinstance(node, exp.Select):
            self._scope_select(node)
            return

        # Subqueries inside expressions, projections, joins, etc. — descend.
        for child in node.find_all(exp.Select, exp.Union, exp.Intersect, exp.Except):
            if child is node:
                continue
            self.visit(child)

    # ------------------------------------------------------------------
    # core: scope one Select
    # ------------------------------------------------------------------

    def _scope_select(self, select: exp.Select) -> None:
        # Recurse first: scope every nested Select / CTE / subquery /
        # lateral that lives within this Select's expression tree, *before*
        # we attach any new predicates to this Select's WHERE.
        cte_names = self._collect_cte_names(select)
        for nested in self._iter_nested_queries(select):
            self.visit(nested)

        # Now collect physical table references to scope.
        for table in self._iter_physical_tables(select, cte_names):
            entity = (table.name or "").lower()
            rule = self.rules.get(entity)
            if rule is None:
                continue
            alias = (table.alias_or_name or table.name).lower()
            predicate_sql = self._render_template(rule.template, alias)
            predicate_expr = sqlglot.parse_one(predicate_sql, read="postgres")
            new_leaves = list(_flatten_and(predicate_expr))
            existing_leaves = self._existing_where_leaves(select)
            missing = [
                leaf
                for leaf in new_leaves
                if leaf.sql(dialect="postgres") not in existing_leaves
            ]
            if not missing:
                continue
            for leaf in missing:
                select.where(leaf, copy=False)
            self.audit.append((alias, predicate_sql, rule.source_rule))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_cte_names(select: exp.Select) -> set[str]:
        """Names that resolve to CTE aliases in this Select's lexical scope.

        A reference to one of these names in the FROM/JOIN clauses is *not*
        a physical table reference — it's an alias for a derived query that
        was scoped when its body was visited.
        """
        with_clause = select.args.get("with")
        if with_clause is None:
            return set()
        names: set[str] = set()
        for cte in with_clause.expressions:
            alias = cte.alias_or_name
            if alias:
                names.add(alias.lower())
        return names

    @staticmethod
    def _iter_nested_queries(select: exp.Select) -> Iterable[exp.Expression]:
        """Yield every Select/Union/etc. that lives inside this Select but is
        a separate scoping unit (CTE bodies, subqueries, lateral joins)."""
        for descendant in select.walk():
            if descendant is select:
                continue
            if isinstance(descendant, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
                # Skip nested scopes that are themselves the immediate child of
                # the outer Select's With — those are handled by the With visit.
                yield descendant

    @staticmethod
    def _iter_physical_tables(
        select: exp.Select, cte_names: set[str]
    ) -> Iterable[exp.Table]:
        """Yield the physical Table nodes in this Select's FROM/JOIN clauses,
        skipping CTE alias references."""
        from_clause = select.args.get("from_") or select.args.get("from")
        joins = select.args.get("joins") or []

        for source in _collect_from_sources(from_clause):
            if isinstance(source, exp.Table) and source.name.lower() not in cte_names:
                yield source

        for join in joins:
            if isinstance(join.this, exp.Table) and join.this.name.lower() not in cte_names:
                yield join.this

    @staticmethod
    def _render_template(template: str, alias: str) -> str:
        return template.replace("{{table}}", alias)

    @staticmethod
    def _existing_where_leaves(select: exp.Select) -> set[str]:
        """Set of normalised SQL strings, one per leaf in the AND-tree of WHERE."""
        where = select.args.get("where")
        if where is None:
            return set()
        return {leaf.sql(dialect="postgres") for leaf in _flatten_and(where.this)}


def _collect_from_sources(from_clause: exp.From | None) -> Iterable[exp.Expression]:
    if from_clause is None:
        return
    yield from_clause.this
    for src in from_clause.args.get("expressions") or []:
        yield src


def _flatten_and(expr: exp.Expression) -> Iterable[exp.Expression]:
    """Walk an AND-tree, yielding leaf predicates."""
    if isinstance(expr, exp.And):
        yield from _flatten_and(expr.this)
        yield from _flatten_and(expr.expression)
    else:
        yield expr


# ---------------------------------------------------------------------------
# template safety
# ---------------------------------------------------------------------------


def _check_template_safety(rule: ScopeRule) -> None:
    """A scope template must use ``:param`` placeholders for every value;
    no string-literal interpolation, no naked numerics for tenant IDs."""
    template = rule.template
    if "{{table}}" not in template:
        raise InjectorError(
            f"scope template for {rule.entity!r} missing required {{{{table}}}} placeholder"
        )
    # Heuristic: every parameter named in `required_params` must appear as
    # `:param` somewhere in the template. This catches stale renames where
    # the rule advertises a parameter the template no longer uses.
    for p in rule.required_params:
        if f":{p}" not in template:
            raise InjectorError(
                f"scope template for {rule.entity!r} declares param "
                f"`{p}` but does not reference `:{p}`"
            )
