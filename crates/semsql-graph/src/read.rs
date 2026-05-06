//! Typed read-only views over a `.semsql` file.
//!
//! The Python rewriter has its own reader (sqlite stdlib) so it stays
//! torch-free. Rust's runtime needs the same data; this module provides
//! it. Both readers must agree on the schema — `crates/semsql-graph` is
//! the source of truth and its migrations live in this same crate.

use crate::open;
use rusqlite::OptionalExtension;
use semsql_core::{Result, SemsqlError};
use std::path::Path;

/// One vocabulary row, normalised for runtime lookup.
#[derive(Clone, Debug, PartialEq)]
pub struct VocabularyEntry {
    /// Lower-cased, NFC-normalised user-facing term.
    pub term: String,
    /// `entity` / `field` / `enum_value` / `relationship`.
    pub canonical_kind: String,
    /// For entities: `"users"`. For fields: `"users.created_at"`. For
    /// enum values: `"users.status_code:39"`.
    pub canonical_value: String,
    /// `[0.0, 1.0]`.
    pub confidence: f32,
    /// Layer index (1 = DB schema, …, 6 = form/table label).
    pub source_layer: i32,
}

/// One enum row.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EnumRow {
    /// Canonical name of the enum, e.g. `"users.status_code"`.
    pub canonical_name: String,
    /// Map from raw DB value (string-encoded) to label.
    pub values: indexmap::IndexMap<String, String>,
}

/// Read every vocabulary entry from the graph.
pub fn vocabulary(path: impl AsRef<Path>) -> Result<Vec<VocabularyEntry>> {
    let conn = open(path)?;
    let mut stmt = conn
        .prepare(
            "SELECT term, canonical_kind, canonical_value, confidence, source_layer \
             FROM vocabulary",
        )
        .map_err(|e| SemsqlError::Other(format!("vocabulary prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            Ok(VocabularyEntry {
                term: row.get::<_, String>(0)?.to_lowercase(),
                canonical_kind: row.get(1)?,
                canonical_value: row.get(2)?,
                confidence: row.get::<_, f64>(3)? as f32,
                source_layer: row.get(4)?,
            })
        })
        .map_err(|e| SemsqlError::Other(format!("vocabulary query: {e}")))?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| SemsqlError::Other(format!("vocabulary row: {e}")))?);
    }
    Ok(out)
}

/// Read every enum and its raw → label map.
///
/// Optional column `_enum_values_json` carries the value map; if missing
/// we return empty maps (the test fixture builders set it).
pub fn enums(path: impl AsRef<Path>) -> Result<Vec<EnumRow>> {
    let conn = open(path)?;
    let has_json: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM pragma_table_info('enums') WHERE name = '_enum_values_json'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| SemsqlError::Other(format!("enum schema probe: {e}")))?;

    let mut out = Vec::new();
    if has_json.is_some() {
        let mut stmt = conn
            .prepare("SELECT canonical_name, _enum_values_json FROM enums")
            .map_err(|e| SemsqlError::Other(format!("enum prepare: {e}")))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .map_err(|e| SemsqlError::Other(format!("enum query: {e}")))?;
        for r in rows {
            let (canonical_name, json) = r.map_err(|e| SemsqlError::Other(e.to_string()))?;
            let values: indexmap::IndexMap<String, String> = serde_json::from_str(&json)
                .map_err(|e| {
                    SemsqlError::Other(format!(
                        "enum _enum_values_json malformed for `{canonical_name}`: {e}"
                    ))
                })?;
            out.push(EnumRow {
                canonical_name,
                values,
            });
        }
    } else {
        let mut stmt = conn
            .prepare("SELECT canonical_name FROM enums")
            .map_err(|e| SemsqlError::Other(format!("enum prepare: {e}")))?;
        let rows = stmt
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|e| SemsqlError::Other(format!("enum query: {e}")))?;
        for r in rows {
            out.push(EnumRow {
                canonical_name: r.map_err(|e| SemsqlError::Other(e.to_string()))?,
                values: indexmap::IndexMap::new(),
            });
        }
    }
    Ok(out)
}

