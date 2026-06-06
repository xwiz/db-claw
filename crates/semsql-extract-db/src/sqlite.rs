//! SQLite backend for DB-side introspection.
//!
//! Uses `rusqlite` directly — no async runtime needed since SQLite is
//! file-local. The `Introspect` trait is async-by-default to keep parity
//! with the Postgres / MySQL backends, but the SQLite implementation just
//! returns ready futures.

use crate::{ColumnIntro, DbKind, ForeignKeyIntro, Introspect};
use async_trait::async_trait;
use rusqlite::{params, Connection};
use semsql_core::{Result, SemsqlError};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// SQLite introspector. Opens a single read-only connection at construction
/// time, then guards it behind a mutex for the cheap serial calls.
pub struct SqliteIntrospect {
    /// Path the connection was opened against — surfaced in diagnostic
    /// messages so multi-DB callers can attribute errors.
    pub path: PathBuf,
    /// Connection cache to avoid the open / migrate overhead per call.
    /// `Mutex` rather than `RwLock` because `rusqlite::Connection` is not
    /// `Sync`; the introspector is fundamentally serial anyway.
    conn: Mutex<Connection>,
}

impl SqliteIntrospect {
    /// Connect to the SQLite file at `path`. Opens read-only via the
    /// `file:?mode=ro` URI so introspection cannot mutate the source DB.
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        let uri = format!("file:{}?mode=ro", path.display());
        let conn = Connection::open_with_flags(
            &uri,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )
        .map_err(|e| SemsqlError::Other(format!("sqlite open `{}`: {e}", path.display())))?;
        Ok(Self {
            path,
            conn: Mutex::new(conn),
        })
    }

    fn with_conn<F, T>(&self, f: F) -> Result<T>
    where
        F: FnOnce(&Connection) -> Result<T>,
    {
        let conn = self
            .conn
            .lock()
            .map_err(|_| SemsqlError::Other("sqlite mutex poisoned".into()))?;
        f(&conn)
    }
}

#[async_trait]
impl Introspect for SqliteIntrospect {
    fn kind(&self) -> DbKind {
        DbKind::Sqlite
    }

