//! Typed writer over a `.semsql` file.
//!
//! Counterpart to [`crate::read`]. Built on top of `rusqlite` so it
//! shares the connection pool with the reader and inherits the same
//! `WAL` + `foreign_keys=ON` pragmas that `open()` sets.
//!
//! All inserts use parameterised statements — string interpolation of
//! vocabulary into SQL is forbidden by the canonical-name allow-list,
//! and the writer enforces it again as a defence-in-depth check.

use crate::open;
use rusqlite::{Connection, params};
use semsql_core::{Result, SemsqlError};
use std::path::Path;

/// Insert a new entity. Idempotent: re-inserting an existing canonical
/// name updates the row in place.
pub struct EntityInsert<'a> {
    /// Canonical entity name (must match `[A-Za-z_][A-Za-z0-9_]{0,63}`).
    pub canonical_name: &'a str,
    /// Backing DB table.
    pub db_table: &'a str,
    /// Optional schema namespace (Postgres).
    pub db_schema: Option<&'a str>,
    /// Singular display label, e.g. `"Student"`.
    pub singular_label: Option<&'a str>,
    /// Plural display label, e.g. `"Students"`.
    pub plural_label: Option<&'a str>,
}

/// Insert a new field on an existing entity.
pub struct FieldInsert<'a> {
    /// Owning entity canonical name.
    pub entity: &'a str,
    /// Field canonical name (segment after the dot).
    pub field: &'a str,
    /// Backing DB column.
    pub db_column: &'a str,
    /// Storage type (one of the `FIELD_TYPE_*` enum text forms).
    pub field_type: &'a str,
    /// Display label.
    pub display_label: Option<&'a str>,
    /// Enum canonical name, e.g. `"users.status_code"`.
    pub enum_canonical: Option<&'a str>,
    /// Unit canonical name.
    pub unit_canonical: Option<&'a str>,
}

/// Insert a vocabulary entry.
pub struct VocabInsert<'a> {
    /// User-facing term — lower-cased, NFC-normalised.
    pub term: &'a str,
    /// Canonical-kind tag — `entity` / `field` / `enum_value` / `relationship`.
    pub canonical_kind: &'a str,
    /// Canonical value — `"users"` / `"users.created_at"` / `"users.status_code:39"`.
    pub canonical_value: &'a str,
    /// Confidence in `[0, 1]`.
    pub confidence: f32,
    /// Source layer (1..=6).
    pub source_layer: i32,
    /// Optional JSON locator (file/line/extractor).
    pub source_locator: Option<&'a str>,
}

/// Insert an enum definition.
pub struct EnumInsert<'a> {
    /// Canonical name, e.g. `"users.status_code"`.
    pub canonical_name: &'a str,
    /// Raw → label values, encoded as a JSON object.
    pub values_json: &'a str,
}

/// Insert a mandatory scope predicate.
pub struct ScopeInsert<'a> {
    /// Owning entity.
    pub entity: &'a str,
    /// Scope kind — `tenant` / `soft_delete` / `owner` / `rls` / `custom`.
    pub kind: &'a str,
    /// Predicate template with `{{table}}` placeholder.
    pub template: &'a str,
    /// Required parameters as a JSON array.
    pub required_params_json: &'a str,
    /// Human-readable rule name.
    pub source_rule: Option<&'a str>,
}

/// Open a writer connection. Shorthand for [`open`] with a more
/// intentional name at call sites.
pub fn writer(path: impl AsRef<Path>) -> Result<Connection> {
    open(path)
}

/// Insert an entity. Returns the rowid (sqlite-internal identifier).
pub fn insert_entity(conn: &Connection, e: EntityInsert<'_>) -> Result<i64> {
    check_canonical(e.canonical_name)?;
    conn.execute(
        "INSERT OR REPLACE INTO entities \
         (canonical_name, db_table, db_schema, singular_label, plural_label, proto_blob) \
         VALUES (?1, ?2, ?3, ?4, ?5, X'')",
        params![
            e.canonical_name,
            e.db_table,
            e.db_schema,
            e.singular_label,
            e.plural_label,
        ],
    )
    .map_err(|err| SemsqlError::Other(format!("insert_entity: {err}")))?;
    Ok(conn.last_insert_rowid())
}

