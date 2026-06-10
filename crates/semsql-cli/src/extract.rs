//! `semsql extract` orchestrator.
//!
//! v0.2 implements DB-grounded graph extraction: introspect a database
//! and write a SemanticGraph that the cascade can immediately serve.
//! Postgres / MySQL backends land alongside their integration tests.
//!
//! Framework adapters (Laravel/Filament, Next.js, Django, …) live on the
//! TypeScript side of the workspace; the top-level CLI can invoke
//! `semsql-extract` and merge its JSONL output into the same `.semsql` file.

use anyhow::{Context, Result};
use semsql_extract_db::sqlite::SqliteIntrospect;
#[cfg(feature = "mysql")]
use semsql_extract_db::MySqlIntrospect;
#[cfg(feature = "postgres")]
use semsql_extract_db::PgIntrospect;
use semsql_extract_db::{ColumnIntro, Introspect};
use semsql_graph::open;
use semsql_graph::write::{
    insert_entity, insert_field, insert_metric_definition, insert_relationship,
    insert_sample_values, insert_vocab, stamp_metadata, EntityInsert, FieldInsert,
    MetricDefinitionInsert, RelationshipInsert, SampleValuesInsert, VocabInsert,
};
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::io::Cursor;
use std::path::{Path, PathBuf};

/// Source-layer constant for vocabulary derived from the bare DB schema —
/// always the lowest priority. Mirrors `SOURCE_LAYER_DB_SCHEMA = 1` in the
/// canonical proto.
const SOURCE_LAYER_DB_SCHEMA: i32 = 1;
/// Source-layer constant for external schema descriptions adjacent to a DB
/// or project. These are still schema-derived, but richer than raw column
/// identifiers such as `A2`/`A3`.
const SOURCE_LAYER_DB_DESCRIPTION: i32 = 2;

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
    /// Number of FK relationships written.
    pub relationship_count: usize,
    /// Number of sampled-value rows written.
    pub sample_value_count: usize,
    /// Number of fields enriched from external schema-description files.
    pub field_description_count: usize,
    /// Number of field-compatible value predicates derived from external
    /// schema-description dictionaries.
    pub value_description_predicate_count: usize,
}

/// Summary of one framework/authored JSONL ingest.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct JsonlIngestSummary {
    /// Number of vocabulary rows written.
    pub vocab_count: usize,
    /// Number of metric definition rows written.
    pub metric_definition_count: usize,
    /// Number of grounded relationship edges written.
    pub relationship_count: usize,
}

/// Options for DB-only extraction.
#[derive(Debug, Clone)]
pub struct DbOnlyExtractOptions {
    /// Whether to sample distinct field values into `sample_values`.
    pub sample_values: bool,
    /// Optional directory of table/column description CSVs. The CLI
    /// auto-discovers `database_description/` under the project path; tests
    /// and embedders can pass this directly.
    pub schema_description_dir: Option<PathBuf>,
}

impl Default for DbOnlyExtractOptions {
    fn default() -> Self {
        Self {
            sample_values: true,
            schema_description_dir: None,
        }
    }
}

/// Run a `--framework=none` extraction against `db_url`, writing the
/// SemanticGraph to `out`. Supported URL schemes: `sqlite:` and (when the
/// `postgres` feature is enabled, which it is by default) `postgres:` /
/// `postgresql:`.
#[allow(dead_code)]
pub async fn run_db_only(db_url: &str, out: &Path) -> Result<ExtractSummary> {
    run_db_only_with_options(db_url, out, DbOnlyExtractOptions::default()).await
}