    async fn list_tables(&self) -> Result<Vec<String>> {
        self.with_conn(|conn| {
            let mut stmt = conn
                .prepare(
                    "SELECT name FROM sqlite_master \
                     WHERE type = 'table' AND name NOT LIKE 'sqlite_%' \
                     ORDER BY name",
                )
                .map_err(|e| SemsqlError::Other(format!("list_tables: {e}")))?;
            let rows = stmt
                .query_map([], |row| row.get::<_, String>(0))
                .map_err(|e| SemsqlError::Other(format!("list_tables: {e}")))?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
            }
            Ok(out)
        })
    }

    async fn list_columns(&self) -> Result<Vec<ColumnIntro>> {
        self.with_conn(|conn| {
            let tables = list_tables_blocking(conn)?;
            let mut out = Vec::new();
            for table in tables {
                let mut stmt = conn
                    .prepare("SELECT name, type, [notnull], dflt_value FROM pragma_table_info(?1)")
                    .map_err(|e| SemsqlError::Other(format!("list_columns prepare: {e}")))?;
                let rows = stmt
                    .query_map(params![table], |row| {
                        Ok(ColumnIntro {
                            table: table.clone(),
                            column: row.get::<_, String>(0)?,
                            data_type: normalize_sqlite_type(&row.get::<_, String>(1)?),
                            nullable: row.get::<_, i64>(2)? == 0,
                            default: row.get::<_, Option<String>>(3)?,
                        })
                    })
                    .map_err(|e| SemsqlError::Other(format!("list_columns query: {e}")))?;
                for r in rows {
                    out.push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
                }
            }
            Ok(out)
        })
    }

    async fn list_foreign_keys(&self) -> Result<Vec<ForeignKeyIntro>> {
        self.with_conn(|conn| {
            let tables = list_tables_blocking(conn)?;
            let mut out = Vec::new();
            for table in tables {
                let mut stmt = conn
                    .prepare("SELECT \"from\", \"table\", \"to\" FROM pragma_foreign_key_list(?1)")
                    .map_err(|e| SemsqlError::Other(format!("fk prepare: {e}")))?;
                let rows = stmt
                    .query_map(params![table], |row| {
                        Ok((
                            row.get::<_, String>(0)?,
                            row.get::<_, String>(1)?,
                            row.get::<_, Option<String>>(2)?,
                        ))
                    })
                    .map_err(|e| SemsqlError::Other(format!("fk query: {e}")))?;
                for r in rows {
                    let (from_column, to_table, to_column) =
                        r.map_err(|e| SemsqlError::Other(e.to_string()))?;
                    let to_column = match to_column {
                        Some(col) if !col.trim().is_empty() => col,
                        _ => primary_key_column_blocking(conn, &to_table)?
                            .unwrap_or_else(|| "rowid".to_string()),
                    };
                    out.push(ForeignKeyIntro {
                        from_table: table.clone(),
                        from_column,
                        to_table,
                        to_column,
                    });
                }
            }
            Ok(out)
        })
    }

    async fn sample_values(&self, table: &str, column: &str, limit: u32) -> Result<Vec<String>> {
        let table_ident = quote_sqlite_ident(table)?;
        let column_ident = quote_sqlite_ident(column)?;
        self.with_conn(|conn| {
            let q = format!(
                "SELECT DISTINCT {column_ident} FROM {table_ident} \
                 WHERE {column_ident} IS NOT NULL \
                 ORDER BY {column_ident} COLLATE NOCASE LIMIT ?1"
            );
            let mut stmt = conn
                .prepare(&q)
                .map_err(|e| SemsqlError::Other(format!("sample prepare: {e}")))?;
            let rows = stmt
                .query_map(params![limit], |row| {
                    let v = row.get_ref(0)?;
                    Ok(match v {
                        rusqlite::types::ValueRef::Null => "NULL".to_string(),
                        rusqlite::types::ValueRef::Integer(i) => i.to_string(),
                        rusqlite::types::ValueRef::Real(f) => f.to_string(),
                        rusqlite::types::ValueRef::Text(b) => {
                            String::from_utf8_lossy(b).into_owned()
                        }
                        rusqlite::types::ValueRef::Blob(_) => "<blob>".to_string(),
                    })
                })
                .map_err(|e| SemsqlError::Other(format!("sample query: {e}")))?;
            let mut out = Vec::new();
            for r in rows {
                out.push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
            }
            Ok(out)
        })
    }
}

fn list_tables_blocking(conn: &Connection) -> Result<Vec<String>> {
    let mut stmt = conn
        .prepare(
            "SELECT name FROM sqlite_master \
             WHERE type = 'table' AND name NOT LIKE 'sqlite_%' \
             ORDER BY name",
        )
        .map_err(|e| SemsqlError::Other(format!("list_tables: {e}")))?;
    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| SemsqlError::Other(format!("list_tables: {e}")))?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
    }
    Ok(out)
}

fn primary_key_column_blocking(conn: &Connection, table: &str) -> Result<Option<String>> {
    let mut stmt = conn
        .prepare("SELECT name FROM pragma_table_info(?1) WHERE pk > 0 ORDER BY pk LIMIT 1")
        .map_err(|e| SemsqlError::Other(format!("primary_key prepare: {e}")))?;
    let mut rows = stmt
        .query(params![table])
        .map_err(|e| SemsqlError::Other(format!("primary_key query: {e}")))?;
    match rows
        .next()
        .map_err(|e| SemsqlError::Other(format!("primary_key row: {e}")))?
    {
        Some(row) => row
            .get::<_, String>(0)
            .map(Some)
            .map_err(|e| SemsqlError::Other(format!("primary_key value: {e}"))),
        None => Ok(None),
    }
}

/// Map SQLite's loose type-affinity strings to the SemanticGraph type
/// vocabulary. SQLite is permissive (any string in CREATE TABLE works);
/// we collapse to the closest `FieldType`.
pub fn normalize_sqlite_type(decl: &str) -> String {
    let lc = decl.to_lowercase();
    if lc.contains("int") {
        "integer".into()
    } else if lc.contains("char") || lc.contains("clob") || lc.contains("text") {
        "text".into()
    } else if lc.contains("blob") {
        "blob".into()
    } else if lc.contains("real") || lc.contains("floa") || lc.contains("doub") {
        "float".into()
    } else if lc.contains("bool") {
        "boolean".into()
    } else if lc.contains("date") || lc.contains("time") {
        "timestamp".into()
    } else if lc.is_empty() {
        // Untyped column — sqlite affinity rules call this `BLOB`.
        "blob".into()
    } else {
        "text".into()
    }
}

