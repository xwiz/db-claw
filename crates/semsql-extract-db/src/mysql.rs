//! MySQL / MariaDB backend for DB-side introspection.
//!
//! Uses `information_schema` through `sqlx`. The backend only introspects the
//! connected database (`DATABASE()`); production callers should point the URL at
//! the exact schema they want to extract.

use crate::{ColumnIntro, DbKind, ForeignKeyIntro, Introspect};
use async_trait::async_trait;
use futures::TryStreamExt;
use semsql_core::{Result, SemsqlError};
use sqlx::mysql::{MySqlConnectOptions, MySqlPoolOptions};
use sqlx::{MySqlPool, Row};
use std::str::FromStr;

/// MySQL / MariaDB introspector. Holds a small async connection pool.
#[derive(Clone)]
pub struct MySqlIntrospect {
    pool: MySqlPool,
}

impl MySqlIntrospect {
    /// Connect to a MySQL URL. `mariadb://` is accepted as a convenience and
    /// normalized to the MySQL URL scheme before passing to `sqlx`.
    pub async fn connect(url: &str) -> Result<Self> {
        let normalized = normalize_mysql_url(url);
        let opts = MySqlConnectOptions::from_str(&normalized)
            .map_err(|e| SemsqlError::Other(format!("mysql url `{url}`: {e}")))?;
        let pool = MySqlPoolOptions::new()
            .max_connections(4)
            .connect_with(opts)
            .await
            .map_err(|e| SemsqlError::Other(format!("mysql connect: {e}")))?;
        Ok(Self { pool })
    }

    /// Connect using an existing pool. Useful for embedded callers and tests.
    pub fn with_pool(pool: MySqlPool) -> Self {
        Self { pool }
    }

    /// Borrow the underlying pool.
    pub fn pool(&self) -> &MySqlPool {
        &self.pool
    }
}

#[async_trait]
impl Introspect for MySqlIntrospect {
    fn kind(&self) -> DbKind {
        DbKind::MySql
    }