/// Run a `--framework=none` extraction with explicit privacy/sampling options.
pub async fn run_db_only_with_options(
    db_url: &str,
    out: &Path,
    options: DbOnlyExtractOptions,
) -> Result<ExtractSummary> {
    let backend = pick_backend(db_url).await?;
    let intro: &dyn Introspect = backend.as_ref();
    let schema_descriptions =
        load_schema_description_index(options.schema_description_dir.as_deref())?;

    if out.exists() {
        std::fs::remove_file(out).context("clearing previous SemanticGraph file")?;
    }
    let conn = open(out).context("opening SemanticGraph for write")?;
    conn.execute_batch("BEGIN IMMEDIATE")
        .context("begin SemanticGraph write transaction")?;

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
        let (singular_label, plural_label) = entity_labels_from_canonical(&canonical);
        insert_entity(
            &conn,
            EntityInsert {
                canonical_name: &canonical,
                db_table: &raw_table,
                db_schema: schema.as_deref(),
                singular_label: singular_label.as_deref(),
                plural_label: plural_label.as_deref(),
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_entity({table}): {e}"))?;
        summary.entity_count += 1;

        // The bare table name itself is a low-confidence vocabulary entry
        // so the pre-resolver can hit it before any framework-supplied
        // label is loaded.
        for term in entity_vocab_terms(
            &canonical,
            singular_label.as_deref(),
            plural_label.as_deref(),
        ) {
            insert_vocab(
                &conn,
                VocabInsert {
                    term: &term,
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
    }

    let columns = intro
        .list_columns()
        .await
        .map_err(|e| anyhow::anyhow!("list_columns: {e}"))?;
    for col in &columns {
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
        let field_description = schema_descriptions
            .fields
            .get(&(canonical_table.clone(), canonical_col.clone()));
        insert_field(
            &conn,
            FieldInsert {
                entity: &canonical_table,
                field: &canonical_col,
                db_column: &col.column,
                field_type: &col.data_type,
                display_label: field_description
                    .and_then(|description| description.display_label.as_deref()),
                enum_canonical: None,
                unit_canonical: None,
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_field({}.{}): {e}", col.table, col.column))?;
        summary.field_count += 1;
        if field_description
            .and_then(|description| description.display_label.as_ref())
            .is_some()
        {
            summary.field_description_count += 1;
        }

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
        .map_err(|e| anyhow::anyhow!("insert_vocab(field {}.{}): {e}", col.table, col.column))?;
        summary.vocab_count += 1;

        if let Some(description) = field_description {
            for term in description.vocab_terms() {
                insert_vocab(
                    &conn,
                    VocabInsert {
                        term: &term,
                        canonical_kind: "field",
                        canonical_value: &field_vocab_value,
                        confidence: 0.85,
                        source_layer: SOURCE_LAYER_DB_DESCRIPTION,
                        source_locator: Some(description.locator_json.as_str()),
                    },
                )
                .map_err(|e| {
                    anyhow::anyhow!(
                        "insert_vocab(description {}.{} term `{term}`): {e}",
                        col.table,
                        col.column
                    )
                })?;
                summary.vocab_count += 1;
            }
            for predicate in &description.value_predicates {
                let scope = format!(
                    "{field_vocab_value}.{}",
                    to_canonical_snake(&predicate.term).unwrap_or_else(|| "value".to_string())
                );
                let canonical_value = serde_json::json!({
                    "scope": scope,
                    "field": field_vocab_value,
                    "operator": predicate.operator,
                    "rawValue": predicate.raw_value,
                })
                .to_string();
                insert_vocab(
                    &conn,
                    VocabInsert {
                        term: &predicate.term,
                        canonical_kind: "scope_predicate",
                        canonical_value: &canonical_value,
                        confidence: 0.88,
                        source_layer: SOURCE_LAYER_DB_DESCRIPTION,
                        source_locator: Some(description.locator_json.as_str()),
                    },
                )
                .map_err(|e| {
                    anyhow::anyhow!(
                        "insert_vocab(value description {}.{} term `{}`): {e}",
                        col.table,
                        col.column,
                        predicate.term
                    )
                })?;
                summary.vocab_count += 1;
                summary.value_description_predicate_count += 1;
            }
        }
    }

    let foreign_keys = intro
        .list_foreign_keys()
        .await
        .map_err(|e| anyhow::anyhow!("list_foreign_keys: {e}"))?;
    let mut relationship_keys: HashSet<(String, String, String, String)> = HashSet::new();
    for fk in foreign_keys {
        let Some(from_entity) = canonicalize_table_name(&fk.from_table) else {
            tracing::warn!(table = %fk.from_table, "skipping FK â€” from table not canonicalisable");
            continue;
        };
        let Some(from_field) = canonicalize_identifier(&fk.from_column) else {
            tracing::warn!(column = %fk.from_column, "skipping FK â€” from column not canonicalisable");
            continue;
        };
        let Some(to_entity) = canonicalize_table_name(&fk.to_table) else {
            tracing::warn!(table = %fk.to_table, "skipping FK â€” to table not canonicalisable");
            continue;
        };
        let Some(to_field) = canonicalize_identifier(&fk.to_column) else {
            tracing::warn!(column = %fk.to_column, "skipping FK â€” to column not canonicalisable");
            continue;
        };
        let key = (
            from_entity.clone(),
            from_field.clone(),
            to_entity.clone(),
            to_field.clone(),
        );
        if !relationship_keys.insert(key) {
            continue;
        }
        insert_relationship(
            &conn,
            RelationshipInsert {
                from_entity: &from_entity,
                from_field: &from_field,
                to_entity: &to_entity,
                to_field: &to_field,
                kind: "many_to_one",
                relation_name: None,
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_relationship({from_entity}->{to_entity}): {e}"))?;
        summary.relationship_count += 1;
    }

    for (from_entity, from_field, to_entity, to_field) in infer_name_matched_relationships(&columns)
    {
        let key = (
            from_entity.clone(),
            from_field.clone(),
            to_entity.clone(),
            to_field.clone(),
        );
        if !relationship_keys.insert(key) {
            continue;
        }
        insert_relationship(
            &conn,
            RelationshipInsert {
                from_entity: &from_entity,
                from_field: &from_field,
                to_entity: &to_entity,
                to_field: &to_field,
                kind: "many_to_one",
                relation_name: None,
            },
        )
        .map_err(|e| anyhow::anyhow!("insert_relationship({from_entity}->{to_entity}): {e}"))?;
        summary.relationship_count += 1;
    }

    if options.sample_values {
        for col in &columns {
            if is_likely_pii_column(&col.table, &col.column) {
                continue;
            }
            let Some(entity) = canonicalize_table_name(&col.table) else {
                continue;
            };
            let Some(field) = canonicalize_identifier(&col.column) else {
                continue;
            };
            let values = intro
                .sample_values(
                    &col.table,
                    &col.column,
                    sample_limit_for_column(&col.table, &col.column, &col.data_type),
                )
                .await
                .unwrap_or_default();
            if values.is_empty() {
                continue;
            }
            let examples_json = serde_json::to_string(&values)
                .map_err(|e| anyhow::anyhow!("sample_values json: {e}"))?;
            let field_canonical = format!("{entity}.{field}");
            insert_sample_values(
                &conn,
                SampleValuesInsert {
                    field_canonical: &field_canonical,
                    examples_json: &examples_json,
                    pii_redacted: false,
                },
            )
            .map_err(|e| anyhow::anyhow!("insert_sample_values({field_canonical}): {e}"))?;
            summary.sample_value_count += 1;
        }
    }

    let schema_hash = compute_schema_hash(&summary, &tables);
    stamp_metadata(&conn, &derive_app_name_for(db_url), &schema_hash)
        .map_err(|e| anyhow::anyhow!("stamp_metadata: {e}"))?;
    conn.execute_batch("COMMIT")
        .context("commit SemanticGraph write transaction")?;

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
    #[cfg(feature = "mysql")]
    {
        if db_url.starts_with("mysql:") || db_url.starts_with("mariadb:") {
            let intro = MySqlIntrospect::connect(db_url)
                .await
                .map_err(|e| anyhow::anyhow!("mysql connect: {e}"))?;
            return Ok(Box::new(intro));
        }
    }
    anyhow::bail!(
        "unsupported --db-url scheme `{db_url}`; expected sqlite:, postgres:, postgresql:, mysql:, or mariadb:"
    )
}

/// Auto-detect the conventional schema-description directory under a project
/// or benchmark DB directory.
pub fn discover_schema_description_dir(project_path: &Path) -> Option<PathBuf> {
    [
        "database_description",
        "schema_description",
        "schema_descriptions",
        "db_description",
    ]
    .into_iter()
    .map(|name| project_path.join(name))
    .find(|candidate| candidate.is_dir())
}

#[derive(Debug, Default)]
struct SchemaDescriptionIndex {
    fields: HashMap<(String, String), SchemaFieldDescription>,
}

#[derive(Debug, Clone)]
struct SchemaFieldDescription {
    display_label: Option<String>,
    aliases: Vec<String>,
    value_predicates: Vec<SchemaValuePredicateDescription>,
    locator_json: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
struct SchemaValuePredicateDescription {
    term: String,
    operator: String,
    raw_value: String,
}

impl SchemaFieldDescription {
    fn vocab_terms(&self) -> Vec<String> {
        let mut terms = Vec::new();
        if let Some(label) = &self.display_label {
            push_description_term(&mut terms, label);
        }
        for alias in &self.aliases {
            push_description_term(&mut terms, alias);
        }
        terms.sort();
        terms.dedup();
        terms
    }
}

fn load_schema_description_index(dir: Option<&Path>) -> Result<SchemaDescriptionIndex> {
    let Some(dir) = dir else {
        return Ok(SchemaDescriptionIndex::default());
    };
    if !dir.is_dir() {
        anyhow::bail!(
            "schema description directory does not exist: {}",
            dir.display()
        );
    }

    let mut index = SchemaDescriptionIndex::default();
    for entry in std::fs::read_dir(dir)
        .with_context(|| format!("read schema description dir {}", dir.display()))?
    {
        let entry = entry.with_context(|| format!("read entry under {}", dir.display()))?;
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("csv") {
            continue;
        }
        ingest_schema_description_csv(&path, &mut index)
            .with_context(|| format!("ingest schema description CSV {}", path.display()))?;
    }
    Ok(index)
}

fn ingest_schema_description_csv(path: &Path, index: &mut SchemaDescriptionIndex) -> Result<()> {
    let file_table = path
        .file_stem()
        .and_then(|stem| stem.to_str())
        .and_then(canonicalize_table_name);
    let csv_bytes = std::fs::read(path)
        .with_context(|| format!("read schema description CSV {}", path.display()))?;
    let csv_text = String::from_utf8_lossy(&csv_bytes).into_owned();
    let mut reader = csv::ReaderBuilder::new()
        .flexible(true)
        .trim(csv::Trim::All)
        .from_reader(Cursor::new(csv_text));
    let headers = reader
        .headers()
        .with_context(|| format!("read CSV headers {}", path.display()))?
        .clone();

    let table_idx = csv_header_index(&headers, &["table", "table_name", "entity", "entity_name"]);
    let original_column_idx = csv_header_index(
        &headers,
        &[
            "original_column_name",
            "original_column",
            "db_column",
            "database_column",
            "column",
            "field",
            "field_name",
        ],
    )
    .or_else(|| csv_header_index(&headers, &["column_name"]));
    let label_idx = csv_header_index(
        &headers,
        &[
            "display_label",
            "label",
            "business_name",
            "friendly_name",
            "column_label",
            "column_name",
        ],
    );
    let description_idx = csv_header_index(
        &headers,
        &["column_description", "description", "comment", "remarks"],
    );
    let value_description_idx = csv_header_index(
        &headers,
        &[
            "value_description",
            "value_desc",
            "value_meaning",
            "value_dictionary",
            "allowed_values",
            "values",
        ],
    );

    for (row_idx, row) in reader.records().enumerate() {
        let row = row.with_context(|| format!("read CSV row {}", row_idx + 2))?;
        let Some(table) = table_idx
            .and_then(|idx| csv_cell(&row, idx))
            .and_then(canonicalize_table_name)
            .or_else(|| file_table.clone())
        else {
            continue;
        };
        let Some(field) = original_column_idx
            .and_then(|idx| csv_cell(&row, idx))
            .and_then(canonicalize_identifier)
        else {
            continue;
        };

        let display_label = label_idx
            .and_then(|idx| csv_cell(&row, idx))
            .and_then(normalize_schema_label);
        let description_alias = description_idx
            .and_then(|idx| csv_cell(&row, idx))
            .and_then(normalize_schema_description_alias);
        let value_predicates = value_description_idx
            .and_then(|idx| csv_cell(&row, idx))
            .map(|value_description| {
                parse_schema_value_predicates(
                    value_description,
                    &field,
                    display_label.as_deref(),
                    description_alias.as_deref(),
                )
            })
            .unwrap_or_default();
        if display_label.is_none() && description_alias.is_none() && value_predicates.is_empty() {
            continue;
        }

        let locator_json = serde_json::json!({
            "file": path.to_string_lossy(),
            "line": row_idx + 2,
            "layer": SOURCE_LAYER_DB_DESCRIPTION,
            "extractor": "db-description-csv",
        })
        .to_string();
        let entry = index
            .fields
            .entry((table, field))
            .or_insert_with(|| SchemaFieldDescription {
                display_label: None,
                aliases: Vec::new(),
                value_predicates: Vec::new(),
                locator_json: locator_json.clone(),
            });
        if entry.display_label.is_none() {
            entry.display_label = display_label;
        }
        if let Some(alias) = description_alias {
            push_description_term(&mut entry.aliases, &alias);
        }
        for predicate in value_predicates {
            push_schema_value_predicate_description(&mut entry.value_predicates, predicate);
        }
    }
    Ok(())
}

fn csv_header_index(headers: &csv::StringRecord, names: &[&str]) -> Option<usize> {
    headers.iter().position(|header| {
        let normalized = normalize_csv_header(header);
        names
            .iter()
            .any(|name| normalized == normalize_csv_header(name))
    })
}

fn normalize_csv_header(value: &str) -> String {
    value
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn csv_cell(record: &csv::StringRecord, idx: usize) -> Option<&str> {
    record
        .get(idx)
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn normalize_schema_label(raw: &str) -> Option<String> {
    normalize_schema_term(raw).filter(|term| schema_label_is_useful(term))
}

fn normalize_schema_description_alias(raw: &str) -> Option<String> {
    let term = normalize_schema_term(raw)?;
    if !schema_label_is_useful(&term) {
        return None;
    }
    let token_count = term.split_whitespace().count();
    let has_risky_prose = term.contains('<')
        || term.contains('>')
        || term.contains(';')
        || term.contains(':')
        || term.contains(',')
        || term.contains(" or ")
        || term.contains(" and ");
    (token_count <= 5 && !has_risky_prose).then_some(term)
}

fn normalize_schema_term(raw: &str) -> Option<String> {
    let mut out = String::new();
    let mut last_space = true;
    for ch in raw.trim().trim_matches('"').chars() {
        if ch == '_' || ch == '-' || ch.is_whitespace() {
            if !last_space {
                out.push(' ');
                last_space = true;
            }
        } else {
            out.push(ch);
            last_space = false;
        }
    }
    let term = out.trim().to_string();
    (!term.is_empty()).then_some(term)
}

fn schema_label_is_useful(term: &str) -> bool {
    let lower = term.to_ascii_lowercase();
    if matches!(
        lower.as_str(),
        "null" | "none" | "n/a" | "na" | "not useful" | "unknown" | "unused"
    ) {
        return false;
    }
    term.chars().filter(|ch| ch.is_ascii_alphanumeric()).count() >= 2
}

fn push_description_term(out: &mut Vec<String>, raw: &str) {
    if let Some(term) = normalize_schema_term(raw) {
        if schema_label_is_useful(&term) && !out.iter().any(|existing| existing == &term) {
            out.push(term.to_ascii_lowercase());
        }
    }
}

fn parse_schema_value_predicates(
    raw: &str,
    canonical_field: &str,
    display_label: Option<&str>,
    description_alias: Option<&str>,
) -> Vec<SchemaValuePredicateDescription> {
    let field_terms = schema_value_field_terms(canonical_field, display_label, description_alias);
    let mut out = Vec::new();
    for segment in schema_value_description_segments(raw) {
        if segment_value_is_noise(&segment) {
            continue;
        }
        if let Some((raw_value, meaning)) = split_schema_value_mapping(&segment) {
            let Some(raw_value) = normalize_schema_raw_value(&raw_value) else {
                continue;
            };
            for term in schema_value_terms(&raw_value, &meaning, &field_terms) {
                push_schema_value_predicate(&mut out, &term, "=", &raw_value);
            }
        } else if schema_value_bare_term_is_safe(&segment) {
            let Some(term) = normalize_schema_value_term(&segment) else {
                continue;
            };
            push_schema_value_predicate(&mut out, &term, "=", &term);
        }
    }
    out.sort_by(|a, b| {
        a.term
            .cmp(&b.term)
            .then_with(|| a.raw_value.cmp(&b.raw_value))
            .then_with(|| a.operator.cmp(&b.operator))
    });
    out.dedup();
    out
}

fn schema_value_description_segments(raw: &str) -> Vec<String> {
    raw.replace(['\r', '\t'], "\n")
        .replace(['•', '·', '●', '▪'], "\n")
        .split(['\n', ';'])
        .filter_map(clean_schema_value_segment)
        .filter(|term| !term.is_empty())
        .collect()
}

fn clean_schema_value_segment(raw: &str) -> Option<String> {
    let term = raw
        .trim()
        .trim_matches('"')
        .trim()
        .replace(['\u{2013}', '\u{2014}'], "-");
    (!term.is_empty()).then_some(term.to_string())
}

fn split_schema_value_mapping(segment: &str) -> Option<(String, String)> {
    for delimiter in [" = ", "=", ":", " - "] {
        if let Some((left, right)) = segment.split_once(delimiter) {
            if schema_value_code_is_safe(left) && schema_value_meaning_is_safe(right) {
                return Some((left.trim().to_string(), right.trim().to_string()));
            }
        }
    }
    None
}

fn schema_value_code_is_safe(raw: &str) -> bool {
    let value = raw.trim().trim_matches(['"', '\'', '`']);
    if value.is_empty() || value.len() > 32 {
        return false;
    }
    let alnum = value
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .count();
    alnum > 0 && value.split_whitespace().count() <= 4
}

fn schema_value_meaning_is_safe(raw: &str) -> bool {
    if schema_boolean_value(raw).is_some() {
        return true;
    }
    let Some(term) = normalize_schema_term(raw) else {
        return false;
    };
    schema_label_is_useful(&term) && !segment_value_is_noise(&term)
}

fn normalize_schema_raw_value(raw: &str) -> Option<String> {
    let value = raw.trim().trim_matches(['"', '\'', '`']).trim();
    if value.is_empty() || value.len() > 64 {
        return None;
    }
    Some(value.to_string())
}

fn schema_value_terms(raw_value: &str, meaning: &str, field_terms: &[String]) -> Vec<String> {
    let mut terms = Vec::new();
    if let Some(term) = normalize_schema_value_term(meaning) {
        push_description_term(&mut terms, &term);
        for variant in schema_value_phrase_variants(&term) {
            push_description_term(&mut terms, &variant);
        }
    }
    if schema_raw_value_is_semantic(raw_value) {
        push_description_term(&mut terms, raw_value);
    }
    if let Some(value) =
        schema_boolean_value(meaning).or_else(|| schema_compact_boolean_value(raw_value))
    {
        for field_term in field_terms {
            if value {
                push_description_term(&mut terms, field_term);
            } else {
                push_description_term(&mut terms, &format!("not {field_term}"));
                push_description_term(&mut terms, &format!("non {field_term}"));
            }
        }
    }
    terms.sort();
    terms.dedup();
    terms
}

fn normalize_schema_value_term(raw: &str) -> Option<String> {
    let mut term = normalize_schema_term(schema_value_term_head(raw))?;
    for delimiter in [" - ", " – ", " — ", ". "] {
        if let Some((head, _)) = term.split_once(delimiter) {
            if head.split_whitespace().count() <= 8 {
                term = head.trim().to_string();
            }
        }
    }
    let lower = term.to_ascii_lowercase();
    for prefix in [
        "the field is coded as follows",
        "values are as follows",
        "definitions of the valid status types are listed below",
    ] {
        if lower == prefix {
            return None;
        }
    }
    schema_label_is_useful(&term).then_some(term.to_ascii_lowercase())
}

fn schema_value_term_head(raw: &str) -> &str {
    let mut head = raw.trim();
    for delimiter in [" - ", ". "] {
        if let Some((candidate, _)) = head.split_once(delimiter) {
            if candidate.split_whitespace().count() <= 8 {
                head = candidate.trim();
            }
        }
    }
    head
}

fn schema_value_phrase_variants(term: &str) -> Vec<String> {
    let mut variants = Vec::new();
    let lower = term.to_ascii_lowercase();
    for (needle, prefix) in [
        (" is not a ", "not "),
        (" is not an ", "not "),
        (" is not ", "not "),
        (" is a ", ""),
        (" is an ", ""),
        (" is ", ""),
    ] {
        if let Some((_, suffix)) = lower.split_once(needle) {
            let candidate = format!("{prefix}{}", suffix.trim());
            if candidate.split_whitespace().count() <= 5 {
                variants.push(candidate);
            }
        }
    }
    if let Some(stripped) = lower.strip_prefix("not ") {
        if stripped.split_whitespace().count() <= 5 {
            variants.push(format!("not {}", stripped.trim()));
        }
    }
    variants
}

fn schema_raw_value_is_semantic(raw_value: &str) -> bool {
    if schema_boolean_value(raw_value).is_some() {
        return false;
    }
    let normalized = raw_value.trim();
    if normalized.len() < 2 {
        return false;
    }
    normalized.chars().any(|ch| ch.is_ascii_alphabetic())
}

fn schema_boolean_value(raw: &str) -> Option<bool> {
    let term = normalize_schema_term(raw)?;
    match term.to_ascii_lowercase().as_str() {
        "1" | "y" | "yes" | "true" | "t" => Some(true),
        "0" | "n" | "no" | "false" | "f" => Some(false),
        _ => None,
    }
}

fn schema_compact_boolean_value(raw: &str) -> Option<bool> {
    let term = normalize_schema_term(raw)?;
    match term.to_ascii_lowercase().as_str() {
        "1" | "y" | "yes" | "true" => Some(true),
        "0" | "n" | "no" | "false" => Some(false),
        _ => None,
    }
}

fn schema_value_field_terms(
    canonical_field: &str,
    display_label: Option<&str>,
    description_alias: Option<&str>,
) -> Vec<String> {
    let mut terms = Vec::new();
    if let Some(label) = display_label {
        push_schema_value_field_term(&mut terms, label);
    }
    if let Some(alias) = description_alias {
        push_schema_value_field_term(&mut terms, alias);
    }
    if let Some(label) = humanize_identifier(canonical_field) {
        push_schema_value_field_term(&mut terms, &label);
    }
    terms.sort();
    terms.dedup();
    terms
}

fn push_schema_value_field_term(out: &mut Vec<String>, raw: &str) {
    let Some(term) = normalize_schema_value_field_term(raw) else {
        return;
    };
    push_description_term(out, &term);
}

fn normalize_schema_value_field_term(raw: &str) -> Option<String> {
    let mut term = normalize_schema_term(raw)?.to_ascii_lowercase();
    for marker in ["(", "["] {
        if let Some((head, _)) = term.split_once(marker) {
            term = head.trim().to_string();
        }
    }
    for suffix in [
        " y n",
        " yes no",
        " true false",
        " flag",
        " indicator",
        " code",
        " type",
    ] {
        if term.ends_with(suffix) {
            term.truncate(term.len() - suffix.len());
            term = term.trim().to_string();
        }
    }
    schema_label_is_useful(&term).then_some(term)
}

fn segment_value_is_noise(segment: &str) -> bool {
    let lower = segment.to_ascii_lowercase();
    lower.starts_with("note")
        || lower.starts_with("commonsense evidence")
        || lower.starts_with("for example")
        || lower.contains("not useful")
}

fn schema_value_bare_term_is_safe(segment: &str) -> bool {
    let Some(term) = normalize_schema_value_term(segment) else {
        return false;
    };
    let token_count = term.split_whitespace().count();
    (1..=7).contains(&token_count)
        && !term.contains(':')
        && !term.contains('=')
        && !term.contains(" field ")
        && !term.contains(" note ")
}

fn push_schema_value_predicate(
    out: &mut Vec<SchemaValuePredicateDescription>,
    term: &str,
    operator: &str,
    raw_value: &str,
) {
    let Some(term) = normalize_schema_value_term(term) else {
        return;
    };
    if term.split_whitespace().count() > 8 {
        return;
    }
    let predicate = SchemaValuePredicateDescription {
        term,
        operator: operator.to_string(),
        raw_value: raw_value.to_string(),
    };
    push_schema_value_predicate_description(out, predicate);
}

fn push_schema_value_predicate_description(
    out: &mut Vec<SchemaValuePredicateDescription>,
    predicate: SchemaValuePredicateDescription,
) {
    if !out.iter().any(|existing| existing == &predicate) {
        out.push(predicate);
    }
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
pub fn ingest_vocab_jsonl(graph_out: &Path, jsonl: &Path) -> Result<JsonlIngestSummary> {
    let conn = open(graph_out).context("open graph for vocab ingest")?;
    let resolver = GraphCanonicalResolver::load(graph_out)?;
    let mut relationship_keys = semsql_graph::read::relationships(graph_out)?
        .into_iter()
        .map(|relationship| {
            (
                relationship.from_entity,
                relationship.from_field,
                relationship.to_entity,
                relationship.to_field,
                relationship.kind,
            )
        })
        .collect::<HashSet<_>>();
    let text = std::fs::read_to_string(jsonl)
        .with_context(|| format!("read JSONL {}", jsonl.display()))?;

    let mut summary = JsonlIngestSummary::default();
    for (lineno, line) in text.lines().enumerate() {
        let line = line.trim();
        let line = line.strip_prefix('\u{feff}').unwrap_or(line).trim_start();
        if line.is_empty() {
            continue;
        }
        let raw: serde_json::Value = serde_json::from_str(line)
            .with_context(|| format!("{}:{}: invalid JSON", jsonl.display(), lineno + 1))?;
        if raw.get("record_kind").and_then(serde_json::Value::as_str) == Some("metric_definition")
            || raw.get("recordKind").and_then(serde_json::Value::as_str)
                == Some("metric_definition")
        {
            if ingest_metric_definition_fragment(&conn, &resolver, raw, lineno + 1)? {
                summary.metric_definition_count += 1;
            }
            continue;
        }
        let frag: TsVocabFragment = serde_json::from_value(raw).with_context(|| {
            format!(
                "{}:{}: invalid VocabFragment JSON",
                jsonl.display(),
                lineno + 1
            )
        })?;
        let kind_value = match frag.canonical {
            TsCanonical::Entity { entity } => {
                let entity = entity.to_ascii_lowercase();
                let Some(entity) = resolver.resolve_entity(&entity) else {
                    tracing::warn!(line = lineno + 1, value = %entity, "rejecting non-canonical entity name");
                    continue;
                };
                ("entity".to_string(), entity)
            }
            TsCanonical::Field { field } => {
                let field = field.to_ascii_lowercase();
                let Some(field) = resolver.resolve_field(&field) else {
                    tracing::warn!(line = lineno + 1, value = %field, "rejecting non-canonical field name");
                    continue;
                };
                ("field".to_string(), field)
            }
            TsCanonical::EnumValue {
                enum_name,
                raw_value,
            } => {
                let enum_name = enum_name.to_ascii_lowercase();
                let Some(enum_name) = resolver.resolve_field(&enum_name) else {
                    tracing::warn!(line = lineno + 1, value = %enum_name, "rejecting non-canonical enum name");
                    continue;
                };
                ("enum_value".to_string(), format!("{enum_name}:{raw_value}"))
            }
            TsCanonical::ScopePredicate {
                scope,
                field,
                operator,
                raw_value,
            } => {
                let scope = scope.to_ascii_lowercase();
                let field = field.to_ascii_lowercase();
                let Some(scope) = resolver.resolve_scope(&scope) else {
                    tracing::warn!(
                        line = lineno + 1,
                        "rejecting scope predicate with non-canonical scope"
                    );
                    continue;
                };
                let Some(field) = resolver.resolve_field(&field) else {
                    tracing::warn!(
                        line = lineno + 1,
                        "rejecting scope predicate with non-canonical field"
                    );
                    continue;
                };
                if !matches!(
                    operator.as_str(),
                    "=" | "==" | "!=" | "<>" | ">" | ">=" | "<" | "<="
                ) {
                    tracing::warn!(line = lineno + 1, value = %operator, "rejecting scope predicate with unsupported operator");
                    continue;
                }
                let canonical_value = serde_json::json!({
                    "scope": scope,
                    "field": field,
                    "operator": operator,
                    "rawValue": raw_value,
                })
                .to_string();
                ("scope_predicate".to_string(), canonical_value)
            }
            TsCanonical::Relationship {
                from,
                to,
                from_field,
                to_field,
                relationship_kind,
                relation_name,
            } => {
                let from = from.to_ascii_lowercase();
                let to = to.to_ascii_lowercase();
                let Some(from) = resolver.resolve_entity(&from) else {
                    tracing::warn!(
                        line = lineno + 1,
                        "rejecting relationship with non-canonical from endpoint"
                    );
                    continue;
                };
                let Some(to) = resolver.resolve_entity(&to) else {
                    tracing::warn!(
                        line = lineno + 1,
                        "rejecting relationship with non-canonical to endpoint"
                    );
                    continue;
                };
                match (from_field, to_field) {
                    (Some(from_field), Some(to_field)) => {
                        let Some(from_field) =
                            resolver.resolve_field(&from_field.to_ascii_lowercase())
                        else {
                            tracing::warn!(
                                line = lineno + 1,
                                "rejecting relationship with non-canonical from field"
                            );
                            continue;
                        };
                        let Some(to_field) = resolver.resolve_field(&to_field.to_ascii_lowercase())
                        else {
                            tracing::warn!(
                                line = lineno + 1,
                                "rejecting relationship with non-canonical to field"
                            );
                            continue;
                        };
                        if !from_field.starts_with(&format!("{from}."))
                            || !to_field.starts_with(&format!("{to}."))
                        {
                            tracing::warn!(
                                line = lineno + 1,
                                "rejecting relationship whose fields do not belong to its endpoints"
                            );
                            continue;
                        }
                        let kind = relationship_kind.unwrap_or_else(|| "many_to_one".to_string());
                        if !matches!(kind.as_str(), "many_to_one" | "one_to_many" | "one_to_one") {
                            tracing::warn!(
                                line = lineno + 1,
                                value = %kind,
                                "rejecting relationship with unsupported kind"
                            );
                            continue;
                        }
                        let from_field_name = from_field
                            .split_once('.')
                            .map(|(_, field)| field)
                            .unwrap_or(from_field.as_str());
                        let to_field_name = to_field
                            .split_once('.')
                            .map(|(_, field)| field)
                            .unwrap_or(to_field.as_str());
                        let (edge_from, edge_from_field, edge_to, edge_to_field, edge_kind) =
                            if kind == "one_to_many" {
                                (
                                    to.as_str(),
                                    to_field_name,
                                    from.as_str(),
                                    from_field_name,
                                    "many_to_one",
                                )
                            } else {
                                (
                                    from.as_str(),
                                    from_field_name,
                                    to.as_str(),
                                    to_field_name,
                                    kind.as_str(),
                                )
                            };
                        let relationship_key = (
                            edge_from.to_string(),
                            edge_from_field.to_string(),
                            edge_to.to_string(),
                            edge_to_field.to_string(),
                            edge_kind.to_string(),
                        );
                        if relationship_keys.insert(relationship_key) {
                            insert_relationship(
                                &conn,
                                RelationshipInsert {
                                    from_entity: edge_from,
                                    from_field: edge_from_field,
                                    to_entity: edge_to,
                                    to_field: edge_to_field,
                                    kind: edge_kind,
                                    relation_name: relation_name.as_deref(),
                                },
                            )
                            .with_context(|| {
                                format!(
                                    "{}:{}: ingest grounded relationship",
                                    jsonl.display(),
                                    lineno + 1
                                )
                            })?;
                            summary.relationship_count += 1;
                        }
                    }
                    (None, None) => {}
                    _ => {
                        tracing::warn!(
                            line = lineno + 1,
                            "rejecting relationship with only one join field"
                        );
                        continue;
                    }
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
            Ok(()) => summary.vocab_count += 1,
            Err(e) => tracing::warn!(
                line = lineno + 1,
                error = %e,
                "skipping vocab fragment that failed sanitiser"
            ),
        }
    }
    Ok(summary)
}

fn ingest_metric_definition_fragment(
    conn: &rusqlite::Connection,
    resolver: &GraphCanonicalResolver,
    raw: serde_json::Value,
    line: usize,
) -> Result<bool> {
    let frag: TsMetricDefinitionFragment = serde_json::from_value(raw)
        .with_context(|| format!("line {line}: invalid MetricDefinitionFragment JSON"))?;
    if !matches!(frag.metric_kind.as_str(), "conditional_rate" | "aggregate") {
        tracing::warn!(
            line,
            value = %frag.metric_kind,
            "rejecting unsupported metric definition kind"
        );
        return Ok(false);
    }
    let name = frag.name.to_ascii_lowercase();
    if !is_safe_canonical(&name) {
        tracing::warn!(line, value = %frag.name, "rejecting unsafe metric name");
        return Ok(false);
    }
    let subject = frag.subject_entity.to_ascii_lowercase();
    let Some(subject_entity) = resolver.resolve_entity(&subject) else {
        tracing::warn!(line, value = %frag.subject_entity, "rejecting metric with unknown subject entity");
        return Ok(false);
    };
    let (
        numerator_field,
        operator,
        numerator_value,
        numerator_value_kind,
        denominator_field,
        measure_field,
        aggregate,
    ) = if frag.metric_kind == "conditional_rate" {
        let Some(raw_numerator) = frag.numerator_field.as_ref() else {
            tracing::warn!(line, "rejecting rate metric without numerator field");
            return Ok(false);
        };
        let numerator = raw_numerator.to_ascii_lowercase();
        let Some(numerator_field) = resolver.resolve_field(&numerator) else {
            tracing::warn!(line, value = %raw_numerator, "rejecting metric with unknown numerator field");
            return Ok(false);
        };
        let Some(raw_denominator) = frag.denominator_field.as_ref() else {
            tracing::warn!(line, "rejecting rate metric without denominator field");
            return Ok(false);
        };
        let denominator = raw_denominator.to_ascii_lowercase();
        let Some(denominator_field) = resolver.resolve_field(&denominator) else {
            tracing::warn!(line, value = %raw_denominator, "rejecting metric with unknown denominator field");
            return Ok(false);
        };
        let operator = if frag.numerator_operator == "==" {
            "=".to_string()
        } else {
            frag.numerator_operator.clone()
        };
        if !matches!(
            operator.as_str(),
            "=" | "!=" | "<>" | ">" | ">=" | "<" | "<="
        ) {
            tracing::warn!(line, value = %operator, "rejecting metric with unsupported numerator operator");
            return Ok(false);
        }
        (
            numerator_field,
            operator,
            frag.numerator_value.clone(),
            frag.numerator_value_kind.clone(),
            denominator_field,
            None,
            None,
        )
    } else {
        let aggregate = frag
            .aggregate
            .as_deref()
            .unwrap_or("")
            .trim()
            .to_ascii_uppercase();
        if !matches!(aggregate.as_str(), "AVG" | "COUNT" | "MAX" | "MIN" | "SUM") {
            tracing::warn!(line, value = %aggregate, "rejecting metric with unsupported aggregate");
            return Ok(false);
        }
        if frag.distinct && aggregate != "COUNT" {
            tracing::warn!(
                line,
                value = %aggregate,
                "rejecting distinct aggregate metric unless aggregate is COUNT"
            );
            return Ok(false);
        }
        let Some(raw_measure) = frag.measure_field.as_ref() else {
            tracing::warn!(line, "rejecting aggregate metric without measure field");
            return Ok(false);
        };
        let measure = raw_measure.to_ascii_lowercase();
        let Some(measure_field) = resolver.resolve_field(&measure) else {
            tracing::warn!(line, value = %raw_measure, "rejecting metric with unknown measure field");
            return Ok(false);
        };
        let raw_denominator = frag.denominator_field.as_ref().unwrap_or(raw_measure);
        let denominator = raw_denominator.to_ascii_lowercase();
        let Some(denominator_field) = resolver.resolve_field(&denominator) else {
            tracing::warn!(line, value = %raw_denominator, "rejecting metric with unknown denominator field");
            return Ok(false);
        };
        (
            measure_field.clone(),
            "=".to_string(),
            String::new(),
            "metric_definition".to_string(),
            denominator_field,
            Some(measure_field),
            Some(aggregate),
        )
    };
    if frag.scale <= 0.0 || !frag.scale.is_finite() {
        tracing::warn!(
            line,
            value = frag.scale,
            "rejecting metric with invalid scale"
        );
        return Ok(false);
    }
    let mut required_entities = Vec::new();
    for entity in frag.required_entities {
        let entity = entity.to_ascii_lowercase();
        let Some(entity) = resolver.resolve_entity(&entity) else {
            tracing::warn!(line, "rejecting metric with unknown required entity");
            return Ok(false);
        };
        if !required_entities.iter().any(|existing| existing == &entity) {
            required_entities.push(entity);
        }
    }
    if !required_entities
        .iter()
        .any(|entity| entity == &subject_entity)
    {
        required_entities.push(subject_entity.clone());
    }
    for field in [
        numerator_field.as_str(),
        denominator_field.as_str(),
        measure_field.as_deref().unwrap_or(""),
    ] {
        let Some((entity, _field)) = field.split_once('.') else {
            continue;
        };
        if !required_entities.iter().any(|existing| existing == entity) {
            required_entities.push(entity.to_string());
        }
    }
    let required_entities_json = serde_json::to_string(&required_entities)
        .context("serialising metric required entities")?;
    let aliases_json =
        serde_json::to_string(&frag.aliases).context("serialising metric aliases")?;
    let locator_json = serde_json::to_string(&frag.locator).ok();
    let attempt = insert_metric_definition(
        conn,
        MetricDefinitionInsert {
            name: &name,
            display_label: frag.display_label.as_deref(),
            metric_kind: &frag.metric_kind,
            subject_entity: &subject_entity,
            numerator_field: &numerator_field,
            numerator_operator: &operator,
            numerator_value: &numerator_value,
            numerator_value_kind: &numerator_value_kind,
            denominator_field: &denominator_field,
            scale: frag.scale,
            measure_field: measure_field.as_deref(),
            aggregate: aggregate.as_deref(),
            distinct_measure: frag.metric_kind == "aggregate" && frag.distinct,
            required_entities_json: &required_entities_json,
            aliases_json: &aliases_json,
            source_locator: locator_json.as_deref(),
        },
    );
    match attempt {
        Ok(()) => Ok(true),
        Err(e) => {
            tracing::warn!(
                line,
                error = %e,
                "skipping metric definition fragment that failed sanitiser"
            );
            Ok(false)
        }
    }
}

#[derive(Debug)]
struct GraphCanonicalResolver {
    entities: HashSet<String>,
    entity_aliases: HashMap<String, String>,
    fields: HashSet<String>,
}

impl GraphCanonicalResolver {
    fn load(graph_out: &Path) -> Result<Self> {
        let entity_rows = semsql_graph::read::entities(graph_out)
            .map_err(|e| anyhow::anyhow!("read graph entities for vocab ingest: {e}"))?;
        let field_rows = semsql_graph::read::fields(graph_out)
            .map_err(|e| anyhow::anyhow!("read graph fields for vocab ingest: {e}"))?;
        let mut entities = HashSet::new();
        let mut entity_aliases = HashMap::new();
        for row in entity_rows {
            let canonical = row.canonical_name.to_ascii_lowercase();
            entities.insert(canonical.clone());
            insert_entity_alias(&mut entity_aliases, &canonical, &canonical);
            if let Some(label) = row.singular_label {
                insert_entity_alias(&mut entity_aliases, &label, &canonical);
            }
            if let Some(label) = row.plural_label {
                insert_entity_alias(&mut entity_aliases, &label, &canonical);
            }
            if let Some(humanized) = humanize_identifier(&canonical) {
                insert_entity_alias(&mut entity_aliases, &humanized, &canonical);
                insert_entity_alias(
                    &mut entity_aliases,
                    &singularize_phrase(&humanized),
                    &canonical,
                );
            }
            for alias in irregular_entity_aliases(&canonical) {
                insert_entity_alias(&mut entity_aliases, alias, &canonical);
            }
        }
        let fields = field_rows
            .into_iter()
            .map(|row| format!("{}.{}", row.entity, row.field).to_ascii_lowercase())
            .collect();
        Ok(Self {
            entities,
            entity_aliases,
            fields,
        })
    }

    fn resolve_entity(&self, entity: &str) -> Option<String> {
        if !is_safe_canonical(entity) {
            return None;
        }
        if self.entities.contains(entity) {
            return Some(entity.to_string());
        }
        self.entity_aliases.get(entity).cloned()
    }

    fn resolve_field(&self, field: &str) -> Option<String> {
        if !is_safe_field(field) {
            return None;
        }
        if self.fields.contains(field) {
            return Some(field.to_string());
        }
        let (entity, field_name) = field.split_once('.')?;
        let resolved_entity = self.resolve_entity(entity)?;
        let resolved = format!("{resolved_entity}.{field_name}");
        self.fields.contains(&resolved).then_some(resolved)
    }

    fn resolve_scope(&self, scope: &str) -> Option<String> {
        if !is_safe_field(scope) {
            return None;
        }
        let (entity, scope_name) = scope.split_once('.')?;
        if !is_safe_canonical(scope_name) {
            return None;
        }
        let resolved_entity = self.resolve_entity(entity)?;
        Some(format!("{resolved_entity}.{scope_name}"))
    }
}

fn insert_entity_alias(aliases: &mut HashMap<String, String>, alias: &str, canonical: &str) {
    let Some(normalized) = normalize_entity_alias(alias) else {
        return;
    };
    match aliases.get(&normalized) {
        Some(existing) if existing != canonical => {
            aliases.remove(&normalized);
        }
        Some(_) => {}
        None => {
            aliases.insert(normalized, canonical.to_string());
        }
    }
}

fn normalize_entity_alias(alias: &str) -> Option<String> {
    let lowered = alias.trim().to_ascii_lowercase();
    if lowered.is_empty() {
        return None;
    }
    to_canonical_snake(&lowered).filter(|candidate| is_safe_canonical(candidate))
}

fn irregular_entity_aliases(canonical: &str) -> &'static [&'static str] {
    match canonical {
        "people" => &["person"],
        "children" => &["child"],
        "men" => &["man"],
        "women" => &["woman"],
        "criteria" => &["criterion"],
        "data" => &["datum"],
        _ => &[],
    }
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
#[serde(rename_all = "camelCase")]
struct TsMetricDefinitionFragment {
    #[serde(default, rename = "record_kind", alias = "recordKind")]
    _record_kind: Option<String>,
    name: String,
    #[serde(default)]
    display_label: Option<String>,
    metric_kind: String,
    subject_entity: String,
    #[serde(default)]
    numerator_field: Option<String>,
    #[serde(default = "default_eq_operator")]
    numerator_operator: String,
    #[serde(default)]
    numerator_value: String,
    #[serde(default = "default_literal_value_kind")]
    numerator_value_kind: String,
    #[serde(default)]
    denominator_field: Option<String>,
    #[serde(default)]
    measure_field: Option<String>,
    #[serde(default)]
    aggregate: Option<String>,
    #[serde(default)]
    distinct: bool,
    #[serde(default = "default_metric_scale")]
    scale: f64,
    #[serde(default)]
    required_entities: Vec<String>,
    #[serde(default)]
    aliases: Vec<String>,
    locator: TsLocator,
}

fn default_eq_operator() -> String {
    "=".to_string()
}

fn default_literal_value_kind() -> String {
    "literal".to_string()
}

fn default_metric_scale() -> f64 {
    100.0
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
    ScopePredicate {
        scope: String,
        field: String,
        operator: String,
        #[serde(rename = "rawValue")]
        raw_value: String,
    },
    Relationship {
        from: String,
        to: String,
        #[serde(default, rename = "fromField")]
        from_field: Option<String>,
        #[serde(default, rename = "toField")]
        to_field: Option<String>,
        #[serde(default, rename = "relationshipKind")]
        relationship_kind: Option<String>,
        #[serde(default, rename = "relationName")]
        relation_name: Option<String>,
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

fn canonicalize_table_name(table: &str) -> Option<String> {
    let raw = table.split_once('.').map(|(_, t)| t).unwrap_or(table);
    canonicalize_identifier(raw)
}

fn canonicalize_identifier(raw: &str) -> Option<String> {
    if is_safe_canonical(raw) {
        Some(raw.to_lowercase())
    } else {
        to_canonical_snake(raw)
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

fn entity_labels_from_canonical(canonical: &str) -> (Option<String>, Option<String>) {
    let Some(plural) = humanize_identifier(canonical) else {
        return (None, None);
    };
    let singular = singularize_phrase(&plural);
    let singular = (singular != plural).then_some(singular);
    (singular, Some(plural))
}

fn entity_vocab_terms(
    canonical: &str,
    singular_label: Option<&str>,
    plural_label: Option<&str>,
) -> Vec<String> {
    let mut terms = vec![canonical.to_ascii_lowercase()];
    if let Some(label) = plural_label {
        terms.push(label.to_ascii_lowercase());
    }
    if let Some(label) = singular_label {
        terms.push(label.to_ascii_lowercase());
    }
    terms.sort();
    terms.dedup();
    terms
}

fn humanize_identifier(value: &str) -> Option<String> {
    let parts: Vec<&str> = value
        .split('_')
        .map(str::trim)
        .filter(|part| !part.is_empty())
        .collect();
    if parts.is_empty() {
        None
    } else {
        Some(parts.join(" "))
    }
}

fn singularize_phrase(phrase: &str) -> String {
    let mut parts: Vec<String> = phrase.split_whitespace().map(str::to_string).collect();
    let Some(last) = parts.pop() else {
        return phrase.to_string();
    };
    let singular_last = if let Some(stem) = last.strip_suffix("ies") {
        format!("{stem}y")
    } else if let Some(stem) = last.strip_suffix("ses") {
        format!("{stem}s")
    } else if last.ends_with('s') && last.len() > 1 {
        last.trim_end_matches('s').to_string()
    } else {
        last
    };
    parts.push(singular_last);
    parts.join(" ")
}

fn infer_name_matched_relationships(
    columns: &[ColumnIntro],
) -> Vec<(String, String, String, String)> {
    let mut key_field_owners: HashMap<String, Vec<(String, String)>> = HashMap::new();
    let mut canonical_columns: Vec<(String, String)> = Vec::new();

    for col in columns {
        let Some(entity) = canonicalize_table_name(&col.table) else {
            continue;
        };
        let Some(field) = canonicalize_identifier(&col.column) else {
            continue;
        };
        for reference_name in owner_reference_field_names(&entity, &field) {
            key_field_owners
                .entry(reference_name)
                .or_default()
                .push((entity.clone(), field.clone()));
        }
        canonical_columns.push((entity, field));
    }

    let mut out = Vec::new();
    let mut seen: HashSet<(String, String, String, String)> = HashSet::new();
    for (from_entity, from_field) in canonical_columns {
        if reference_field_is_too_generic(&from_field) {
            continue;
        }
        let Some(owners) = key_field_owners.get(&from_field) else {
            continue;
        };
        let targets: Vec<&(String, String)> = owners
            .iter()
            .filter(|(entity, _)| !entity.eq_ignore_ascii_case(&from_entity))
            .collect();
        if targets.len() != 1 {
            continue;
        }
        let (to_entity, to_field) = targets[0];
        let key = (
            from_entity.clone(),
            from_field.clone(),
            to_entity.clone(),
            to_field.clone(),
        );
        if seen.insert(key.clone()) {
            out.push(key);
        }
    }
    out
}

fn owner_reference_field_names(entity: &str, field: &str) -> Vec<String> {
    let field_compact = field.replace('_', "");
    let mut out = Vec::new();
    if entity_stems(entity)
        .into_iter()
        .any(|stem| field_compact == format!("{stem}id"))
    {
        out.push(field_compact.clone());
    }
    if matches!(field_compact.as_str(), "code" | "id") {
        for stem in entity_stems(entity) {
            out.push(format!("{stem}{field_compact}"));
        }
    }
    out.sort();
    out.dedup();
    out
}

fn entity_stems(entity: &str) -> Vec<String> {
    let compact = entity.replace('_', "");
    let mut stems = vec![compact.clone()];
    if let Some(stem) = compact.strip_suffix("ies") {
        stems.push(format!("{stem}y"));
    }
    if let Some(stem) = compact.strip_suffix('s') {
        stems.push(stem.to_string());
    }
    stems.sort();
    stems.dedup();
    stems
}

fn reference_field_is_too_generic(field: &str) -> bool {
    matches!(field.replace('_', "").as_str(), "id" | "code")
}

fn is_likely_pii_column(table: &str, column: &str) -> bool {
    let c = column.to_ascii_lowercase();
    let t = table.to_ascii_lowercase();
    let compact = c
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .collect::<String>();
    if [
        "email",
        "phone",
        "password",
        "passcode",
        "secret",
        "token",
        "address",
        "ssn",
        "credential",
        "encrypted",
        "cipher",
        "hash",
        "salt",
        "signature",
        "session",
        "cookie",
        "bearer",
        "oauth",
        "jwt",
        "otp",
        "totp",
        "mfa",
        "2fa",
    ]
    .iter()
    .any(|needle| c.contains(needle))
    {
        return true;
    }

    if [
        "apikey",
        "accesskey",
        "secretkey",
        "privatekey",
        "publickey",
        "sshkey",
        "encryptionkey",
        "refreshsecret",
        "clientsecret",
        "recoverycode",
    ]
    .iter()
    .any(|needle| compact.contains(needle))
    {
        return true;
    }

    if [
        "user_id",
        "userid",
        "account_id",
        "accountid",
        "customer_id",
        "customerid",
        "client_id",
        "clientid",
        "member_id",
        "memberid",
        "tenant_id",
        "tenantid",
        "owner_id",
        "ownerid",
    ]
    .iter()
    .any(|needle| c.contains(needle) || compact.contains(needle))
    {
        return true;
    }

    if column_name_looks_object_identifier(&c)
        && (table_name_looks_private_person(&t) || table_name_looks_credential_or_connection(&t))
    {
        return true;
    }

    if column_name_looks_person_name(&c) && !column_name_looks_business_or_object_name(&c, &t) {
        return table_name_looks_private_person(&t);
    }

    false
}

fn column_name_looks_person_name(column: &str) -> bool {
    [
        "name",
        "displayname",
        "display_name",
        "firstname",
        "first_name",
        "lastname",
        "last_name",
        "surname",
        "forename",
    ]
    .iter()
    .any(|needle| column.contains(needle))
}

fn column_name_looks_business_or_object_name(column: &str, table: &str) -> bool {
    [
        "company_name",
        "business_name",
        "organization_name",
        "organisation_name",
        "org_name",
        "tenant_name",
        "team_name",
        "product_name",
        "school_name",
    ]
    .iter()
    .any(|needle| column.contains(needle))
        || column_name_stem_matches_table(column, table)
}

fn column_name_stem_matches_table(column: &str, table: &str) -> bool {
    let stem = column
        .strip_suffix("_name")
        .or_else(|| column.strip_suffix("name"));
    let Some(stem) = stem else {
        return false;
    };
    let stem = stem.replace(['_', '-'], "");
    if stem.len() < 3 {
        return false;
    }
    let table = table.replace(['_', '-'], "");
    table == stem || table == format!("{stem}s") || stem == format!("{table}s")
}

fn column_name_looks_object_identifier(column: &str) -> bool {
    matches!(
        column.replace(['_', '-'], "").as_str(),
        "id" | "uuid" | "guid"
    )
}

fn table_name_looks_credential_or_connection(table: &str) -> bool {
    let compact = table.replace(['_', '-'], "");
    [
        "credential",
        "credentials",
        "connection",
        "connections",
        "session",
        "sessions",
        "apikey",
        "secret",
        "oauth",
    ]
    .iter()
    .any(|needle| compact == *needle || compact.ends_with(needle))
}

fn table_name_looks_private_person(table: &str) -> bool {
    let compact = table.replace(['_', '-'], "");
    [
        "user",
        "users",
        "member",
        "members",
        "client",
        "clients",
        "customer",
        "customers",
        "person",
        "people",
        "student",
        "students",
        "employee",
        "employees",
        "patient",
        "patients",
        "accountowner",
        "accountholder",
    ]
    .iter()
    .any(|needle| compact == *needle || compact.ends_with(needle))
}

fn sample_limit_for_column(table: &str, column: &str, data_type: &str) -> u32 {
    if is_likely_pii_column(table, column) {
        return 0;
    }
    let c = column.to_ascii_lowercase();
    let t = data_type.to_ascii_lowercase();
    let high_cardinality_grounding = ["city", "county", "state", "region", "street", "zip"]
        .iter()
        .any(|needle| c.contains(needle));
    if high_cardinality_grounding {
        return 200;
    }
    let object_name_grounding = [
        "name",
        "title",
        "label",
        "mcmname",
        "long_name",
        "short_name",
        "displayname",
        "display_name",
    ]
    .iter()
    .any(|needle| c.contains(needle));
    if object_name_grounding {
        return 5_000;
    }
    let code_like = [
        "code",
        "status",
        "type",
        "grade",
        "charter",
        "nces",
        "zip",
        "county",
        "city",
        "state",
        "funding",
        "frequency",
        "region",
        "year",
        "language",
        "alignment",
        "gender",
        "nationality",
        "block",
        "category",
    ]
    .iter()
    .any(|needle| c.contains(needle));
    if code_like {
        500
    } else if t.contains("text") || t.contains("char") {
        50
    } else {
        10
    }
}

/// Convert an arbitrary DB identifier (spaces, parens, slashes, etc.) to a
/// `[a-z_][a-z0-9_]{0,62}` canonical form. Used so BIRD-style columns like
/// "Free Meal Count (K-12)" become "free_meal_count_k_12" in the graph while
/// the original string is preserved as `db_column`/`db_table` for SQL emit.
/// Returns `None` when the result would be empty after stripping non-word chars.
fn to_canonical_snake(s: &str) -> Option<String> {
    let lowered: String = s
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
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
    if result.is_empty() {
        None
    } else {
        Some(result)
    }
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
                 created_at TEXT,
                 FOREIGN KEY (tenant_id) REFERENCES tenants(id)
             );
             INSERT INTO tenants VALUES (1, 'Acme');
             INSERT INTO users (id, tenant_id, status_code, created_at)
             VALUES (1, 1, 2, '2026-01-01');",
        )
        .unwrap();
        drop(conn);
        (dir, path)
    }

    #[test]
    fn pii_sampling_policy_keeps_non_person_object_names() {
        assert!(is_likely_pii_column("users", "DisplayName"));
        assert!(is_likely_pii_column("members", "last_name"));
        assert!(is_likely_pii_column("clients", "phone"));
        assert!(is_likely_pii_column("api_tokens", "secret_token"));
        assert!(is_likely_pii_column(
            "exchange_connections",
            "api_key_encrypted"
        ));
        assert!(is_likely_pii_column(
            "exchange_connections",
            "apiSecretEncrypted"
        ));
        assert!(is_likely_pii_column(
            "exchange_connections",
            "exchange_user_id"
        ));
        assert!(is_likely_pii_column("users", "id"));
        assert!(is_likely_pii_column("exchange_connections", "id"));

        assert!(!is_likely_pii_column("clients", "company_name"));
        assert!(!is_likely_pii_column("cards", "name"));
        assert!(!is_likely_pii_column("sets", "mcmName"));
        assert!(!is_likely_pii_column("Team", "team_long_name"));
        assert!(!is_likely_pii_column("publisher", "publisher_name"));
    }

    #[tokio::test]
    async fn samples_named_objects_without_sampling_private_user_names() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("names.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE users (
                 id INTEGER PRIMARY KEY,
                 DisplayName TEXT,
                 phone TEXT
             );
             CREATE TABLE cards (
                 id INTEGER PRIMARY KEY,
                 name TEXT,
                 setCode TEXT
             );
             CREATE TABLE sets (
                 code TEXT PRIMARY KEY,
                 mcmName TEXT
             );
             CREATE TABLE clients (
                 id INTEGER PRIMARY KEY,
                 company_name TEXT
             );
             CREATE TABLE exchange_connections (
                 id INTEGER PRIMARY KEY,
                 api_key_encrypted TEXT,
                 api_secret_encrypted TEXT,
                 exchange_user_id TEXT
             );
             INSERT INTO users VALUES (1, 'private-user', '555-0100');
             INSERT INTO cards VALUES (1, 'Ancestor''s Chosen', 'ARC');
             INSERT INTO sets VALUES ('ARC', 'Archenemy');
             INSERT INTO clients VALUES (1, 'Acme 4744');
             INSERT INTO exchange_connections
             VALUES (1, 'unsafe-api-key', 'unsafe-api-secret', 'unsafe-exchange-user');",
        )
        .unwrap();
        drop(conn);

        let out = dir.path().join("names.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let samples = semsql_graph::read::sample_values(&out).unwrap();
        let fields: std::collections::HashSet<_> = samples
            .iter()
            .map(|row| row.field_canonical.as_str())
            .collect();
        assert!(fields.contains("cards.name"));
        assert!(fields.contains("sets.mcmname"));
        assert!(fields.contains("clients.company_name"));
        assert!(!fields.contains("users.displayname"));
        assert!(!fields.contains("users.phone"));
        assert!(!fields.contains("users.id"));
        assert!(!fields.contains("exchange_connections.id"));
        assert!(!fields.contains("exchange_connections.api_key_encrypted"));
        assert!(!fields.contains("exchange_connections.api_secret_encrypted"));
        assert!(!fields.contains("exchange_connections.exchange_user_id"));
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
        assert_eq!(summary.relationship_count, 1);
        assert!(summary.sample_value_count >= 3);
        assert!(out.exists());
    }

    #[tokio::test]
    async fn db_only_extraction_can_disable_sample_values() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("schema_only.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only_with_options(
            &url,
            &out,
            DbOnlyExtractOptions {
                sample_values: false,
                ..DbOnlyExtractOptions::default()
            },
        )
        .await
        .unwrap();

        assert_eq!(summary.entity_count, 2);
        assert!(summary.field_count >= 6);
        assert_eq!(summary.relationship_count, 1);
        assert_eq!(summary.sample_value_count, 0);
        assert!(semsql_graph::read::sample_values(&out).unwrap().is_empty());
    }

    #[tokio::test]
    async fn db_only_extraction_ingests_column_description_csvs() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("opaque.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE district (
                 district_id INTEGER PRIMARY KEY,
                 A2 TEXT,
                 A3 TEXT
             );
             CREATE TABLE account (
                 account_id INTEGER PRIMARY KEY,
                 district_id INTEGER NOT NULL,
                 FOREIGN KEY (district_id) REFERENCES district(district_id)
             );
             CREATE TABLE loan (
                 loan_id INTEGER PRIMARY KEY,
                 account_id INTEGER NOT NULL,
                 FOREIGN KEY (account_id) REFERENCES account(account_id)
             );",
        )
        .unwrap();
        drop(conn);

        let description_dir = dir.path().join("database_description");
        std::fs::create_dir(&description_dir).unwrap();
        std::fs::write(
            description_dir.join("district.csv"),
            "original_column_name,column_name,column_description,data_format,value_description\n\
district_id,location of branch,location of branch,integer,\n\
A2,district_name,district_name,text,\n\
A3,region,region,text,\n\
A5,no. of municipalities with inhabitants < 499,municipality < district < region,text,\n",
        )
        .unwrap();

        assert_eq!(
            discover_schema_description_dir(dir.path()).as_deref(),
            Some(description_dir.as_path())
        );

        let out = dir.path().join("opaque.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only_with_options(
            &url,
            &out,
            DbOnlyExtractOptions {
                sample_values: false,
                schema_description_dir: Some(description_dir),
            },
        )
        .await
        .unwrap();
        assert_eq!(summary.field_description_count, 3);

        let fields = semsql_graph::read::fields(&out).unwrap();
        let display_labels: std::collections::HashMap<_, _> = fields
            .iter()
            .map(|field| (field.canonical(), field.display_label.clone()))
            .collect();
        assert_eq!(
            display_labels.get("district.a2").cloned().flatten(),
            Some("district name".to_string())
        );
        assert_eq!(
            display_labels.get("district.a3").cloned().flatten(),
            Some("region".to_string())
        );

        let vocab = semsql_graph::read::vocabulary(&out).unwrap();
        assert!(vocab.iter().any(|entry| {
            entry.term == "district name"
                && entry.canonical_kind == "field"
                && entry.canonical_value == "district.a2"
        }));
        assert!(vocab.iter().any(|entry| {
            entry.term == "region"
                && entry.canonical_kind == "field"
                && entry.canonical_value == "district.a3"
        }));
        assert!(!vocab.iter().any(|entry| {
            entry.term == "municipality < district < region"
                && entry.canonical_value == "district.a5"
        }));
    }

    #[tokio::test]
    async fn db_only_extraction_derives_scope_predicates_from_value_descriptions() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("coded.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE schools (
                 id INTEGER PRIMARY KEY,
                 Charter INTEGER,
                 Magnet INTEGER,
                 Virtual TEXT,
                 DOC TEXT,
                 StatusType TEXT,
                 \"Charter School (Y/N)\" INTEGER
             );",
        )
        .unwrap();
        drop(conn);

        let description_dir = dir.path().join("database_description");
        std::fs::create_dir(&description_dir).unwrap();
        std::fs::write(
            description_dir.join("schools.csv"),
            "original_column_name,column_name,column_description,data_format,value_description\n\
Charter,,This field identifies a charter school,integer,\"1 = The school is a charter; 0 = The school is not a charter\"\n\
Magnet,,This field identifies whether a school is a magnet school,integer,\"1 = Magnet - The school offers a magnet program; 0 = Not Magnet - The school is not a magnet school\"\n\
Virtual,,Virtual instruction type,text,\"F = Exclusively Virtual - all instruction is virtual; N = Not Virtual - no virtual instruction\"\n\
DOC,District Ownership Code,District Ownership Code,text,\"31 - State Special Schools; 52 - Elementary School District\"\n\
StatusType,,This field identifies the status of the district,text,\"Active: The district is in operation; Closed: The district is not in operation\"\n\
Charter School (Y/N),,Charter School (Y/N),integer,\"0: N; 1: Y\"\n",
        )
        .unwrap();

        let out = dir.path().join("coded.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only_with_options(
            &url,
            &out,
            DbOnlyExtractOptions {
                sample_values: false,
                schema_description_dir: Some(description_dir),
            },
        )
        .await
        .unwrap();
        assert!(summary.value_description_predicate_count >= 12);

        let vocab = semsql_graph::read::vocabulary(&out).unwrap();
        assert_scope_predicate(&vocab, "charter", "schools.charter", "1");
        assert_scope_predicate(&vocab, "not charter", "schools.charter", "0");
        assert_scope_predicate(&vocab, "magnet", "schools.magnet", "1");
        assert_scope_predicate(&vocab, "not magnet", "schools.magnet", "0");
        assert_scope_predicate(&vocab, "exclusively virtual", "schools.virtual", "F");
        assert_scope_predicate(&vocab, "state special schools", "schools.doc", "31");
        assert_scope_predicate(&vocab, "active", "schools.statustype", "Active");
        assert_scope_predicate(&vocab, "charter school", "schools.charter_school_y_n", "1");
    }

    #[tokio::test]
    async fn db_only_extraction_tolerates_lossy_description_csv_bytes() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("lossy.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE qualifying (
                 qualifyId INTEGER PRIMARY KEY,
                 q1 TEXT
             );",
        )
        .unwrap();
        drop(conn);

        let description_dir = dir.path().join("database_description");
        std::fs::create_dir(&description_dir).unwrap();
        let mut csv =
            b"original_column_name,column_name,column_description,data_format,value_description\n\
q1,qualifying 1,time in qualifying "
                .to_vec();
        csv.push(0xFF);
        csv.extend_from_slice(b" 1,text,\n");
        std::fs::write(description_dir.join("qualifying.csv"), csv).unwrap();

        let out = dir.path().join("lossy.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only_with_options(
            &url,
            &out,
            DbOnlyExtractOptions {
                sample_values: false,
                schema_description_dir: Some(description_dir),
            },
        )
        .await
        .unwrap();

        assert_eq!(summary.field_description_count, 1);
        let fields = semsql_graph::read::fields(&out).unwrap();
        let q1 = fields
            .iter()
            .find(|field| field.canonical() == "qualifying.q1")
            .expect("q1 field");
        assert_eq!(q1.display_label.as_deref(), Some("qualifying 1"));
    }

    fn assert_scope_predicate(
        vocab: &[semsql_graph::read::VocabularyEntry],
        term: &str,
        field: &str,
        raw_value: &str,
    ) {
        let found = vocab.iter().any(|entry| {
            if entry.term != term || entry.canonical_kind != "scope_predicate" {
                return false;
            }
            let Ok(value) = serde_json::from_str::<serde_json::Value>(&entry.canonical_value)
            else {
                return false;
            };
            value.get("field").and_then(serde_json::Value::as_str) == Some(field)
                && value.get("operator").and_then(serde_json::Value::as_str) == Some("=")
                && value.get("rawValue").and_then(serde_json::Value::as_str) == Some(raw_value)
        });
        if !found {
            let related: Vec<_> = vocab
                .iter()
                .filter_map(|entry| {
                    if entry.canonical_kind != "scope_predicate" {
                        return None;
                    }
                    let value =
                        serde_json::from_str::<serde_json::Value>(&entry.canonical_value).ok()?;
                    (value.get("field").and_then(serde_json::Value::as_str) == Some(field))
                        .then(|| (entry.term.clone(), value))
                })
                .collect();
            panic!("missing {term:?} -> {field} = {raw_value}; related={related:?}");
        }
    }

    #[tokio::test]
    async fn infers_name_matched_foreign_keys_when_sqlite_schema_omits_them() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("raw.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE customers (
                 CustomerID INTEGER PRIMARY KEY,
                 Segment TEXT
             );
             CREATE TABLE gasstations (
                 GasStationID INTEGER PRIMARY KEY,
                 Country TEXT
             );
             CREATE TABLE products (
                 ProductID INTEGER PRIMARY KEY,
                 Description TEXT
             );
             CREATE TABLE transactions_1k (
                 TransactionID INTEGER PRIMARY KEY,
                 CustomerID INTEGER,
                 GasStationID INTEGER,
                 ProductID INTEGER
             );",
        )
        .unwrap();
        drop(conn);

        let out = dir.path().join("raw.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only(&url, &out).await.unwrap();
        let rels = semsql_graph::read::relationships(&out).unwrap();
        let got: std::collections::HashSet<_> = rels
            .iter()
            .map(|rel| {
                (
                    rel.from_entity.as_str(),
                    rel.from_field.as_str(),
                    rel.to_entity.as_str(),
                    rel.to_field.as_str(),
                )
            })
            .collect();

        assert_eq!(summary.relationship_count, 3);
        assert!(got.contains(&("transactions_1k", "customerid", "customers", "customerid")));
        assert!(got.contains(&(
            "transactions_1k",
            "gasstationid",
            "gasstations",
            "gasstationid"
        )));
        assert!(got.contains(&("transactions_1k", "productid", "products", "productid")));
    }

    #[tokio::test]
    async fn infers_entity_code_relationships_when_sqlite_schema_omits_them() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("raw_code.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE sets (
                 code TEXT PRIMARY KEY,
                 name TEXT
             );
             CREATE TABLE cards (
                 id INTEGER PRIMARY KEY,
                 setCode TEXT,
                 name TEXT
             );",
        )
        .unwrap();
        drop(conn);

        let out = dir.path().join("raw_code.semsql");
        let url = format!("sqlite:{}", src.display());
        let summary = run_db_only(&url, &out).await.unwrap();
        let rels = semsql_graph::read::relationships(&out).unwrap();

        assert_eq!(summary.relationship_count, 1);
        assert!(rels.iter().any(|rel| {
            rel.from_entity == "cards"
                && rel.from_field == "setcode"
                && rel.to_entity == "sets"
                && rel.to_field == "code"
        }));
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

    #[tokio::test]
    async fn db_only_extraction_adds_humanized_entity_aliases() {
        let dir = TempDir::new().unwrap();
        let src = dir.path().join("aliases.sqlite");
        let conn = Connection::open(&src).unwrap();
        conn.execute_batch(
            "CREATE TABLE personal_access_tokens (id INTEGER PRIMARY KEY);
             CREATE TABLE password_reset_tokens (email TEXT, token TEXT);
             CREATE TABLE failed_jobs (id INTEGER PRIMARY KEY);",
        )
        .unwrap();
        drop(conn);

        let out = dir.path().join("aliases.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only_with_options(
            &url,
            &out,
            DbOnlyExtractOptions {
                sample_values: false,
                ..DbOnlyExtractOptions::default()
            },
        )
        .await
        .unwrap();

        let cascade = semsql_runtime::Cascade::load(&out, None).unwrap();
        assert_eq!(
            cascade
                .run("how many personal access tokens")
                .unwrap()
                .sql_text,
            "SELECT COUNT(*) FROM personal_access_tokens"
        );
        assert_eq!(
            cascade.run("how many failed jobs").unwrap().sql_text,
            "SELECT COUNT(*) FROM failed_jobs"
        );
    }

    #[test]
    fn rejects_non_sqlite_url() {
        let r = parse_sqlite_url("postgres://user:pass@host/db");
        assert!(r.is_err());
    }

    #[tokio::test]
    async fn jsonl_fragments_lift_pre_resolver_recall() {
        // Without the JSONL fragment, "how many students" must fail (no vocab
        // anchors `students` to `users`). After ingest the cascade
        // resolves the safe aggregate deterministically. Bare entity dumps
        // still fail closed elsewhere.
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let cascade = semsql_runtime::Cascade::load(&out, None).unwrap();
        let pre_ingest = cascade.run("how many students");
        assert!(
            pre_ingest.is_err(),
            "expected NeedsModel before vocab ingest"
        );

        let jsonl = dir.path().join("frags.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"students","canonical":{"kind":"entity","entity":"users"},"confidence":0.95,"locator":{"file":"lang/en/models.php","line":3,"layer":5,"extractor":"extractor-laravel:lang:en:php"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 1);
        assert_eq!(written.metric_definition_count, 0);

        let cascade2 = semsql_runtime::Cascade::load(&out, None).unwrap();
        let outcome = cascade2.run("how many students").unwrap();
        assert_eq!(outcome.sql_text, "SELECT COUNT(*) FROM users");
    }

    #[tokio::test]
    async fn jsonl_fragments_resolve_model_singulars_to_db_plural_graph_items() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("frags.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"students","canonical":{"kind":"entity","entity":"user"},"confidence":0.95,"locator":{"file":"app/models/user.rb","line":1,"layer":5,"extractor":"extractor-rails:locales"}}
{"term":"account status","canonical":{"kind":"field","field":"user.status_code"},"confidence":0.95,"locator":{"file":"app/admin.py","line":2,"layer":4,"extractor":"extractor-django:admin"}}
{"term":"active","canonical":{"kind":"enum_value","enumName":"user.status_code","rawValue":"2"},"confidence":0.90,"locator":{"file":"app/models/user.rb","line":3,"layer":3,"extractor":"extractor-rails:enum"}}
{"term":"blocked","canonical":{"kind":"scope_predicate","scope":"user.blocked","field":"user.status_code","operator":"=","rawValue":"2"},"confidence":0.90,"locator":{"file":"app/Models/User.php","line":4,"layer":2,"extractor":"extractor-laravel:eloquent"}}
{"term":"organization owner","canonical":{"kind":"relationship","from":"user","to":"tenant"},"confidence":0.70,"locator":{"file":"app/models.py","line":5,"layer":2,"extractor":"extractor-django:models"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 5);
        assert_eq!(written.metric_definition_count, 0);

        let vocab = semsql_graph::read::vocabulary(&out).unwrap();
        assert!(vocab.iter().any(|entry| {
            entry.term == "students"
                && entry.canonical_kind == "entity"
                && entry.canonical_value == "users"
        }));
        assert!(vocab.iter().any(|entry| {
            entry.term == "account status"
                && entry.canonical_kind == "field"
                && entry.canonical_value == "users.status_code"
        }));
        assert!(vocab.iter().any(|entry| {
            entry.term == "active"
                && entry.canonical_kind == "enum_value"
                && entry.canonical_value == "users.status_code:2"
        }));
        assert!(vocab.iter().any(|entry| {
            entry.term == "organization owner"
                && entry.canonical_kind == "relationship"
                && entry.canonical_value == "users->tenants"
        }));
        let scope_entry = vocab
            .iter()
            .find(|entry| entry.term == "blocked" && entry.canonical_kind == "scope_predicate")
            .expect("scope predicate vocab");
        let scope_json: serde_json::Value =
            serde_json::from_str(&scope_entry.canonical_value).unwrap();
        assert_eq!(scope_json["scope"], "users.blocked");
        assert_eq!(scope_json["field"], "users.status_code");

        let cascade = semsql_runtime::Cascade::load(&out, None).unwrap();
        let outcome = cascade.run("how many students").unwrap();
        assert_eq!(outcome.sql_text, "SELECT COUNT(*) FROM users");
    }

    #[tokio::test]
    async fn jsonl_grounded_relationships_become_join_edges() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("app.sqlite");
        let graph = dir.path().join("app.semsql");
        {
            let conn = rusqlite::Connection::open(&db).unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE crm_clients (
                    uid INTEGER PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE sales_orders (
                    id INTEGER PRIMARY KEY,
                    client_ref INTEGER NOT NULL,
                    order_number TEXT NOT NULL
                );
                "#,
            )
            .unwrap();
        }
        run_db_only(&format!("sqlite:{}", db.display()), &graph)
            .await
            .unwrap();
        assert!(
            semsql_graph::read::relationships(&graph)
                .unwrap()
                .is_empty(),
            "non-standard key names should not be guessed as a DB relationship"
        );

        let jsonl = dir.path().join("laravel.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"account","canonical":{"kind":"relationship","from":"sales_orders","to":"crm_clients","fromField":"sales_orders.client_ref","toField":"crm_clients.uid","relationshipKind":"many_to_one","relationName":"account"},"confidence":0.9,"locator":{"file":"app/Models/Order.php","line":8,"layer":2,"extractor":"extractor-laravel:eloquent:belongsTo"}}
{"term":"orders","canonical":{"kind":"relationship","from":"crm_clients","to":"sales_orders","fromField":"crm_clients.uid","toField":"sales_orders.client_ref","relationshipKind":"one_to_many","relationName":"orders"},"confidence":0.9,"locator":{"file":"app/Models/Customer.php","line":8,"layer":2,"extractor":"extractor-laravel:eloquent:hasMany"}}
"#,
        )
        .unwrap();

        let written = ingest_vocab_jsonl(&graph, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 2);
        assert_eq!(written.relationship_count, 1);
        let relationships = semsql_graph::read::relationships(&graph).unwrap();
        assert_eq!(relationships.len(), 1);
        assert_eq!(relationships[0].from_entity, "sales_orders");
        assert_eq!(relationships[0].from_field, "client_ref");
        assert_eq!(relationships[0].to_entity, "crm_clients");
        assert_eq!(relationships[0].to_field, "uid");
        assert_eq!(relationships[0].kind, "many_to_one");
    }

    #[tokio::test]
    async fn jsonl_relationships_fail_closed_when_join_fields_do_not_ground() {
        let (dir, src) = build_demo_db();
        let graph = dir.path().join("g.semsql");
        run_db_only(&format!("sqlite:{}", src.display()), &graph)
            .await
            .unwrap();

        let jsonl = dir.path().join("bad-relationship.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"tenant","canonical":{"kind":"relationship","from":"users","to":"tenants","fromField":"users.missing_tenant_id","toField":"tenants.id","relationshipKind":"many_to_one","relationName":"tenant"},"confidence":0.9,"locator":{"file":"app/Models/User.php","line":8,"layer":2,"extractor":"extractor-laravel:eloquent:belongsTo"}}
"#,
        )
        .unwrap();

        let written = ingest_vocab_jsonl(&graph, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 0);
        assert_eq!(written.relationship_count, 0);
    }

    #[tokio::test]
    async fn jsonl_metric_definitions_resolve_model_singulars_to_graph_items() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("metrics.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"record_kind":"metric_definition","name":"active_user_rate","displayLabel":"Active user rate","metricKind":"conditional_rate","subjectEntity":"user","numeratorField":"user.status_code","numeratorOperator":"=","numeratorValue":"2","numeratorValueKind":"value_dictionary","denominatorField":"user.id","scale":100,"requiredEntities":["user"],"aliases":["active user rate"],"locator":{"file":"semsql.metrics.json","line":1,"layer":3,"extractor":"semsql:metrics"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 0);
        assert_eq!(written.metric_definition_count, 1);

        let metrics = semsql_graph::read::metric_definitions(&out).unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0].name, "active_user_rate");
        assert_eq!(metrics[0].subject_entity, "users");
        assert_eq!(metrics[0].numerator_field, "users.status_code");
        assert_eq!(metrics[0].denominator_field, "users.id");
        assert_eq!(metrics[0].required_entities, vec!["users".to_string()]);
        assert_eq!(metrics[0].aliases, vec!["active user rate".to_string()]);
    }

    #[tokio::test]
    async fn jsonl_metric_definitions_tolerate_utf8_bom() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("metrics.jsonl");
        std::fs::write(
            &jsonl,
            "\u{feff}{\"record_kind\":\"metric_definition\",\"name\":\"active_user_rate\",\"displayLabel\":\"Active user rate\",\"metricKind\":\"conditional_rate\",\"subjectEntity\":\"user\",\"numeratorField\":\"user.status_code\",\"numeratorOperator\":\"=\",\"numeratorValue\":\"2\",\"numeratorValueKind\":\"value_dictionary\",\"denominatorField\":\"user.id\",\"scale\":100,\"requiredEntities\":[\"user\"],\"aliases\":[\"active user rate\"],\"locator\":{\"file\":\"semsql.metrics.json\",\"line\":1,\"layer\":3,\"extractor\":\"semsql:metrics\"}}\n",
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 0);
        assert_eq!(written.metric_definition_count, 1);
    }

    #[tokio::test]
    async fn jsonl_aggregate_metric_definitions_resolve_measure_fields() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("metrics.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"record_kind":"metric_definition","name":"average_user_id","displayLabel":"Average user id","metricKind":"aggregate","subjectEntity":"user","measureField":"user.id","aggregate":"avg","scale":1,"requiredEntities":["user"],"aliases":["average user id"],"locator":{"file":"semsql.metrics.json","line":1,"layer":3,"extractor":"semsql:metrics"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 0);
        assert_eq!(written.metric_definition_count, 1);

        let metrics = semsql_graph::read::metric_definitions(&out).unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0].name, "average_user_id");
        assert_eq!(metrics[0].metric_kind, "aggregate");
        assert_eq!(metrics[0].subject_entity, "users");
        assert_eq!(metrics[0].measure_field.as_deref(), Some("users.id"));
        assert_eq!(metrics[0].aggregate.as_deref(), Some("AVG"));
        assert!(!metrics[0].distinct_measure);
        assert_eq!(metrics[0].required_entities, vec!["users".to_string()]);
    }

    #[tokio::test]
    async fn jsonl_distinct_count_metric_definitions_resolve_measure_fields() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("metrics.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"record_kind":"metric_definition","name":"unique_users","displayLabel":"Unique users","metricKind":"aggregate","subjectEntity":"user","measureField":"user.id","aggregate":"count","distinct":true,"scale":1,"requiredEntities":["user"],"aliases":["unique users"],"locator":{"file":"semsql.metrics.json","line":1,"layer":3,"extractor":"semsql:metrics"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 0);
        assert_eq!(written.metric_definition_count, 1);

        let metrics = semsql_graph::read::metric_definitions(&out).unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0].name, "unique_users");
        assert_eq!(metrics[0].metric_kind, "aggregate");
        assert_eq!(metrics[0].measure_field.as_deref(), Some("users.id"));
        assert_eq!(metrics[0].aggregate.as_deref(), Some("COUNT"));
        assert!(metrics[0].distinct_measure);
    }

    #[tokio::test]
    async fn jsonl_fragments_skip_unresolved_source_only_fields() {
        let (dir, src) = build_demo_db();
        let out = dir.path().join("g.semsql");
        let url = format!("sqlite:{}", src.display());
        run_db_only(&url, &out).await.unwrap();

        let jsonl = dir.path().join("frags.jsonl");
        std::fs::write(
            &jsonl,
            r#"{"term":"profile score","canonical":{"kind":"field","field":"user.profile_score"},"confidence":0.95,"locator":{"file":"app/admin.py","line":2,"layer":4,"extractor":"extractor-django:admin"}}
{"term":"students","canonical":{"kind":"entity","entity":"user"},"confidence":0.95,"locator":{"file":"app/admin.py","line":3,"layer":4,"extractor":"extractor-django:admin"}}
"#,
        )
        .unwrap();
        let written = ingest_vocab_jsonl(&out, &jsonl).unwrap();
        assert_eq!(written.vocab_count, 1);
        assert_eq!(written.metric_definition_count, 0);
        let vocab = semsql_graph::read::vocabulary(&out).unwrap();
        assert!(!vocab
            .iter()
            .any(|entry| entry.canonical_value == "users.profile_score"));
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
        assert_eq!(written.vocab_count, 1);
        assert_eq!(written.metric_definition_count, 0);
    }
}
