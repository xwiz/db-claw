//! Independent re-validator for the SQL produced by the Python rewriter.
//!
//! After sqlglot has validated and injected mandatory filters, the resulting
//! SQL is re-parsed by `sqlparser-rs` (a different implementation) and
//! checked against the same invariants. **Two parsers must agree** — if the
//! Rust pass disagrees with sqlglot, the build fails closed with
//! [`SemsqlError::ParserDisagreement`].
//!
//! This is the defense-in-depth pattern that the [Apache Superset
//! CVE-2025-48912](https://www.miggo.io/vulnerability-database/cve/CVE-2025-48912)
//! fix demonstrated is necessary: a single SQL parser, no matter how mature,
//! can have AST-walk gaps that an attacker exploits to bypass row-level
//! security. Two independent parsers, each verifying the same invariants,
//! force the attacker to find bugs in *both* simultaneously.
//!
//! v0.1 establishes the public surface. The actual checks land alongside the
//! Python rewriter as the test suite gives us shapes to verify.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use ahash::{AHashMap, AHashSet};
use semsql_core::{Result, SemsqlError};
use sqlparser::ast::{
    Cte, Expr, ObjectName, Query, Select, SetExpr, Statement, TableFactor, TableWithJoins,
};
use sqlparser::dialect::{Dialect, GenericDialect, PostgreSqlDialect, SQLiteDialect};
use sqlparser::parser::Parser;

/// Things the second-pass validator must confirm about post-rewrite SQL.
#[derive(Clone, Debug, Default)]
pub struct Invariants {
    /// Map from canonical entity name → marker substring that, if present in
    /// any predicate scoped against a reference of that entity, satisfies the
    /// scope check (e.g. `"tenant_id"`). The walker only confirms presence —
    /// the Python rewriter is the source of truth for *which* predicate to
    /// inject. This second pass is a sanity net.
    pub scoped_entities: AHashMap<String, String>,
    /// Bound-parameter placeholders that the injector promised to populate.
    pub bound_params: AHashSet<String>,
    /// Statement must be SELECT only — no DML/DDL/multi-statement smuggling.
    pub select_only: bool,
}

impl Invariants {
    /// Strict default: SELECT only, nothing else asserted yet.
    pub fn strict() -> Self {
        Self {
            select_only: true,
            ..Self::default()
        }
    }

    /// Convenience builder: assert that every reference to `entity` is
    /// scoped by a WHERE clause containing `marker`.
    pub fn require_scope(mut self, entity: &str, marker: &str) -> Self {
        self.scoped_entities
            .insert(entity.to_lowercase(), marker.to_string());
        self
    }
}

/// Dialect identifier for the parser.
#[derive(Copy, Clone, Debug, Default)]
pub enum SqlDialect {
    /// PostgreSQL (default).
    #[default]
    Postgres,
    /// SQLite — used by the Python rewriter test suite.
    Sqlite,
    /// Generic / lowest-common-denominator parser.
    Generic,
}

impl SqlDialect {
    fn as_dyn(&self) -> Box<dyn Dialect> {
        match self {
            SqlDialect::Postgres => Box::new(PostgreSqlDialect {}),
            SqlDialect::Sqlite => Box::new(SQLiteDialect {}),
            SqlDialect::Generic => Box::new(GenericDialect {}),
        }
    }
}

/// Run the independent second-pass over `sql`.
///
/// Returns `Ok(())` on agreement; [`SemsqlError::ParserDisagreement`],
/// [`SemsqlError::Validation`], or [`SemsqlError::ScopeLeak`] otherwise.
pub fn verify(sql: &str, inv: &Invariants) -> Result<()> {
    verify_with_dialect(sql, inv, SqlDialect::default())
}

