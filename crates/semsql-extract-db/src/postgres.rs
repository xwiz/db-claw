//! Postgres backend for DB-side introspection.
//!
//! Uses `sqlx` against `pg_catalog` / `information_schema`. The
//! introspector is read-only by construction — every query is a `SELECT`
//! and the helper that runs `sample_values` validates identifiers via
//! [`is_safe_ident`] before quoting them into a dynamic SQL string. There
//! is no path through this module that emits DML or DDL.
//!
//! Multi-schema policy (v0.2): tables are listed across every schema in
//! the connection's `current_schemas(false)` (i.e. the active
//! `search_path`, excluding implicit ones). Cross-schema collisions —
//! `app.users` vs `audit.users` — are surfaced by `semsql doctor` via
//! the conflict log, *not* silently merged.
//!
//! Connection pooling: a single `PgPool` is held by the introspector with
//! `max_connections = 4` — introspection is bursty (one round-trip per
//! query) but rarely needs parallelism; four is enough to overlap
//! list_columns with sample_values without flooding a small DB.

use crate::{ColumnIntro, DbKind, ForeignKeyIntro, Introspect};
use async_trait::async_trait;
use futures::TryStreamExt;
use semsql_core::{Result, SemsqlError};
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::{PgPool, Row};
use std::str::FromStr;

/// Postgres introspector. Holds a `PgPool`; cheap to clone and share.
#[derive(Clone)]
pub struct PgIntrospect {
    pool: PgPool,
}

impl PgIntrospect {
    /// Connect to a Postgres URL (`postgres://` or `postgresql://`). The
    /// pool is sized for typical introspection workloads — a handful of
    /// short-lived `SELECT`s, no long transactions.
    pub async fn connect(url: &str) -> Result<Self> {
        let opts = PgConnectOptions::from_str(url)
            .map_err(|e| SemsqlError::Other(format!("postgres url `{url}`: {e}")))?
            // Keep default `application_name` so DBAs can tell what's
            // hitting their cluster from `pg_stat_activity`.
            .application_name("semsql-extract");
        let pool = PgPoolOptions::new()
            .max_connections(4)
            .connect_with(opts)
            .await
            .map_err(|e| SemsqlError::Other(format!("postgres connect: {e}")))?;
        Ok(Self { pool })
    }

    /// Connect using an existing pool. Useful when callers want to manage
    /// pool lifecycle themselves (e.g. embedding `semsql-extract-db` in a
    /// long-running daemon).
    pub fn with_pool(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Borrow the underlying pool. Exposed so the `doctor` RLS check
    /// can re-use the connection without re-authenticating.
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Snapshot of which tenanted tables have RLS enabled. Returned as
    /// `(schema, table, rls_enabled, has_policies)` tuples so the caller
    /// can render its own diagnostic. The query joins `pg_class.relrowsecurity`
    /// (RLS toggle) with the count of `pg_policies` rows so we can warn
    /// when RLS is enabled but no policies exist (a common mis-configuration
    /// that *defaults to deny-all*, breaking the app).
    pub async fn rls_status(&self) -> Result<Vec<RlsRow>> {
        let rows = sqlx::query(
            "SELECT n.nspname AS schema, c.relname AS table, \
                    c.relrowsecurity AS rls_enabled, \
                    (SELECT COUNT(*) FROM pg_policies p \
                     WHERE p.schemaname = n.nspname AND p.tablename = c.relname) AS policy_count \
             FROM pg_class c \
             JOIN pg_namespace n ON n.oid = c.relnamespace \
             WHERE c.relkind = 'r' \
               AND n.nspname NOT IN ('pg_catalog','information_schema') \
               AND n.nspname NOT LIKE 'pg_toast%' \
             ORDER BY n.nspname, c.relname",
        )
        .fetch_all(&self.pool)
        .await
        .map_err(|e| SemsqlError::Other(format!("rls_status: {e}")))?;

        Ok(rows
            .into_iter()
            .map(|r| RlsRow {
                schema: r.get::<String, _>("schema"),
                table: r.get::<String, _>("table"),
                rls_enabled: r.get::<bool, _>("rls_enabled"),
                policy_count: r.get::<i64, _>("policy_count") as u32,
            })
            .collect())
    }
}

/// One row from [`PgIntrospect::rls_status`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RlsRow {
    /// Schema name.
    pub schema: String,
    /// Table name.
    pub table: String,
    /// `pg_class.relrowsecurity` — whether RLS is *enabled* on the table.
    /// Note: enabling RLS without any matching policy denies *all*
    /// non-bypass access, so this flag alone isn't enough.
    pub rls_enabled: bool,
    /// Number of `pg_policies` rows attached to the table. RLS-on +
    /// zero policies is the dangerous mis-config.
    pub policy_count: u32,
}

