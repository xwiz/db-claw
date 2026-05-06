//! DB-side schema introspection.
//!
//! Reads system catalogues, foreign keys, check constraints, and a
//! sampled set of values per column. Output is fed into the merge engine
//! as the lowest-priority source layer (`SOURCE_LAYER_DB_SCHEMA`).
//!
//! The extractor is engine-aware: each backend has its own dialect quirks
//! around system schemas and identifier casing.
//!
//! v0.2 ships the SQLite backend (real implementation, used by the
//! end-to-end test). Postgres / MySQL / MSSQL land alongside their
//! framework adapters.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod sqlite;

#[cfg(feature = "postgres")]
pub mod postgres;

use async_trait::async_trait;
use semsql_core::Result;
use serde::{Deserialize, Serialize};

pub use sqlite::SqliteIntrospect;

#[cfg(feature = "postgres")]
pub use postgres::{PgIntrospect, RlsRow};

/// One introspected column.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ColumnIntro {
    /// Owning table name (DB-side).
    pub table: String,
    /// DB-side column name.
    pub column: String,
    /// SQL type as the engine reports it.
    pub data_type: String,
    /// Whether the column is nullable.
    pub nullable: bool,
    /// `DEFAULT` expression as the engine reports it.
    pub default: Option<String>,
}

/// One introspected foreign key.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ForeignKeyIntro {
    /// Owning table.
    pub from_table: String,
    /// Owning column.
    pub from_column: String,
    /// Target table.
    pub to_table: String,
    /// Target column.
    pub to_column: String,
}

/// Engines we plan to support.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum DbKind {
    /// PostgreSQL.
    Postgres,
    /// MySQL / MariaDB.
    MySql,
    /// SQLite.
    Sqlite,
    /// Microsoft SQL Server.
    MsSql,
}

/// Trait every backend implements.
#[async_trait]
pub trait Introspect: Send + Sync {
    /// Engine identifier.
    fn kind(&self) -> DbKind;

    /// List every user table in the connected schema.
    async fn list_tables(&self) -> Result<Vec<String>>;

    /// List every column on every table.
    async fn list_columns(&self) -> Result<Vec<ColumnIntro>>;

    /// List every foreign-key relationship.
    async fn list_foreign_keys(&self) -> Result<Vec<ForeignKeyIntro>>;

    /// Sample up to `limit` distinct values from a column. Used to seed
    /// `sample_values` in the SemanticGraph for disambiguation.
    async fn sample_values(&self, table: &str, column: &str, limit: u32)
        -> Result<Vec<String>>;
}
