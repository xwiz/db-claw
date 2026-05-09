//! `semsql extract` orchestrator.
//!
//! v0.2 implements the `--framework=none` path: introspect a SQLite DB
//! and write a SemanticGraph that the cascade can immediately serve.
//! Postgres / MySQL backends land alongside their integration tests.
//!
//! Framework adapters (Laravel/Filament, Next.js, Django, …) live on the
//! TypeScript side of the workspace; the CLI's job here is to merge their
//! JSONL output into the same `.semsql` file once they ship.

use anyhow::{Context, Result};
#[cfg(feature = "postgres")]
use semsql_extract_db::PgIntrospect;
use semsql_extract_db::sqlite::SqliteIntrospect;
use semsql_extract_db::Introspect;
use semsql_graph::write::{
    insert_entity, insert_field, insert_vocab, stamp_metadata, EntityInsert, FieldInsert,
    VocabInsert,
};
use semsql_graph::open;
use serde::Deserialize;
use std::path::Path;

/// Source-layer constant for vocabulary derived from the bare DB schema —
/// always the lowest priority. Mirrors `SOURCE_LAYER_DB_SCHEMA = 1` in the
/// canonical proto.
const SOURCE_LAYER_DB_SCHEMA: i32 = 1;

/// Summary of one extract run, surfaced to the CLI for the user-facing
/// log message.
#[derive(Debug, Default)]
pub struct ExtractSummary {
    /// Number of entities written.
    pub entity_count: usize,
    /// Number of fields written.
    pub field_count: usize,
    /// Number of vocabulary rows written.
    pub vocab_count: usize,
}