#[async_trait]
impl Introspect for PgIntrospect {
    fn kind(&self) -> DbKind {
        DbKind::Postgres
    }

    async fn list_tables(&self) -> Result<Vec<String>> {
        // Active schemas only — `current_schemas(false)` returns the
        // user-set search path with implicit ones (`pg_catalog`) excluded.
        // We further filter to relkind = 'r' (ordinary tables); partitions
        // are visible via `'p'` and views via `'v'` but those are out of
        // scope for v0.2.
        let mut stream = sqlx::query(
            "SELECT n.nspname AS schema, c.relname AS table \
             FROM pg_class c \
             JOIN pg_namespace n ON n.oid = c.relnamespace \
             WHERE c.relkind = 'r' \
               AND n.nspname = ANY (current_schemas(false)) \
               AND n.nspname NOT IN ('pg_catalog','information_schema') \
             ORDER BY n.nspname, c.relname",
        )
        .fetch(&self.pool);

        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("list_tables: {e}")))?
        {
            let schema: String = row.get("schema");
            let table: String = row.get("table");
            // For default-schema tables (`public.users`) we emit just the
            // bare name to match the SemanticGraph convention. Non-default
            // schemas qualify (`audit.events`).
            out.push(if schema == "public" {
                table
            } else {
                format!("{schema}.{table}")
            });
        }
        Ok(out)
    }

    async fn list_columns(&self) -> Result<Vec<ColumnIntro>> {
        // information_schema.columns is the canonical source. We pull
        // table_schema + table_name so we can re-join the `public`
        // collapsing rule from list_tables.
        let mut stream = sqlx::query(
            "SELECT c.table_schema, c.table_name, c.column_name, \
                    c.data_type, c.udt_name, c.is_nullable, c.column_default \
             FROM information_schema.columns c \
             WHERE c.table_schema = ANY (current_schemas(false)) \
               AND c.table_schema NOT IN ('pg_catalog','information_schema') \
             ORDER BY c.table_schema, c.table_name, c.ordinal_position",
        )
        .fetch(&self.pool);

        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("list_columns: {e}")))?
        {
            let schema: String = row.get("table_schema");
            let tbl: String = row.get("table_name");
            let table_qualified = if schema == "public" {
                tbl
            } else {
                format!("{schema}.{tbl}")
            };
            let data_type: String = row.get("data_type");
            let udt: String = row.get("udt_name");
            let is_nullable: String = row.get("is_nullable");
            let default: Option<String> = row.get("column_default");
            out.push(ColumnIntro {
                table: table_qualified,
                column: row.get("column_name"),
                data_type: normalize_pg_type(&data_type, &udt),
                nullable: is_nullable == "YES",
                default,
            });
        }
        Ok(out)
    }

    async fn list_foreign_keys(&self) -> Result<Vec<ForeignKeyIntro>> {
        // Joining the three views is the canonical SQL-standard approach
        // and works on every Postgres version we care about (>= 10).
        let mut stream = sqlx::query(
            "SELECT \
                kcu.table_schema   AS from_schema, \
                kcu.table_name     AS from_table, \
                kcu.column_name    AS from_column, \
                ccu.table_schema   AS to_schema, \
                ccu.table_name     AS to_table, \
                ccu.column_name    AS to_column \
             FROM information_schema.table_constraints tc \
             JOIN information_schema.key_column_usage kcu \
               ON tc.constraint_name = kcu.constraint_name \
              AND tc.table_schema    = kcu.table_schema \
             JOIN information_schema.constraint_column_usage ccu \
               ON ccu.constraint_name = tc.constraint_name \
              AND ccu.table_schema    = tc.table_schema \
             WHERE tc.constraint_type = 'FOREIGN KEY' \
               AND tc.table_schema = ANY (current_schemas(false)) \
             ORDER BY kcu.table_schema, kcu.table_name, kcu.ordinal_position",
        )
        .fetch(&self.pool);

        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("list_foreign_keys: {e}")))?
        {
            let qualify = |sch: String, tbl: String| {
                if sch == "public" {
                    tbl
                } else {
                    format!("{sch}.{tbl}")
                }
            };
            out.push(ForeignKeyIntro {
                from_table: qualify(row.get("from_schema"), row.get("from_table")),
                from_column: row.get("from_column"),
                to_table: qualify(row.get("to_schema"), row.get("to_table")),
                to_column: row.get("to_column"),
            });
        }
        Ok(out)
    }

    async fn sample_values(
        &self,
        table: &str,
        column: &str,
        limit: u32,
    ) -> Result<Vec<String>> {
        // Identifier safety: any character outside the canonical regex
        // means the value didn't come from our introspection (which
        // produces canonical names) and we refuse to interpolate it. The
        // value side is bound as a parameter; only the identifiers go
        // through string formatting, behind this gate.
        let (schema, base_table) = match table.split_once('.') {
            Some((s, t)) => (s, t),
            None => ("public", table),
        };
        if !is_safe_ident(schema) || !is_safe_ident(base_table) || !is_safe_ident(column) {
            return Err(SemsqlError::InvalidIdentifier(format!(
                "{schema}.{base_table}.{column}"
            )));
        }
        let q = format!(
            "SELECT DISTINCT \"{column}\"::text AS v \
             FROM \"{schema}\".\"{base_table}\" \
             WHERE \"{column}\" IS NOT NULL \
             LIMIT $1"
        );
        let mut stream = sqlx::query(&q).bind(limit as i64).fetch(&self.pool);
        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("sample_values: {e}")))?
        {
            let v: Option<String> = row.try_get("v").ok();
            out.push(v.unwrap_or_else(|| "NULL".into()));
        }
        Ok(out)
    }
}

