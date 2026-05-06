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
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
                | rusqlite::OpenFlags::SQLITE_OPEN_URI,
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
                    .prepare(
                        "SELECT \"from\", \"table\", \"to\" FROM pragma_foreign_key_list(?1)",
                    )
                    .map_err(|e| SemsqlError::Other(format!("fk prepare: {e}")))?;
                let rows = stmt
                    .query_map(params![table], |row| {
                        Ok(ForeignKeyIntro {
                            from_table: table.clone(),
                            from_column: row.get::<_, String>(0)?,
                            to_table: row.get::<_, String>(1)?,
                            to_column: row.get::<_, String>(2)?,
                        })
                    })
                    .map_err(|e| SemsqlError::Other(format!("fk query: {e}")))?;
                for r in rows {
                    out.push(r.map_err(|e| SemsqlError::Other(e.to_string()))?);
                }
            }
            Ok(out)
        })
    }

    async fn sample_values(
        &self,
        table: &str,
        column: &str,
        limit: u32,
    ) -> Result<Vec<String>> {
        if !is_safe_ident(table) || !is_safe_ident(column) {
            return Err(SemsqlError::InvalidIdentifier(format!(
                "{table}.{column}"
            )));
        }
        self.with_conn(|conn| {
            let q = format!(
                "SELECT DISTINCT \"{column}\" FROM \"{table}\" \
                 WHERE \"{column}\" IS NOT NULL LIMIT ?1"
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
                 created_at TEXT
             );
             INSERT INTO tenants VALUES (1, 'Acme'), (2, 'Globex');
             INSERT INTO users VALUES (1, 1, 'Ann', 2, '2026-01-01'),
                                       (2, 1, 'Bob', 1, '2026-02-01'),
                                       (3, 2, 'Cara', 2, '2026-03-01');",
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
        let id_col = cols.iter().find(|c| c.table == "users" && c.column == "id").unwrap();
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
    async fn samples_distinct_values() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let mut s = intro.sample_values("users", "status_code", 10).await.unwrap();
        s.sort();
        assert_eq!(s, vec!["1", "2"]);
    }

    #[tokio::test]
    async fn sample_rejects_unsafe_idents() {
        let (_dir, path) = build_demo_db();
        let intro = SqliteIntrospect::open(&path).unwrap();
        let r = intro.sample_values("users; DROP", "id", 1).await;
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