/// Run a `--framework=none` extraction against `db_url`, writing the
/// SemanticGraph to `out`. Supported URL schemes: `sqlite:` and (when the
/// `postgres` feature is enabled, which it is by default) `postgres:` /
/// `postgresql:`.
pub async fn run_db_only(db_url: &str, out: &Path) -> Result<ExtractSummary> {
    let backend = pick_backend(db_url).await?;
    let intro: &dyn Introspect = backend.as_ref();

    if out.exists() {
        std::fs::remove_file(out).context("clearing previous SemanticGraph file")?;
    }
    let conn = open(out).context("opening SemanticGraph for write")?;

    let mut summary = ExtractSummary::default();

    let tables = intro
        .list_tables()
        .await
        .map_err(|e| anyhow::anyhow!("list_tables: {e}"))?;
    for table in &tables {
        // Postgres can return `schema.table` for non-default schemas;
        // collapse to the bare canonical name (the SemanticGraph already
        // tracks schema separately on the entity row).
        let (raw_table, schema) = match table.split_once('.') {
            Some((s, t)) => (t.to_string(), Some(s.to_string())),
            None => (table.clone(), None),
        };
        // Canonical entity names are always lowercased — model weights trained
        // on lowercase snake_case names (Spider/BIRD). db_table preserves original.
        let canonical = if is_safe_canonical(&raw_table) {
            raw_table.to_lowercase()
        } else {
            match to_canonical_snake(&raw_table) {
                Some(c) => {
                    tracing::debug!(table = %raw_table, canonical = %c, "canonicalized non-standard table name");
                    c
                }
                None => {
                    tracing::warn!(table = %table, "skipping table — name fails canonical allow-list");
                    continue;
                }
            }
        };
        insert_entity(
            &conn,
            EntityInsert {
                canonical_name: &canonical,
                db_table: &raw_table,
                db_schema: schema.as_deref(),
                singular_label: None,
                plural_label: None,
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_entity({table}): {e}"))?;
        summary.entity_count += 1;

        // The bare table name itself is a low-confidence vocabulary entry
        // so the pre-resolver can hit it before any framework-supplied
        // label is loaded.
        insert_vocab(
            &conn,
            VocabInsert {
                term: &canonical.to_lowercase(),
                canonical_kind: "entity",
                canonical_value: &canonical,
                confidence: 0.5,
                source_layer: SOURCE_LAYER_DB_SCHEMA,
                source_locator: None,
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_vocab({table}): {e}"))?;
        summary.vocab_count += 1;
    }

    let columns = intro
        .list_columns()
        .await
        .map_err(|e| anyhow::anyhow!("list_columns: {e}"))?;
    for col in columns {
        let raw_col_table = col
            .table
            .split_once('.')
            .map(|(_, t)| t.to_string())
            .unwrap_or_else(|| col.table.clone());
        let canonical_table = if is_safe_canonical(&raw_col_table) {
            raw_col_table.to_lowercase()
        } else {
            match to_canonical_snake(&raw_col_table) {
                Some(c) => c,
                None => {
                    tracing::warn!(table = %col.table, "skipping field — table name not canonicalisable");
                    continue;
                }
            }
        };
        // Canonical field names are always lowercased — model weights trained
        // on lowercase names. db_column preserves original for SQL emit.
        let canonical_col = if is_safe_canonical(&col.column) {
            col.column.to_lowercase()
        } else {
            match to_canonical_snake(&col.column) {
                Some(c) => {
                    tracing::debug!(
                        table = %col.table,
                        column = %col.column,
                        canonical = %c,
                        "canonicalized non-standard column name"
                    );
                    c
                }
                None => {
                    tracing::warn!(
                        table = %col.table,
                        column = %col.column,
                        "skipping field — column name not canonicalisable"
                    );
                    continue;
                }
            }
        };
        insert_field(
            &conn,
            FieldInsert {
                entity: &canonical_table,
                field: &canonical_col,
                db_column: &col.column,
                field_type: &col.data_type,
                display_label: None,
                enum_canonical: None,
                unit_canonical: None,
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_field({}.{}): {e}", col.table, col.column))?;
        summary.field_count += 1;

        // Register field in vocabulary so Stage 1 (schema linker) can
        // score it. canonical_value is "entity.field" as expected by
        // collect_schema_items in stage_linker.rs.
        let field_vocab_value = format!("{canonical_table}.{canonical_col}");
        insert_vocab(
            &conn,
            VocabInsert {
                term: &canonical_col,
                canonical_kind: "field",
                canonical_value: &field_vocab_value,
                confidence: 0.5,
                source_layer: SOURCE_LAYER_DB_SCHEMA,
                source_locator: None,
            },
        )
        .map_err(|e| {
            anyhow::anyhow!("insert_vocab(field {}.{}): {e}", col.table, col.column)
        })?;
        summary.vocab_count += 1;
    }

    let schema_hash = compute_schema_hash(&summary, &tables);
    stamp_metadata(&conn, &derive_app_name_for(db_url), &schema_hash)
        .map_err(|e| anyhow::anyhow!("stamp_metadata: {e}"))?;

    Ok(summary)
}

/// Pick the right `Introspect` backend for `db_url`. Boxed because the
/// trait is not object-safe-by-default for borrowing — we need a stable
/// `&dyn Introspect` for the duration of the extract. Postgres is gated
/// behind the `postgres` feature so downstream embedders that don't need
/// it can skip the sqlx dependency.
async fn pick_backend(db_url: &str) -> Result<Box<dyn Introspect>> {
    if db_url.starts_with("sqlite:") || db_url.starts_with("sqlite://") {
        let path = parse_sqlite_url(db_url).context("parsing --db-url")?;
        let intro = SqliteIntrospect::open(&path).context("opening source SQLite database")?;
        return Ok(Box::new(intro));
    }
    #[cfg(feature = "postgres")]
    {
        if db_url.starts_with("postgres:") || db_url.starts_with("postgresql:") {
            let intro = PgIntrospect::connect(db_url)
                .await
                .map_err(|e| anyhow::anyhow!("postgres connect: {e}"))?;
            return Ok(Box::new(intro));
        }
    }
    anyhow::bail!(
        "unsupported --db-url scheme `{db_url}`; expected sqlite:, postgres:, or postgresql:"
    )
}

/// App-name derivation that works for both file-paths and URLs. Used to
/// stamp `metadata.app_name` so multi-DB users can tell graphs apart.
fn derive_app_name_for(db_url: &str) -> String {
    if db_url.starts_with("sqlite:") || db_url.starts_with("sqlite://") {
        if let Ok(path) = parse_sqlite_url(db_url) {
            return derive_app_name(&path);
        }
    }
    // Postgres: take the path component (database name) from the URL.
    db_url
        .rsplit_once('/')
        .map(|(_, db)| db.split('?').next().unwrap_or(db).to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

/// Ingest a JSONL file of TypeScript-emitted [`VocabFragment`] records
/// into an existing `.semsql` graph. Each line is one record matching the
/// TS shape (see `packages/extractor-sdk/src/types.ts`).
///
/// Returns the number of vocabulary rows actually written. Records that
/// fail sanitisation in the writer are skipped with a warning, not
/// aborted — partial extracts are useful and the merge engine surfaces
/// the conflict log via `semsql doctor`.
pub fn ingest_vocab_jsonl(graph_out: &Path, jsonl: &Path) -> Result<usize> {
    let conn = open(graph_out).context("open graph for vocab ingest")?;
    let text = std::fs::read_to_string(jsonl)
        .with_context(|| format!("read JSONL {}", jsonl.display()))?;

    let mut written = 0usize;
    for (lineno, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let frag: TsVocabFragment = serde_json::from_str(line).with_context(|| {
            format!("{}:{}: invalid VocabFragment JSON", jsonl.display(), lineno + 1)
        })?;
        let kind_value = match frag.canonical {
            TsCanonical::Entity { entity } => {
                if !is_safe_canonical(&entity) {
                    tracing::warn!(line = lineno + 1, value = %entity, "rejecting non-canonical entity name");
                    continue;
                }
                ("entity".to_string(), entity)
            }
            TsCanonical::Field { field } => {
                if !is_safe_field(&field) {
                    tracing::warn!(line = lineno + 1, value = %field, "rejecting non-canonical field name");
                    continue;
                }
                ("field".to_string(), field)
            }
            TsCanonical::EnumValue { enum_name, raw_value } => {
                if !is_safe_field(&enum_name) {
                    tracing::warn!(line = lineno + 1, value = %enum_name, "rejecting non-canonical enum name");
                    continue;
                }
                ("enum_value".to_string(), format!("{enum_name}:{raw_value}"))
            }
            TsCanonical::Relationship { from, to } => {
                if !is_safe_canonical(&from) || !is_safe_canonical(&to) {
                    tracing::warn!(line = lineno + 1, "rejecting relationship with non-canonical endpoint");
                    continue;
                }
                ("relationship".to_string(), format!("{from}->{to}"))
            }
        };
        let confidence = frag.confidence.clamp(0.0, 1.0);
        let layer = frag.locator.layer.clamp(1, 6);
        let locator_json = serde_json::to_string(&frag.locator).ok();

        let attempt = insert_vocab(
            &conn,
            VocabInsert {
                term: &frag.term.to_lowercase(),
                canonical_kind: &kind_value.0,
                canonical_value: &kind_value.1,
                confidence,
                source_layer: layer,
                source_locator: locator_json.as_deref(),
            },
        );
        match attempt {
            Ok(()) => written += 1,
            Err(e) => tracing::warn!(
                line = lineno + 1,
                error = %e,
                "skipping vocab fragment that failed sanitiser"
            ),
        }
    }
    Ok(written)
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TsVocabFragment {
    term: String,
    canonical: TsCanonical,
    confidence: f32,
    locator: TsLocator,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum TsCanonical {
    Entity {
        entity: String,
    },
    Field {
        field: String,
    },
    EnumValue {
        #[serde(rename = "enumName")]
        enum_name: String,
        #[serde(rename = "rawValue")]
        raw_value: String,
    },
    Relationship {
        from: String,
        to: String,
    },
}

#[derive(Debug, Deserialize, serde::Serialize)]
struct TsLocator {
    file: String,
    line: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    column: Option<u32>,
    /// Layer index — 1 (DB) … 6 (form/table label).
    layer: i32,
    extractor: String,
}

fn parse_sqlite_url(url: &str) -> Result<std::path::PathBuf> {
    let raw = url
        .strip_prefix("sqlite://")
        .or_else(|| url.strip_prefix("sqlite:"))
        .ok_or_else(|| {
            anyhow::anyhow!("only sqlite:<path> URLs are supported in v0.2 (got `{url}`)")
        })?;
    Ok(std::path::PathBuf::from(raw))
}

fn is_safe_field(s: &str) -> bool {
    match s.split_once('.') {
        Some((entity, field)) => is_safe_canonical(entity) && is_safe_canonical(field),
        None => is_safe_canonical(s),
    }
}

fn is_safe_canonical(s: &str) -> bool {
    if s.is_empty() || s.len() > 64 {
        return false;
    }
    let mut bytes = s.bytes();
    let first = bytes.next().unwrap();
    (first.is_ascii_alphabetic() || first == b'_')
        && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// Convert an arbitrary DB identifier (spaces, parens, slashes, etc.) to a
/// `[a-z_][a-z0-9_]{0,62}` canonical form. Used so BIRD-style columns like
/// "Free Meal Count (K-12)" become "free_meal_count_k_12" in the graph while
/// the original string is preserved as `db_column`/`db_table` for SQL emit.
/// Returns `None` when the result would be empty after stripping non-word chars.
fn to_canonical_snake(s: &str) -> Option<String> {
    let lowered: String = s
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c.to_ascii_lowercase() } else { '_' })
        .collect();
    // Collapse consecutive underscores and drop empty segments.
    let parts: Vec<&str> = lowered.split('_').filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return None;
    }
    let mut result = parts.join("_");
    // Ensure it doesn't start with a digit.
    if result.starts_with(|c: char| c.is_ascii_digit()) {
        result.insert(0, '_');
    }
    // Truncate to 63 chars.
    result.truncate(63);
    if result.is_empty() { None } else { Some(result) }
}

fn compute_schema_hash(summary: &ExtractSummary, tables: &[String]) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut h = DefaultHasher::new();
    summary.entity_count.hash(&mut h);
    summary.field_count.hash(&mut h);
    for t in tables {
        t.hash(&mut h);
    }
    format!("{:016x}", h.finish())
}

fn derive_app_name(sqlite_path: &Path) -> String {
    sqlite_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("unknown")
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;
    use tempfile::TempDir;

    fn build_demo_db() -> (TempDir, std::path::PathBuf) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("demo.sqlite");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
             CREATE TABLE users (
                 id INTEGER PRIMARY KEY,
                 tenant_id INTEGER NOT NULL,
                 status_code INTEGER DEFAULT 1,
                 created_at TEXT
             );",
        )
        .unwrap();
        drop(conn);
        (dir, path)
    }

    #[tokio::test]
    async fn extracts_entities_and_fields() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only(&url, &out).await.unwrap();
        assert_eq!(summary.entity_count, 2);
        assert!(summary.field_count >= 6);
        assert!(summary.vocab_count >= 2);
        assert!(out.exists());
    }

    #[tokio::test]
    async fn output_supports_immediate_cascade_query() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let cascade = semsql_runtime::Cascade::load(&out, None).unwrap();
        let outcome = cascade.run("show tenants").unwrap();
        assert_eq!(outcome.sql_text, "SELECT * FROM tenants");
    }

    #[test]
    fn rejects_non_sqlite_url() {
        let r = parse_sqlite_url("postgres://user:pass@host/db");
        assert!(r.is_err());
    }

    #[tokio::test]
    async fn jsonl_fragments_lift_pre_resolver_recall() {
        // Without the JSONL fragment, "show students" must fail (no vocab
        // anchors `students` to `users`). After ingest the cascade
        // resolves it deterministically.
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let cascade = semsql_runtime::Cascade::load(&out, None).unwrap();
        let pre_ingest = cascade.run("show students");
        assert!(pre_ingest.is_err(), "expected NeedsModel before vocab ingest");

        let jsonl = dir.path().join("frags.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"students","canonical":{"kind":"entity","entity":"users"},"confidence":0.95,"locator":{"file":"lang/en/models.php","line":3,"layer":5,"extractor":"extractor-laravel:lang:en:php"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written, 1);

        let cascade2 = semsql_runtime::Cascade::load(&out, None).unwrap();
        let outcome = cascade2.run("show students").unwrap();
        assert_eq!(outcome.sql_text, "SELECT * FROM users");
    }

    #[tokio::test]
    async fn jsonl_skips_invalid_canonical_silently() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("frags.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"students","canonical":{"kind":"entity","entity":"users; DROP"},"confidence":0.95,"locator":{"file":"x","line":1,"layer":5,"extractor":"t"}}
{"term":"organizations","canonical":{"kind":"entity","entity":"tenants"},"confidence":0.95,"locator":{"file":"x","line":2,"layer":5,"extractor":"t"}}
"#,
        )
        .unwrap();
        // Only the legal row should land; the hostile row is logged + skipped.
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written, 1);
    }
}