/// One conflict-log row.
#[derive(Clone, Debug, PartialEq)]
pub struct ConflictLogRow {
    /// Auto-incremented row id.
    pub id: i64,
    /// What the conflict was about, e.g. `"users.status_code"`.
    pub canonical_target: String,
    /// JSON array of candidates as inserted by the merge engine.
    pub candidates_json: String,
    /// Free-form resolution string.
    pub resolution: String,
    /// Suggested override the user can paste into `semsql.overrides.yaml`.
    pub suggested_override: Option<String>,
}

/// Read every row in `conflict_log`. Returned in id order so the
/// `semsql doctor` UX is stable across runs.
pub fn conflicts(path: impl AsRef<Path>) -> Result<Vec<ConflictLogRow>> {
    let conn = open(path)?;
    let mut stmt = conn
        .prepare(
            "SELECT id, canonical_target, candidates, resolution, suggested_override \
             FROM conflict_log ORDER BY id",
        )
        .map_err(|e| SemsqlError::Other(format!("conflicts prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            Ok(ConflictLogRow {
                id: row.get(0)?,
                canonical_target: row.get(1)?,
                candidates_json: row.get(2)?,
                resolution: row.get(3)?,
                suggested_override: row.get(4)?,
            })
        })
        .map_err(|e| SemsqlError::Other(format!("conflicts query: {e}")))?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
    }
    Ok(out)
}

/// Coverage stats — counts of entities, fields, vocabulary, scopes,
/// enums, and the entities that have *no* vocabulary entry beyond their
/// canonical name. The latter is the most useful diagnostic: empty-vocab
/// entities are the ones the cascade can only resolve via DB-table-name
/// fallback (low-confidence layer 1).
#[derive(Clone, Debug, Default)]
pub struct GraphCoverage {
    /// Count of rows in `entities`.
    pub entity_count: usize,
    /// Count of rows in `fields`.
    pub field_count: usize,
    /// Count of rows in `vocabulary`.
    pub vocab_count: usize,
    /// Count of rows in `scopes`.
    pub scope_count: usize,
    /// Count of rows in `enums`.
    pub enum_count: usize,
    /// Entities for which the only vocabulary entry is the bare table
    /// name (source layer 1). These rely on framework-supplied
    /// vocabulary and benefit most from running an extractor.
    pub entities_lacking_ui_vocab: Vec<String>,
    /// Entities tagged as scoped (in `scopes`) so RLS doctoring can
    /// cross-reference them against the live DB.
    pub scoped_entities: Vec<String>,
}