fn quote_sqlite_ident(s: &str) -> Result<String> {
    if s.is_empty() || s.len() > 128 || s.chars().any(|ch| ch == '\0' || ch.is_control()) {
        return Err(SemsqlError::InvalidIdentifier(s.to_string()));
    }
    Ok(format!("\"{}\"", s.replace('"', "\"\"")))
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
                 tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                 name TEXT NOT NULL,
                 status_code INTEGER DEFAULT 1,
                 created_at TEXT,
                 \"Display Status\" TEXT
             );
             INSERT INTO tenants VALUES (1, 'Acme'), (2, 'Globex');
             INSERT INTO users VALUES (1, 1, 'Ann', 2, '2026-01-01', 'Directly funded'),
                                       (2, 1, 'Bob', 1, '2026-02-01', 'Locally funded'),
                                       (3, 2, 'Cara', 2, '2026-03-01', 'Directly funded');",
        )
        .unwrap();
        drop(conn);
        (dir, path)
    }

    #[tokio::test]
    async fn lists_user_tables() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let mut tables = intro.list_tables().await.unwrap();
        tables.sort();
        assert_eq!(tables, vec!["tenants", "users"]);
    }

    #[tokio::test]
    async fn lists_columns_with_types_and_nullability() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let cols = intro.list_columns().await.unwrap();
        let names: Vec<_> = cols.iter().map(|c| (&c.table, &c.column)).collect();
        assert!(names.contains(&(&"users".to_string(), &"tenant_id".to_string())));
        let id_col = cols
            .iter()
            .find(|c| c.table == "users" && c.column == "id")
            .unwrap();
        assert_eq!(id_col.data_type, "integer");
    }

    #[tokio::test]
    async fn lists_foreign_keys() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let fks = intro.list_foreign_keys().await.unwrap();
        assert_eq!(fks.len(), 1);
        assert_eq!(fks[0].from_table, "users");
        assert_eq!(fks[0].from_column, "tenant_id");
        assert_eq!(fks[0].to_table, "tenants");
    }

    #[tokio::test]
    async fn lists_foreign_keys_with_implicit_referenced_pk() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("implicit_fk.sqlite");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);
             CREATE TABLE child (
                 id INTEGER PRIMARY KEY,
                 parent_id INTEGER REFERENCES parent
             );",
        )
        .unwrap();
        drop(conn);

        let intro = SqliteIntrospect::open(&path).unwrap();
        let fks = intro.list_foreign_keys().await.unwrap();

        assert_eq!(fks.len(), 1);
        assert_eq!(fks[0].from_table, "child");
        assert_eq!(fks[0].from_column, "parent_id");
        assert_eq!(fks[0].to_table, "parent");
        assert_eq!(fks[0].to_column, "id");
    }

    #[tokio::test]
    async fn samples_distinct_values() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let mut s = intro
            .sample_values("users", "status_code", 10)
            .await
            .unwrap();
        s.sort();
        assert_eq!(s, vec!["1", "2"]);
    }

    #[tokio::test]
    async fn samples_quoted_display_name_columns() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let mut s = intro
            .sample_values("users", "Display Status", 10)
            .await
            .unwrap();
        s.sort();
        assert_eq!(s, vec!["Directly funded", "Locally funded"]);
    }

    #[tokio::test]
    async fn sample_rejects_invalid_identifiers() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let r = intro.sample_values("users", "bad\0name", 1).await;
        assert!(matches!(r, Err(SemsqlError::InvalidIdentifier(_))));
    }

    #[test]
    fn type_normaliser_maps_common_decls() {
        assert_eq!(normalize_sqlite_type("INTEGER"), "integer");
        assert_eq!(normalize_sqlite_type("VARCHAR(255)"), "text");
        assert_eq!(normalize_sqlite_type("REAL"), "float");
        assert_eq!(normalize_sqlite_type("DATETIME"), "timestamp");
        assert_eq!(normalize_sqlite_type(""), "blob");
    }
}
