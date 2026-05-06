//! Last-mile SQL dialect renderer.
//!
//! The cascade emits dialect-agnostic SQL text that the Python rewriter (via
//! sqlglot) inspects and rewrites. This crate handles the final emit per
//! target engine — identifier quoting, function name mapping, LIMIT/OFFSET
//! vs TOP, JSON ops.
//!
//! v0.1 ships Postgres only. v0.2 adds MySQL + SQLite. v1.0 covers MSSQL,
//! BigQuery, Snowflake, DuckDB.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use semsql_core::{Result, SemsqlError};

/// Target SQL dialects.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum Dialect {
    /// PostgreSQL — v0.1 default.
    Postgres,
    /// MySQL / MariaDB — v0.2.
    MySql,
    /// SQLite — v0.2.
    Sqlite,
    /// Microsoft SQL Server — v1.0.
    MsSql,
    /// Google BigQuery — v1.0.
    BigQuery,
    /// Snowflake — v1.0.
    Snowflake,
    /// DuckDB — used by the differential-render test.
    DuckDb,
}

impl Dialect {
    /// Whether this dialect is supported in the current build.
    pub fn is_supported(self) -> bool {
        matches!(self, Dialect::Postgres)
    }

    /// Identifier-quote a name for this dialect.
    ///
    /// **Security**: identifiers entering this function must already have
    /// passed `CanonicalName` validation. We assert that invariant here as a
    /// belt-and-suspenders check; if a non-validated string ever reaches this
    /// path it indicates a bug upstream.
    pub fn quote_ident(self, name: &str) -> Result<String> {
        if !is_safe_identifier(name) {
            return Err(SemsqlError::InvalidIdentifier(name.to_string()));
        }
        Ok(match self {
            Dialect::Postgres
            | Dialect::Sqlite
            | Dialect::DuckDb
            | Dialect::Snowflake
            | Dialect::BigQuery => format!("\"{name}\""),
            Dialect::MySql => format!("`{name}`"),
            Dialect::MsSql => format!("[{name}]"),
        })
    }
}

fn is_safe_identifier(s: &str) -> bool {
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
    fn quotes_per_dialect() {
        assert_eq!(Dialect::Postgres.quote_ident("users").unwrap(), r#""users""#);
        assert_eq!(Dialect::MySql.quote_ident("users").unwrap(), "`users`");
        assert_eq!(Dialect::MsSql.quote_ident("users").unwrap(), "[users]");
    }

    #[test]
    fn refuses_unsafe_identifiers() {
        for bad in ["", "users--", "users\"; DROP", "1abc"] {
            assert!(Dialect::Postgres.quote_ident(bad).is_err());
        }
    }
}