/// Compute coverage stats over the graph file.
pub fn coverage(path: impl AsRef<Path>) -> Result<GraphCoverage> {
    let conn = open(path)?;
    let mut cov = GraphCoverage::default();
    cov.entity_count = count(&conn, "entities")?;
    cov.field_count = count(&conn, "fields")?;
    cov.vocab_count = count(&conn, "vocabulary")?;
    cov.scope_count = count(&conn, "scopes")?;
    cov.enum_count = count(&conn, "enums")?;

    let mut stmt = conn
        .prepare(
            "SELECT e.canonical_name FROM entities e \
             WHERE NOT EXISTS ( \
                 SELECT 1 FROM vocabulary v \
                 WHERE v.canonical_kind = 'entity' \
                   AND v.canonical_value = e.canonical_name \
                   AND v.source_layer >= 5 \
             ) ORDER BY e.canonical_name",
        )
        .map_err(|e| SemsqlError::Other(format!("coverage prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| SemsqlError::Other(format!("coverage query: {e}")))?;
    for r in rows {
        cov.entities_lacking_ui_vocab
            .push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
    }

    let mut stmt = conn
        .prepare("SELECT DISTINCT entity FROM scopes ORDER BY entity")
        .map_err(|e| SemsqlError::Other(format!("scoped prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| SemsqlError::Other(format!("scoped query: {e}")))?;
    for r in rows {
        cov.scoped_entities
            .push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
    }

    Ok(cov)
}

fn count(conn: &rusqlite::Connection, table: &str) -> Result<usize> {
    let stmt = format!("SELECT COUNT(*) FROM {table}");
    conn.query_row(&stmt, [], |row| row.get::<_, i64>(0))
        .map(|n| n as usize)
        .map_err(|e| SemsqlError::Other(format!("count({table}): {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::open;
    use crate::write::{insert_entity, insert_scope, insert_vocab, EntityInsert, ScopeInsert, VocabInsert};
    use tempfile::tempdir;

    #[test]
    fn coverage_flags_entities_lacking_ui_vocab() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("g.semsql");
        let conn = open(&path).unwrap();

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
        insert_entity(
            &conn,
            EntityInsert {
                canonical_name: "tenants",
                db_table: "tenants",
                db_schema: None,
                singular_label: None,
                plural_label: None,
            },
        )
        .unwrap();
        // Only `users` gets a UI-layer vocab entry.
        insert_vocab(
            &conn,
            VocabInsert {
                term: "students",
                canonical_kind: "entity",
                canonical_value: "users",
                confidence: 0.95,
                source_layer: 6,
                source_locator: None,
            },
        )
        .unwrap();
        // Both get a layer-1 fallback (counts as missing UI vocab).
        for name in ["users", "tenants"] {
            insert_vocab(
                &conn,
                VocabInsert {
                    term: name,
                    canonical_kind: "entity",
                    canonical_value: name,
                    confidence: 0.5,
                    source_layer: 1,
                    source_locator: None,
                },
            )
            .unwrap();
        }
        drop(conn);

        let cov = coverage(&path).unwrap();
        assert_eq!(cov.entity_count, 2);
        assert_eq!(cov.entities_lacking_ui_vocab, vec!["tenants".to_string()]);
    }

    #[test]
    fn coverage_lists_scoped_entities() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("g.semsql");
        let conn = open(&path).unwrap();
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
        insert_scope(
            &conn,
            ScopeInsert {
                entity: "users",
                kind: "tenant",
                template: "{{table}}.tenant_id = :tenant",
                required_params_json: "[\"tenant\"]",
                source_rule: Some("tenant_isolation"),
            },
        )
        .unwrap();
        drop(conn);

        let cov = coverage(&path).unwrap();
        assert_eq!(cov.scope_count, 1);
        assert_eq!(cov.scoped_entities, vec!["users".to_string()]);
    }

    #[test]
    fn conflicts_round_trip() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("g.semsql");
        let conn = open(&path).unwrap();
        conn.execute(
            "INSERT INTO conflict_log (canonical_target, candidates, resolution, suggested_override) \
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params!["users.status_code", "[]", "filament_form wins", "users.status_code: filament"],
        )
        .unwrap();
        drop(conn);

        let rows = conflicts(&path).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].canonical_target, "users.status_code");
        assert_eq!(rows[0].resolution, "filament_form wins");
    }
}

/// Pluralised entity index — keys are the *plural / singular label*
/// lower-cased; the value is the list of canonical entity names that
/// claim that label.
///
/// A `Vec<String>` (rather than a single `String`) is required because
/// two distinct entities can share the same display label in real
/// codebases — e.g. an `archived_users` resource also labelled
/// "Students" beside a current `users` resource. The pre-resolver
/// (Stage 0a) treats any term with `len > 1` as ambiguous and falls
/// through to the model stages instead of silently picking one.
///
/// Each value list is deduplicated and stable in insertion order so
/// downstream consumers can reproduce the same `NeedsModel` decisions
/// across runs.
pub fn plural_label_index(
    path: impl AsRef<Path>,
) -> Result<indexmap::IndexMap<String, Vec<String>>> {
    let conn = open(path)?;
    let mut stmt = conn
        .prepare("SELECT canonical_name, plural_label, singular_label FROM entities")
        .map_err(|e| SemsqlError::Other(format!("entities prepare: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })
        .map_err(|e| SemsqlError::Other(format!("entities query: {e}")))?;
    let mut out: indexmap::IndexMap<String, Vec<String>> = indexmap::IndexMap::new();
    let push = |label: String, canonical: &str, out: &mut indexmap::IndexMap<String, Vec<String>>| {
        let entry = out.entry(label).or_default();
        if !entry.iter().any(|c| c == canonical) {
            entry.push(canonical.to_string());
        }
    };
    for r in rows {
        let (canonical, plural, singular) = r.map_err(|e| SemsqlError::Other(e.to_string()))?;
        push(canonical.to_lowercase(), &canonical, &mut out);
        if let Some(p) = plural {
            push(p.to_lowercase(), &canonical, &mut out);
        }
        if let Some(s) = singular {
            push(s.to_lowercase(), &canonical, &mut out);
        }
    }
    Ok(out)
}