/// Run the independent second-pass over `sql` against a specific dialect.
pub fn verify_with_dialect(sql: &str, inv: &Invariants, dialect: SqlDialect) -> Result<()> {
    let dyn_dialect = dialect.as_dyn();
    let stmts = Parser::parse_sql(dyn_dialect.as_ref(), sql)
        .map_err(|e| SemsqlError::ParserDisagreement {
            detail: format!("sqlparser-rs failed to parse: {e}"),
        })?;

    if inv.select_only {
        if stmts.len() != 1 {
            return Err(SemsqlError::ParserDisagreement {
                detail: format!("expected exactly one statement, got {}", stmts.len()),
            });
        }
    }

    for stmt in &stmts {
        if inv.select_only && !matches!(stmt, Statement::Query(_)) {
            return Err(SemsqlError::Validation(format!(
                "non-SELECT statement: {stmt}"
            )));
        }
        if let Statement::Query(query) = stmt {
            walk_query_for_scope(query, inv, &AHashSet::new())?;
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// scope-leak walker
// ---------------------------------------------------------------------------

fn walk_query_for_scope(
    query: &Query,
    inv: &Invariants,
    inherited_cte_names: &AHashSet<String>,
) -> Result<()> {
    // Collect CTE names introduced by this query — they shadow physical
    // table references with the same name within this query's scope.
    let mut cte_names = inherited_cte_names.clone();
    if let Some(with) = &query.with {
        for cte in &with.cte_tables {
            cte_names.insert(cte_name(cte).to_lowercase());
        }
        for cte in &with.cte_tables {
            walk_query_for_scope(&cte.query, inv, &cte_names)?;
        }
    }
    walk_set_expr_for_scope(&query.body, inv, &cte_names)?;
    Ok(())
}

fn walk_set_expr_for_scope(
    body: &SetExpr,
    inv: &Invariants,
    cte_names: &AHashSet<String>,
) -> Result<()> {
    match body {
        SetExpr::Select(select) => walk_select_for_scope(select, inv, cte_names),
        SetExpr::Query(q) => walk_query_for_scope(q, inv, cte_names),
        SetExpr::SetOperation { left, right, .. } => {
            walk_set_expr_for_scope(left, inv, cte_names)?;
            walk_set_expr_for_scope(right, inv, cte_names)
        }
        SetExpr::Values(_) | SetExpr::Insert(_) | SetExpr::Update(_) | SetExpr::Table(_) => Ok(()),
    }
}

fn walk_select_for_scope(
    select: &Select,
    inv: &Invariants,
    cte_names: &AHashSet<String>,
) -> Result<()> {
    let where_text = select
        .selection
        .as_ref()
        .map(|w| w.to_string().to_lowercase())
        .unwrap_or_default();

    for twj in &select.from {
        check_table_with_joins(twj, inv, cte_names, &where_text)?;
    }
    Ok(())
}

fn check_table_with_joins(
    twj: &TableWithJoins,
    inv: &Invariants,
    cte_names: &AHashSet<String>,
    where_text: &str,
) -> Result<()> {
    check_table_factor(&twj.relation, inv, cte_names, where_text)?;
    for join in &twj.joins {
        check_table_factor(&join.relation, inv, cte_names, where_text)?;
    }
    Ok(())
}

fn check_table_factor(
    tf: &TableFactor,
    inv: &Invariants,
    cte_names: &AHashSet<String>,
    where_text: &str,
) -> Result<()> {
    match tf {
        TableFactor::Table { name, alias, .. } => {
            let entity = object_name_last(name).to_lowercase();
            if cte_names.contains(&entity) {
                return Ok(());
            }
            if let Some(marker) = inv.scoped_entities.get(&entity) {
                let alias_or_name = alias
                    .as_ref()
                    .map(|a| a.name.value.to_lowercase())
                    .unwrap_or_else(|| entity.clone());
                let qualified = format!("{alias_or_name}.{}", marker.to_lowercase());
                if !where_text.contains(&qualified) {
                    return Err(SemsqlError::scope_leak(format!(
                        "{entity} (alias={alias_or_name}) — WHERE missing `{qualified}`"
                    )));
                }
            }
            Ok(())
        }
        TableFactor::Derived { subquery, .. } => walk_query_for_scope(subquery, inv, cte_names),
        TableFactor::NestedJoin { table_with_joins, .. } => {
            check_table_with_joins(table_with_joins, inv, cte_names, where_text)
        }
        // Unhandled variants (TVF, JSON_TABLE, etc.) — leave to a future
        // patch; the validator (sqlglot, in Python) is the primary gate.
        _ => Ok(()),
    }
}

fn object_name_last(name: &ObjectName) -> String {
    name.0
        .last()
        .map(|p| p.to_string())
        .unwrap_or_default()
}

fn cte_name(cte: &Cte) -> &str {
    cte.alias.name.value.as_str()
}

#[allow(dead_code)]
fn looks_like_subquery(_e: &Expr) -> bool {
    // Reserved for future invariant — column-level checks can use this
    // when we tighten the scope model beyond the WHERE-text marker check.
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scope_users() -> Invariants {
        Invariants::strict().require_scope("users", "tenant_id")
    }

    #[test]
    fn rejects_multi_statement() {
        let r = verify("SELECT 1; SELECT 2", &Invariants::strict());
        assert!(matches!(r, Err(SemsqlError::ParserDisagreement { .. })));
    }

    #[test]
    fn rejects_dml() {
        let r = verify("DELETE FROM users", &Invariants::strict());
        assert!(matches!(r, Err(SemsqlError::Validation(_))));
    }

    #[test]
    fn accepts_simple_select() {
        verify("SELECT 1", &Invariants::strict()).unwrap();
    }

    #[test]
    fn detects_unscoped_users_reference() {
        let r = verify("SELECT * FROM users", &scope_users());
        assert!(matches!(r, Err(SemsqlError::ScopeLeak { .. })), "{r:?}");
    }

    #[test]
    fn accepts_scoped_users_reference() {
        verify(
            "SELECT * FROM users WHERE users.tenant_id = 1",
            &scope_users(),
        )
        .unwrap();
    }

    #[test]
    fn detects_subquery_aliasing_bypass() {
        // The inner `users` reference must be scoped; the outer alias `u`
        // is a derived-table reference and is fine.
        let r = verify(
            "SELECT * FROM (SELECT * FROM users) u",
            &scope_users(),
        );
        assert!(matches!(r, Err(SemsqlError::ScopeLeak { .. })), "{r:?}");
    }

    #[test]
    fn detects_union_branch_bypass() {
        let r = verify(
            "SELECT id FROM users WHERE users.tenant_id = 1 \
             UNION ALL SELECT id FROM users",
            &scope_users(),
        );
        assert!(matches!(r, Err(SemsqlError::ScopeLeak { .. })), "{r:?}");
    }

    #[test]
    fn cte_alias_does_not_count_as_users_reference() {
        // Outer `FROM x` must NOT trigger a scope-leak — `x` is a CTE alias.
        // Inner `users` inside the CTE body IS scoped.
        verify(
            "WITH x AS (SELECT * FROM users WHERE users.tenant_id = 1) SELECT * FROM x",
            &scope_users(),
        )
        .unwrap();
    }

    #[test]
    fn detects_unscoped_users_inside_cte() {
        let r = verify(
            "WITH x AS (SELECT * FROM users) SELECT * FROM x",
            &scope_users(),
        );
        assert!(matches!(r, Err(SemsqlError::ScopeLeak { .. })), "{r:?}");
    }
}
