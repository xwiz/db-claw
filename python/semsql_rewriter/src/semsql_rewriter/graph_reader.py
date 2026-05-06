"""SemanticGraph reader — supplies :class:`ScopeRule` instances to the injector.

The graph file is a SQLite DB written by ``semsql extract``. We read the
``entities`` and ``scopes`` tables directly through ``sqlite3`` (stdlib)
so the rewriter has zero hard dependency on the Rust runtime — pure
Python deployments stay possible.

Schema reference: ``crates/semsql-graph/src/lib.rs``. If the schema
version in the file is newer than ``SUPPORTED_SCHEMA_VERSION`` we refuse
to load — the contract is forward-strict: a runtime that doesn't
understand a feature must not silently ignore it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .injector import ScopeRule

__all__ = [
    "Entity",
    "Field",
    "EnumDef",
    "GraphReadError",
    "GraphSnapshot",
    "SUPPORTED_SCHEMA_VERSION",
    "load_graph",
    "load_scope_rules",
    "schema_version",
]

# Mirror of `semsql_graph::SCHEMA_VERSION` in `crates/semsql-graph/src/lib.rs`.
SUPPORTED_SCHEMA_VERSION = 1


class GraphReadError(RuntimeError):
    """A SemanticGraph file failed to load — corrupt, missing, or too new."""


def schema_version(path: str | Path) -> int:
    """Read the ``schema_version`` row from a `.semsql` file."""
    conn = sqlite3.connect(_uri(path), uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM semsql_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise GraphReadError(f"{path}: missing schema_version metadata row")
        try:
            return int(row[0])
        except (TypeError, ValueError) as e:
            raise GraphReadError(
                f"{path}: schema_version is not an integer: {row[0]!r}"
            ) from e
    finally:
        conn.close()


def load_scope_rules(path: str | Path) -> dict[str, ScopeRule]:
    """Load every entity's mandatory-scope predicates as :class:`ScopeRule`.

    Multiple ``scopes`` rows for one entity are conjoined with ``AND`` so
    every rule fires together at injection time. The injector is
    idempotent across reruns, so this conjunction is safe.

    The schema-version check refuses to load forward-incompatible files —
    callers must upgrade the runtime before the graph file.
    """
    found = schema_version(path)
    if found > SUPPORTED_SCHEMA_VERSION:
        raise GraphReadError(
            f"{path}: schema version {found} is newer than supported {SUPPORTED_SCHEMA_VERSION}"
        )

    rules: dict[str, ScopeRule] = {}
    conn = sqlite3.connect(_uri(path), uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT entity, kind, template, required_params, source_rule FROM scopes"
        ).fetchall()
    finally:
        conn.close()

    bucket: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        bucket.setdefault(r["entity"], []).append(r)

    for entity, ents in bucket.items():
        templates: list[str] = []
        params: list[str] = []
        sources: list[str] = []
        for r in ents:
            tmpl = r["template"]
            if "{{table}}" not in tmpl:
                raise GraphReadError(
                    f"{path}: scope template for {entity!r} missing {{{{table}}}}"
                )
            templates.append(tmpl)
            try:
                params.extend(json.loads(r["required_params"]))
            except (TypeError, ValueError) as e:
                raise GraphReadError(
                    f"{path}: scope required_params for {entity!r} is not a JSON array"
                ) from e
            sources.append(r["source_rule"] or "unspecified")
        rules[entity] = ScopeRule(
            entity=entity,
            template=" AND ".join(f"({t})" for t in templates) if len(templates) > 1 else templates[0],
            required_params=tuple(dict.fromkeys(params)),  # dedupe, keep order
            source_rule=" + ".join(sources),
        )
    return rules


def _uri(path: str | Path) -> str:
    """Return a read-only `file:` URI so we never accidentally mutate the graph."""
    return f"file:{Path(path).resolve()}?mode=ro"


# ---------------------------------------------------------------------------
# typed snapshot — used by the training data generator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    """One row from the ``entities`` table, sans the proto blob."""

    canonical_name: str
    db_table: str
    singular_label: str | None
    plural_label: str | None


@dataclass(frozen=True)
class Field:
    """One row from the ``fields`` table, sans the proto blob."""

    entity: str
    name: str
    db_column: str
    type: str
    display_label: str | None
    enum_canonical: str | None


@dataclass(frozen=True)
class EnumDef:
    """One row from the ``enums`` table.

    ``values`` maps raw DB value (string-encoded, since enums may be int or
    string at the DB level) to the human-readable label.
    """

    canonical_name: str
    values: dict[str, str]


@dataclass(frozen=True)
class GraphSnapshot:
    """Read-only snapshot of a `.semsql` file's domain model."""

    entities: tuple[Entity, ...]
    fields: tuple[Field, ...]
    enums: tuple[EnumDef, ...] = field(default_factory=tuple)

    def fields_for(self, entity: str) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.entity == entity)

    def enum(self, canonical: str) -> EnumDef | None:
        for e in self.enums:
            if e.canonical_name == canonical:
                return e
        return None


def load_graph(path: str | Path) -> GraphSnapshot:
    """Read entities + fields + enums into a typed snapshot.

    Pure read-only — uses the same `mode=ro` URI as the scope-rule loader.
    Schema-version forward-strict.
    """
    found = schema_version(path)
    if found > SUPPORTED_SCHEMA_VERSION:
        raise GraphReadError(
            f"{path}: schema version {found} is newer than supported {SUPPORTED_SCHEMA_VERSION}"
        )

    conn = sqlite3.connect(_uri(path), uri=True)
    try:
        conn.row_factory = sqlite3.Row
        ents = tuple(
            Entity(
                canonical_name=r["canonical_name"],
                db_table=r["db_table"],
                singular_label=r["singular_label"],
                plural_label=r["plural_label"],
            )
            for r in conn.execute(
                "SELECT canonical_name, db_table, singular_label, plural_label FROM entities"
            ).fetchall()
        )
        flds = tuple(
            Field(
                entity=r["entity"],
                name=r["field"],
                db_column=r["db_column"],
                type=r["type"],
                display_label=r["display_label"],
                enum_canonical=r["enum_canonical"],
            )
            for r in conn.execute(
                "SELECT entity, field, db_column, type, display_label, enum_canonical "
                "FROM fields"
            ).fetchall()
        )
        # Enums table may be empty; the proto blob carries the values map so
        # we'd normally decode it here. For v0.2 we keep the schema simple
        # and let extractors emit enum values as JSON for ease of testing.
        # If a `_enum_values_json` column is present we use it; otherwise
        # we surface an empty dict (still a valid snapshot).
        enum_rows: list[EnumDef] = []
        try:
            for r in conn.execute(
                "SELECT canonical_name, _enum_values_json FROM enums"
            ).fetchall():
                values = json.loads(r["_enum_values_json"] or "{}")
                enum_rows.append(EnumDef(r["canonical_name"], values))
        except sqlite3.OperationalError:
            for r in conn.execute("SELECT canonical_name FROM enums").fetchall():
                enum_rows.append(EnumDef(r["canonical_name"], {}))
    finally:
        conn.close()

    return GraphSnapshot(entities=ents, fields=flds, enums=tuple(enum_rows))
