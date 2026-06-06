//! SQLite-backed SemanticGraph store.
//!
//! The graph is held in a single `.semsql` file (a SQLite DB) that travels
//! with the application. Indexed lookups, ACID, `git diff`-friendly via
//! `sqlite-utils dump`. Every row carries a `source_locator` JSON column so
//! provenance is one query away.
//!
//! - [`open`] / [`migrate`] handle file lifecycle.
//! - [`read`] exposes typed read-only views the runtime consumes.
//! - [`write`] exposes typed inserts the extractors use.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod read;
pub mod write;

use rusqlite::{params, Connection};
use semsql_core::{Result, SemsqlError};
use std::path::Path;

/// Latest schema version this build understands. Stored in the
/// `semsql_metadata.schema_version` row inside the SQLite file.
pub const SCHEMA_VERSION: u32 = 5;

/// Open or create a `.semsql` graph file. Migrations run on open.
pub fn open<P: AsRef<Path>>(path: P) -> Result<Connection> {
    let conn = Connection::open(path).map_err(|e| SemsqlError::Other(e.to_string()))?;
    conn.pragma_update(None, "journal_mode", "wal")
        .map_err(|e| SemsqlError::Other(e.to_string()))?;
    conn.pragma_update(None, "foreign_keys", "ON")
        .map_err(|e| SemsqlError::Other(e.to_string()))?;
    migrate(&conn)?;
    Ok(conn)
}

/// Run schema migrations to bring the file up to `SCHEMA_VERSION`.
///
/// Migrations are additive only — each step appends; no destructive renames.
/// If a future version drops a column, write a transition step that copies
/// data into a new column rather than dropping in place.
pub fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(SCHEMA_V1_SQL)
        .map_err(|e| SemsqlError::Other(format!("migration v1 failed: {e}")))?;

    let current: u32 = conn
        .query_row(
            "SELECT value FROM semsql_metadata WHERE key = 'schema_version'",
            [],
            |row| row.get::<_, String>(0),
        )
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);

    if current > SCHEMA_VERSION {
        return Err(SemsqlError::SchemaVersionMismatch {
            found: current,
            supported: SCHEMA_VERSION,
        });
    }

    if current < 2 {
        migrate_v2_scope_predicate_vocabulary(conn)?;
    }
    if current < 3 {
        migrate_v3_metric_definitions(conn)?;
    }
    if current < 4 {
        migrate_v4_metric_aggregate_columns(conn)?;
    }
    if current < 5 {
        migrate_v5_metric_distinct_column(conn)?;
    }

    if current < SCHEMA_VERSION {
        conn.execute(
            "INSERT OR REPLACE INTO semsql_metadata (key, value) VALUES ('schema_version', ?1)",
            params![SCHEMA_VERSION.to_string()],
        )
        .map_err(|e| SemsqlError::Other(e.to_string()))?;
    }

    Ok(())
}

fn migrate_v2_scope_predicate_vocabulary(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
CREATE TABLE IF NOT EXISTS vocabulary_v2 (
    term             TEXT NOT NULL,
    canonical_kind   TEXT NOT NULL CHECK (canonical_kind IN ('entity','field','enum_value','relationship','scope_predicate')),
    canonical_value  TEXT NOT NULL,
    confidence       REAL NOT NULL,
    source_layer     INTEGER NOT NULL,
    source_locator   TEXT,
    PRIMARY KEY (term, canonical_kind, canonical_value)
);
INSERT OR IGNORE INTO vocabulary_v2
    (term, canonical_kind, canonical_value, confidence, source_layer, source_locator)
SELECT term, canonical_kind, canonical_value, confidence, source_layer, source_locator
FROM vocabulary;
DROP TABLE vocabulary;
ALTER TABLE vocabulary_v2 RENAME TO vocabulary;
CREATE INDEX IF NOT EXISTS vocab_by_term      ON vocabulary(term);
CREATE INDEX IF NOT EXISTS vocab_by_canonical ON vocabulary(canonical_kind, canonical_value);
"#,
    )
    .map_err(|e| SemsqlError::Other(format!("migration v2 failed: {e}")))?;
    Ok(())
}

fn migrate_v3_metric_definitions(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
CREATE TABLE IF NOT EXISTS metric_definitions (
    name                   TEXT PRIMARY KEY,
    display_label          TEXT,
    metric_kind            TEXT NOT NULL,
    subject_entity         TEXT NOT NULL REFERENCES entities(canonical_name),
    numerator_field        TEXT NOT NULL,
    numerator_operator     TEXT NOT NULL,
    numerator_value        TEXT NOT NULL,
    numerator_value_kind   TEXT NOT NULL,
    denominator_field      TEXT NOT NULL,
    scale                  REAL NOT NULL,
    required_entities_json TEXT NOT NULL,
    aliases_json           TEXT NOT NULL,
    source_locator         TEXT
);
CREATE INDEX IF NOT EXISTS metric_definitions_by_subject
ON metric_definitions(subject_entity);
"#,
    )
    .map_err(|e| SemsqlError::Other(format!("migration v3 failed: {e}")))?;
    Ok(())
}