/// Map Postgres `information_schema.data_type` (with `udt_name` for
/// disambiguation) to the SemanticGraph type vocabulary. Coverage matches
/// the SQLite normaliser plus first-class Postgres types: `jsonb`, `uuid`,
/// arrays, enums (`USER-DEFINED`).
pub fn normalize_pg_type(data_type: &str, udt: &str) -> String {
    let dt = data_type.to_lowercase();
    let u = udt.to_lowercase();
    if dt == "user-defined" {
        // Could be an enum, a domain, or a composite. For graph
        // purposes, the canonical name is the udt_name.
        return u;
    }
    if dt == "array" {
        // sqlx returns the inner type via udt_name with a leading `_`,
        // e.g. `_int4`. Strip it for readability.
        let inner = u.strip_prefix('_').unwrap_or(&u);
        return format!("array<{inner}>");
    }
    match dt.as_str() {
        "smallint" | "integer" | "bigint" => "integer".into(),
        "boolean" => "boolean".into(),
        "real" | "double precision" | "numeric" => "float".into(),
        "uuid" => "uuid".into(),
        "json" | "jsonb" => "json".into(),
        "date" | "time" | "time with time zone" | "time without time zone" => "date".into(),
        "timestamp with time zone" | "timestamp without time zone" => "timestamp".into(),
        "bytea" => "blob".into(),
        // Default to text for char / varchar / text / etc.
        _ => "text".into(),
    }
}