/// Insert a field. Idempotent on `(entity, field)`.
pub fn insert_field(conn: &Connection, f: FieldInsert<'_>) -> Result<()> {
    check_canonical(f.entity)?;
    check_canonical(f.field)?;
    conn.execute(
        "INSERT OR REPLACE INTO fields \
         (entity, field, db_column, type, display_label, enum_canonical, unit_canonical, proto_blob) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, X'')",
        params![
            f.entity,
            f.field,
            f.db_column,
            f.field_type,
            f.display_label,
            f.enum_canonical,
            f.unit_canonical,
        ],
    )
    .map_err(|err| SemsqlError::Other(format!("insert_field: {err}")))?;
    Ok(())
}

/// Insert a vocabulary entry. Idempotent on the natural key
/// `(term, canonical_kind, canonical_value)`.
pub fn insert_vocab(conn: &Connection, v: VocabInsert<'_>) -> Result<()> {
    if v.confidence < 0.0 || v.confidence > 1.0 {
        return Err(SemsqlError::validation(format!(
            "vocab confidence must be in [0, 1], got {}",
            v.confidence
        )));
    }
    if !(1..=6).contains(&v.source_layer) {
        return Err(SemsqlError::validation(format!(
            "vocab source_layer must be 1..=6, got {}",
            v.source_layer
        )));
    }
    conn.execute(
        "INSERT OR REPLACE INTO vocabulary \
         (term, canonical_kind, canonical_value, confidence, source_layer, source_locator) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            v.term,
            v.canonical_kind,
            v.canonical_value,
            v.confidence as f64,
            v.source_layer,
            v.source_locator,
        ],
    )
    .map_err(|err| SemsqlError::Other(format!("insert_vocab: {err}")))?;
    Ok(())
}

/// Insert an enum definition. Auto-adds the optional
/// `_enum_values_json` column on first call so the runtime reader can
/// pick it up without a migration round-trip.
pub fn insert_enum(conn: &Connection, e: EnumInsert<'_>) -> Result<()> {
    ensure_enum_values_column(conn)?;
    conn.execute(
        "INSERT OR REPLACE INTO enums (canonical_name, proto_blob, _enum_values_json) \
         VALUES (?1, X'', ?2)",
        params![e.canonical_name, e.values_json],
    )
    .map_err(|err| SemsqlError::Other(format!("insert_enum: {err}")))?;
    Ok(())
}

fn ensure_enum_values_column(conn: &Connection) -> Result<()> {
    let exists: bool = conn
        .prepare(
            "SELECT 1 FROM pragma_table_info('enums') WHERE name = '_enum_values_json'",
        )
        .map_err(|e| SemsqlError::Other(format!("pragma probe prepare: {e}")))?
        .exists([])
        .map_err(|e| SemsqlError::Other(format!("pragma probe: {e}")))?;
    if exists {
        return Ok(());
    }
    conn.execute(
        "ALTER TABLE enums ADD COLUMN _enum_values_json TEXT NOT NULL DEFAULT '{}'",
        [],
    )
    .map_err(|e| SemsqlError::Other(format!("alter enums: {e}")))?;
    Ok(())
}

/// Insert a mandatory scope predicate. Idempotent on
/// `(entity, kind, template)`.
pub fn insert_scope(conn: &Connection, s: ScopeInsert<'_>) -> Result<()> {
    if !s.template.contains("{{table}}") {
        return Err(SemsqlError::validation(format!(
            "scope template for `{}` is missing the `{{{{table}}}}` placeholder",
            s.entity
        )));
    }
    conn.execute(
        "INSERT OR REPLACE INTO scopes \
         (entity, kind, template, required_params, source_rule) \
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![
            s.entity,
            s.kind,
            s.template,
            s.required_params_json,
            s.source_rule,
        ],
    )
    .map_err(|err| SemsqlError::Other(format!("insert_scope: {err}")))?;
    Ok(())
}

/// Stamp the application metadata. Run once after the writer has finished
/// inserting so downstream readers see a stable `schema_hash`.
pub fn stamp_metadata(
    conn: &Connection,
    application_name: &str,
    schema_hash: &str,
) -> Result<()> {
    for (k, v) in [
        ("application_name", application_name),
        ("schema_hash", schema_hash),
    ] {
        conn.execute(
            "INSERT OR REPLACE INTO semsql_metadata (key, value) VALUES (?1, ?2)",
            params![k, v],
        )
        .map_err(|err| SemsqlError::Other(format!("stamp_metadata: {err}")))?;
    }
    Ok(())
}

