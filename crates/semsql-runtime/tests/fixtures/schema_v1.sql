-- Schema mirror of `semsql_graph::SCHEMA_V1_SQL` plus the optional
-- `_enum_values_json` column the runtime test fixture seeds with concrete
-- enum data.

CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE entities (
    canonical_name TEXT PRIMARY KEY,
    db_table       TEXT NOT NULL,
    db_schema      TEXT,
    singular_label TEXT,
    plural_label   TEXT,
    proto_blob     BLOB NOT NULL
);

CREATE TABLE fields (
    entity         TEXT NOT NULL REFERENCES entities(canonical_name),
    field          TEXT NOT NULL,
    db_column      TEXT NOT NULL,
    type           TEXT NOT NULL,
    display_label  TEXT,
    enum_canonical TEXT,
    unit_canonical TEXT,
    proto_blob     BLOB NOT NULL,
    PRIMARY KEY (entity, field)
);

CREATE TABLE relationships (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity   TEXT NOT NULL REFERENCES entities(canonical_name),
    from_field    TEXT NOT NULL,
    to_entity     TEXT NOT NULL REFERENCES entities(canonical_name),
    to_field      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    relation_name TEXT,
    proto_blob    BLOB NOT NULL
);

CREATE TABLE enums (
    canonical_name    TEXT PRIMARY KEY,
    proto_blob        BLOB NOT NULL DEFAULT X'',
    _enum_values_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE units (
    canonical_name TEXT PRIMARY KEY,
    storage_unit   TEXT NOT NULL,
    display_unit   TEXT NOT NULL,
    factor         REAL NOT NULL
);

CREATE TABLE vocabulary (
    term            TEXT NOT NULL,
    canonical_kind  TEXT NOT NULL CHECK (canonical_kind IN ('entity','field','enum_value','relationship')),
    canonical_value TEXT NOT NULL,
    confidence      REAL NOT NULL,
    source_layer    INTEGER NOT NULL,
    source_locator  TEXT,
    PRIMARY KEY (term, canonical_kind, canonical_value)
);

CREATE TABLE scopes (
    entity          TEXT NOT NULL REFERENCES entities(canonical_name),
    kind            TEXT NOT NULL,
    template        TEXT NOT NULL,
    required_params TEXT NOT NULL,
    source_rule     TEXT,
    PRIMARY KEY (entity, kind, template)
);

CREATE TABLE sample_values (
    field_canonical TEXT PRIMARY KEY,
    examples        TEXT NOT NULL,
    pii_redacted    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE conflict_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_target   TEXT NOT NULL,
    candidates         TEXT NOT NULL,
    resolution         TEXT NOT NULL,
    suggested_override TEXT
);