fn is_safe_ident(s: &str) -> bool {
    if s.is_empty() || s.len() > 64 {
        return false;
    }
    let mut bytes = s.bytes();
    let first = bytes.next().unwrap();
    (first.is_ascii_alphabetic() || first == b'_')
        && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn type_normaliser_handles_common_pg_types() {
        assert_eq!(normalize_pg_type("integer", "int4"), "integer");
        assert_eq!(normalize_pg_type("bigint", "int8"), "integer");
        assert_eq!(normalize_pg_type("boolean", "bool"), "boolean");
        assert_eq!(normalize_pg_type("text", "text"), "text");
        assert_eq!(
            normalize_pg_type("character varying", "varchar"),
            "text"
        );
        assert_eq!(normalize_pg_type("uuid", "uuid"), "uuid");
        assert_eq!(normalize_pg_type("jsonb", "jsonb"), "json");
        assert_eq!(
            normalize_pg_type("timestamp without time zone", "timestamp"),
            "timestamp"
        );
        assert_eq!(normalize_pg_type("USER-DEFINED", "order_status"), "order_status");
        assert_eq!(normalize_pg_type("ARRAY", "_int4"), "array<int4>");
    }

    #[test]
    fn safe_ident_allows_canonical_names_only() {
        assert!(is_safe_ident("users"));
        assert!(is_safe_ident("order_items"));
        assert!(is_safe_ident("_internal"));
        assert!(!is_safe_ident(""));
        assert!(!is_safe_ident("9start"));
        assert!(!is_safe_ident("with space"));
        assert!(!is_safe_ident("drop;table"));
        assert!(!is_safe_ident("a".repeat(65).as_str()));
    }

    /// Live-DB integration tests — gated behind `SEMSQL_PG_TEST_URL` so
    /// the suite stays green in offline CI. Set the env var to a Postgres
    /// URL pointing at a *throwaway* DB; the test creates and drops its
    /// own schema and is safe to re-run.
    #[tokio::test]
    async fn live_introspection_round_trip() {
        let Ok(url) = std::env::var("SEMSQL_PG_TEST_URL") else {
            return; // skip when no live DB is configured
        };
        let intro = PgIntrospect::connect(&url).await.unwrap();
        // Use a dedicated schema to keep the test idempotent without
        // touching any user data.
        let schema = "semsql_pg_test";
        sqlx::query(&format!("DROP SCHEMA IF EXISTS {schema} CASCADE"))
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query(&format!("CREATE SCHEMA {schema}"))
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query(&format!("SET search_path TO {schema}, public"))
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query(&format!(
            "CREATE TABLE {schema}.tenants (id SERIAL PRIMARY KEY, name TEXT NOT NULL); \
             CREATE TABLE {schema}.users ( \
                 id SERIAL PRIMARY KEY, \
                 tenant_id INTEGER NOT NULL REFERENCES {schema}.tenants(id), \
                 email TEXT NOT NULL, \
                 status_code SMALLINT DEFAULT 1 \
             );"
        ))
        .execute(intro.pool())
        .await
        .unwrap();

        let tables = intro.list_tables().await.unwrap();
        assert!(tables.iter().any(|t| t.ends_with("users")));

        let cols = intro.list_columns().await.unwrap();
        assert!(
            cols.iter()
                .any(|c| c.column == "tenant_id" && c.data_type == "integer")
        );

        let fks = intro.list_foreign_keys().await.unwrap();
        assert!(
            fks.iter()
                .any(|fk| fk.from_column == "tenant_id" && fk.to_column == "id")
        );

        sqlx::query(&format!("DROP SCHEMA {schema} CASCADE"))
            .execute(intro.pool())
            .await
            .unwrap();
    }
}