fn check_canonical(name: &str) -> Result<()> {
    if name.is_empty() || name.len() > 64 {
        return Err(SemsqlError::InvalidIdentifier(name.to_string()));
    }
    let mut bytes = name.bytes();
    let first = bytes.next().unwrap();
    if !(first.is_ascii_alphabetic() || first == b'_') {
        return Err(SemsqlError::InvalidIdentifier(name.to_string()));
    }
    if bytes.any(|b| !(b.is_ascii_alphanumeric() || b == b'_')) {
        return Err(SemsqlError::InvalidIdentifier(name.to_string()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::open;
    use tempfile::tempdir;

    fn fresh() -> (tempfile::TempDir, Connection) {
        let dir = tempdir().unwrap();
        let conn = open(dir.path().join("g.semsql")).unwrap();
        (dir, conn)
    }

    #[test]
    fn round_trips_entity() {
        let (_dir, conn) = fresh();
        insert_entity(
            &conn,
            EntityInsert {
                canonical_name: "users",
                db_table: "users",
                db_schema: Some("public"),
                singular_label: Some("Student"),
                plural_label: Some("Students"),
            },
        )
        .unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn rejects_unsafe_canonical_name() {
        let (_dir, conn) = fresh();
        let r = insert_entity(
            &conn,
            EntityInsert {
                canonical_name: "users; DROP TABLE",
                db_table: "users",
                db_schema: None,
                singular_label: None,
                plural_label: None,
            },
        );
        assert!(matches!(r, Err(SemsqlError::InvalidIdentifier(_))));
    }

    #[test]
    fn rejects_out_of_range_confidence() {
        let (_dir, conn) = fresh();
        let r = insert_vocab(
            &conn,
            VocabInsert {
                term: "students",
                canonical_kind: "entity",
                canonical_value: "users",
                confidence: 1.5,
                source_layer: 6,
                source_locator: None,
            },
        );
        assert!(matches!(r, Err(SemsqlError::Validation(_))));
    }

    #[test]
    fn rejects_template_without_table_placeholder() {
        let (_dir, conn) = fresh();
        insert_entity(
            &conn,
            EntityInsert {
                canonical_name: "users",
                db_table: "users",
                db_schema: None,
                singular_label: None,
                plural_label: None,
            },
        )
        .unwrap();
        let r = insert_scope(
            &conn,
            ScopeInsert {
                entity: "users",
                kind: "tenant",
                template: "tenant_id = :tenant",
                required_params_json: "[\"tenant\"]",
                source_rule: Some("tenant_isolation"),
            },
        );
        assert!(matches!(r, Err(SemsqlError::Validation(_))));
    }

    #[test]
    fn enum_insert_adds_optional_json_column() {
        let (_dir, conn) = fresh();
        insert_enum(
            &conn,
            EnumInsert {
                canonical_name: "users.status_code",
                values_json: r#"{"1":"Pending","2":"Active"}"#,
            },
        )
        .unwrap();
        let json: String = conn
            .query_row(
                "SELECT _enum_values_json FROM enums WHERE canonical_name = 'users.status_code'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(json.contains("Active"));
    }

    #[test]
    fn writer_then_reader_round_trip() {
        let (_dir, conn) = fresh();
        insert_entity(
            &conn,
            EntityInsert {
                canonical_name: "users",
                db_table: "users",
                db_schema: None,
                singular_label: Some("Student"),
                plural_label: Some("Students"),
            },
        )
        .unwrap();
        insert_vocab(
            &conn,
            VocabInsert {
                term: "students",
                canonical_kind: "entity",
                canonical_value: "users",
                confidence: 1.0,
                source_layer: 6,
                source_locator: Some(r#"{"file":"x.tsx","line":10}"#),
            },
        )
        .unwrap();
        let path = conn.path().expect("connection path").to_string();
        drop(conn);
        let vocab = crate::read::vocabulary(&path).unwrap();
        assert_eq!(vocab.len(), 1);
        assert_eq!(vocab[0].canonical_value, "users");
    }
}