fn migrate_v4_metric_aggregate_columns(conn: &Connection) -> Result<()> {
    add_column_if_missing(
        conn,
        "metric_definitions",
        "measure_field",
        "TEXT",
        "migration v4 measure_field",
    )?;
    add_column_if_missing(
        conn,
        "metric_definitions",
        "aggregate",
        "TEXT",
        "migration v4 aggregate",
    )?;
    Ok(())
}

fn migrate_v5_metric_distinct_column(conn: &Connection) -> Result<()> {
    add_column_if_missing(
        conn,
        "metric_definitions",
        "distinct_measure",
        "INTEGER NOT NULL DEFAULT 0",
        "migration v5 distinct_measure",
    )?;
    Ok(())
}

fn add_column_if_missing(
    conn: &Connection,
    table: &str,
    column: &str,
    definition: &str,
    context: &str,
) -> Result<()> {
    let exists: bool = conn
        .prepare(&format!(
            "SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?1"
        ))
        .map_err(|e| SemsqlError::Other(format!("{context} probe prepare: {e}")))?
        .exists([column])
        .map_err(|e| SemsqlError::Other(format!("{context} probe: {e}")))?;
    if exists {
        return Ok(());
    }
    conn.execute(
        &format!("ALTER TABLE {table} ADD COLUMN {column} {definition}"),
        [],
    )
    .map_err(|e| SemsqlError::Other(format!("{context} alter: {e}")))?;
    Ok(())
}

const SCHEMA_V1_SQL: &str = r#"
-- Bookkeeping ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS semsql_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Entities -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    canonical_name   TEXT PRIMARY KEY,
    db_table         TEXT NOT NULL,
    db_schema        TEXT,
    singular_label   TEXT,
    plural_label     TEXT,
    proto_blob       BLOB NOT NULL  -- full Entity protobuf
);

-- Fields ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fields (
    entity           TEXT NOT NULL REFERENCES entities(canonical_name),
    field            TEXT NOT NULL,
    db_column        TEXT NOT NULL,
    type             TEXT NOT NULL,
    display_label    TEXT,
    enum_canonical   TEXT,
    unit_canonical   TEXT,
    proto_blob       BLOB NOT NULL,
    PRIMARY KEY (entity, field)
);
CREATE INDEX IF NOT EXISTS fields_by_label ON fields(display_label);

-- Relationships --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS relationships (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity      TEXT NOT NULL REFERENCES entities(canonical_name),
    from_field       TEXT NOT NULL,
    to_entity        TEXT NOT NULL REFERENCES entities(canonical_name),
    to_field         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    relation_name    TEXT,
    proto_blob       BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS rel_by_from ON relationships(from_entity);
CREATE INDEX IF NOT EXISTS rel_by_to   ON relationships(to_entity);
CREATE UNIQUE INDEX IF NOT EXISTS rel_unique_edge
ON relationships(from_entity, from_field, to_entity, to_field, kind);

-- Enums ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enums (
    canonical_name   TEXT PRIMARY KEY,
    proto_blob       BLOB NOT NULL
);

-- Units ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS units (
    canonical_name   TEXT PRIMARY KEY,
    storage_unit     TEXT NOT NULL,
    display_unit     TEXT NOT NULL,
    factor           REAL NOT NULL
);

-- Vocabulary -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vocabulary (
    term             TEXT NOT NULL,
    canonical_kind   TEXT NOT NULL CHECK (canonical_kind IN ('entity','field','enum_value','relationship','scope_predicate')),
    canonical_value  TEXT NOT NULL,  -- entity name | "users.created_at" | "users.status_code:39" | scope predicate JSON
    confidence       REAL NOT NULL,
    source_layer     INTEGER NOT NULL,
    source_locator   TEXT,           -- JSON
    PRIMARY KEY (term, canonical_kind, canonical_value)
);
CREATE INDEX IF NOT EXISTS vocab_by_term      ON vocabulary(term);
CREATE INDEX IF NOT EXISTS vocab_by_canonical ON vocabulary(canonical_kind, canonical_value);

-- Mandatory scope predicates -------------------------------------------------
CREATE TABLE IF NOT EXISTS scopes (
    entity           TEXT NOT NULL REFERENCES entities(canonical_name),
    kind             TEXT NOT NULL,  -- tenant / soft_delete / owner / rls / custom
    template         TEXT NOT NULL,
    required_params  TEXT NOT NULL,  -- JSON array
    source_rule      TEXT,
    PRIMARY KEY (entity, kind, template)
);

-- Sample values --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sample_values (
    field_canonical  TEXT PRIMARY KEY,
    examples         TEXT NOT NULL,  -- JSON array
    pii_redacted     INTEGER NOT NULL DEFAULT 0
);

-- Conflicts surfaced for `semsql doctor` ------------------------------------
CREATE TABLE IF NOT EXISTS conflict_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_target TEXT NOT NULL,
    candidates       TEXT NOT NULL,  -- JSON
    resolution       TEXT NOT NULL,
    suggested_override TEXT
);
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn opens_and_migrates_fresh_file() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.semsql");
        let conn = open(&path).unwrap();
        let v: String = conn
            .query_row(
                "SELECT value FROM semsql_metadata WHERE key='schema_version'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION.to_string());
    }

    #[test]
    fn open_is_idempotent() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("test.semsql");
        let _ = open(&path).unwrap();
        let _ = open(&path).unwrap(); // second open must be a no-op migration
    }
}