    async fn list_tables(&self) -> Result<Vec<String>> {
        let mut stream = sqlx::query(
            "SELECT TABLE_NAME AS table_name \
             FROM information_schema.TABLES \
             WHERE TABLE_SCHEMA = DATABASE() \
               AND TABLE_TYPE = 'BASE TABLE' \
             ORDER BY TABLE_NAME",
        )
        .fetch(&self.pool);

        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("list_tables: {e}")))?
        {
            out.push(row.get::<String, _>("table_name"));
        }
        Ok(out)
    }

    async fn list_columns(&self) -> Result<Vec<ColumnIntro>> {
        let mut stream = sqlx::query(
            "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, \
                    IS_NULLABLE, COLUMN_DEFAULT \
             FROM information_schema.COLUMNS \
             WHERE TABLE_SCHEMA = DATABASE() \
             ORDER BY TABLE_NAME, ORDINAL_POSITION",
        )
        .fetch(&self.pool);

        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("list_columns: {e}")))?
        {
            let data_type: String = row.get("DATA_TYPE");
            let column_type: String = row.get("COLUMN_TYPE");
            let is_nullable: String = row.get("IS_NULLABLE");
            let default: Option<String> = row.try_get("COLUMN_DEFAULT").ok().flatten();
            out.push(ColumnIntro {
                table: row.get("TABLE_NAME"),
                column: row.get("COLUMN_NAME"),
                data_type: normalize_mysql_type(&data_type, &column_type),
                nullable: is_nullable.eq_ignore_ascii_case("YES"),
                default,
            });
        }
        Ok(out)
    }

    async fn list_foreign_keys(&self) -> Result<Vec<ForeignKeyIntro>> {
        let mut stream = sqlx::query(
            "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, \
                    REFERENCED_COLUMN_NAME \
             FROM information_schema.KEY_COLUMN_USAGE \
             WHERE TABLE_SCHEMA = DATABASE() \
               AND REFERENCED_TABLE_NAME IS NOT NULL \
               AND REFERENCED_COLUMN_NAME IS NOT NULL \
             ORDER BY TABLE_NAME, ORDINAL_POSITION",
        )
        .fetch(&self.pool);

        let mut out = Vec::new();
        while let Some(row) = stream
            .try_next()
            .await
            .map_err(|e| SemsqlError::Other(format!("list_foreign_keys: {e}")))?
        {
            out.push(ForeignKeyIntro {
                from_table: row.get("TABLE_NAME"),
                from_column: row.get("COLUMN_NAME"),
                to_table: row.get("REFERENCED_TABLE_NAME"),
                to_column: row.get("REFERENCED_COLUMN_NAME"),
            });
        }
        Ok(out)
    }

    async fn sample_values(&self, table: &str, column: &str, limit: u32) -> Result<Vec<String>> {
        let table_ident = quote_mysql_ident(table)?;
        let column_ident = quote_mysql_ident(column)?;
        let q = format!(
            "SELECT DISTINCT CAST({column_ident} AS CHAR) AS v \
             FROM {table_ident} \
             WHERE {column_ident} IS NOT NULL \
             ORDER BY v \
             LIMIT ?"
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

/// Normalize MySQL / MariaDB data types to the SemanticGraph vocabulary.
pub fn normalize_mysql_type(data_type: &str, column_type: &str) -> String {
    let dt = data_type.to_lowercase();
    let column = column_type.to_lowercase();
    match dt.as_str() {
        "tinyint" if column.starts_with("tinyint(1)") => "boolean".into(),
        "tinyint" | "smallint" | "mediumint" | "int" | "integer" | "bigint" => "integer".into(),
        "float" | "double" | "decimal" | "numeric" => "float".into(),
        "bool" | "boolean" | "bit" => "boolean".into(),
        "date" | "time" | "year" => "date".into(),
        "datetime" | "timestamp" => "timestamp".into(),
        "json" => "json".into(),
        "blob" | "binary" | "varbinary" | "tinyblob" | "mediumblob" | "longblob" => "blob".into(),
        _ => "text".into(),
    }
}

fn normalize_mysql_url(url: &str) -> String {
    url.strip_prefix("mariadb://")
        .map(|rest| format!("mysql://{rest}"))
        .unwrap_or_else(|| url.to_string())
}

fn quote_mysql_ident(s: &str) -> Result<String> {
    if s.is_empty() || s.len() > 128 || s.chars().any(|ch| ch == '\0' || ch.is_control()) {
        return Err(SemsqlError::InvalidIdentifier(s.to_string()));
    }
    Ok(format!("`{}`", s.replace('`', "``")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn type_normalizer_handles_common_mysql_types() {
        assert_eq!(normalize_mysql_type("int", "int(11)"), "integer");
        assert_eq!(normalize_mysql_type("bigint", "bigint(20)"), "integer");
        assert_eq!(normalize_mysql_type("tinyint", "tinyint(1)"), "boolean");
        assert_eq!(normalize_mysql_type("tinyint", "tinyint(4)"), "integer");
        assert_eq!(normalize_mysql_type("decimal", "decimal(10,2)"), "float");
        assert_eq!(normalize_mysql_type("datetime", "datetime"), "timestamp");
        assert_eq!(normalize_mysql_type("varchar", "varchar(255)"), "text");
        assert_eq!(normalize_mysql_type("json", "json"), "json");
    }

    #[test]
    fn quote_ident_uses_backticks_and_escapes() {
        assert_eq!(quote_mysql_ident("users").unwrap(), "`users`");
        assert_eq!(quote_mysql_ident("weird`name").unwrap(), "`weird``name`");
        assert!(matches!(
            quote_mysql_ident("bad\0name"),
            Err(SemsqlError::InvalidIdentifier(_))
        ));
    }

    /// Live-DB integration test, gated behind `SEMSQL_MYSQL_TEST_URL`.
    ///
    /// The URL should point at a throwaway database. The test creates and drops
    /// only its own tables inside that database.
    #[tokio::test]
    async fn live_introspection_round_trip() {
        let Ok(url) = std::env::var("SEMSQL_MYSQL_TEST_URL") else {
            return;
        };
        let intro = MySqlIntrospect::connect(&url).await.unwrap();
        sqlx::query("DROP TABLE IF EXISTS users")
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query("DROP TABLE IF EXISTS tenants")
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query("CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query(
            "CREATE TABLE users (\
             id INTEGER PRIMARY KEY, \
             tenant_id INTEGER NOT NULL, \
             email TEXT NOT NULL, \
             status_code SMALLINT DEFAULT 1, \
             FOREIGN KEY (tenant_id) REFERENCES tenants(id)\
             )",
        )
        .execute(intro.pool())
        .await
        .unwrap();
        sqlx::query("INSERT INTO tenants VALUES (1, 'Acme'), (2, 'Globex')")
            .execute(intro.pool())
            .await
            .unwrap();

        let tables = intro.list_tables().await.unwrap();
        assert!(tables.iter().any(|t| t == "users"));

        let cols = intro.list_columns().await.unwrap();
        assert!(cols
            .iter()
            .any(|c| c.column == "tenant_id" && c.data_type == "integer"));

        let fks = intro.list_foreign_keys().await.unwrap();
        assert!(fks
            .iter()
            .any(|fk| fk.from_column == "tenant_id" && fk.to_column == "id"));

        let samples = intro.sample_values("tenants", "name", 10).await.unwrap();
        assert!(samples.iter().any(|sample| sample == "Acme"));

        sqlx::query("DROP TABLE IF EXISTS users")
            .execute(intro.pool())
            .await
            .unwrap();
        sqlx::query("DROP TABLE IF EXISTS tenants")
            .execute(intro.pool())
            .await
            .unwrap();
    }
}
