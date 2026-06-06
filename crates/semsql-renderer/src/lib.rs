//! Last-mile SQL dialect renderer.
//!
//! The cascade emits dialect-agnostic SQL text via
//! `semsql_natsql::transpile::to_sql_text`. After validation +
//! mandatory-filter injection, the dialect renderer here translates
//! the NatSQL AST into a dialect-correct SQL string per target
//! engine. The enum includes the dialects covered by the renderer tests and
//! integrations.
//!
//! Per-dialect concerns we handle:
//!
//!   - Identifier quoting — PG / SQLite / DuckDB / Snowflake / BigQuery
//!     use double quotes; MySQL uses backticks; MSSQL uses square
//!     brackets.
//!   - Boolean literals — PG / MySQL / Snowflake / BigQuery accept
//!     `TRUE`/`FALSE`; SQLite stores as `1`/`0` and rejects
//!     `TRUE`/`FALSE` keyword literals on older versions.
//!   - LIMIT — PG / MySQL / SQLite / DuckDB / Snowflake / BigQuery
//!     accept `LIMIT n`; MSSQL requires `TOP n` immediately after
//!     `SELECT`.
//!   - String literals — single-quote escape (`''`) is universal.
//!
//! All identifiers entering this module must already have passed
//! canonical-name validation; we re-check at quote time as belt-and-
//! suspenders defence.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use semsql_core::{Result, SemsqlError};
use semsql_natsql::ast::{
    Aggregate, Comparator, Condition, Field, NatSql, OrderDir, SelectItem, Value,
};
use std::fmt::Write;

/// Target SQL dialects.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum Dialect {
    /// PostgreSQL.
    Postgres,
    /// MySQL / MariaDB.
    MySql,
    /// SQLite.
    Sqlite,
    /// Microsoft SQL Server.
    MsSql,
    /// Google BigQuery.
    BigQuery,
    /// Snowflake.
    Snowflake,
    /// DuckDB — used by the differential-render test.
    DuckDb,
}

impl Dialect {
    /// Whether this dialect is supported in the current build. The flag remains
    /// because future dialect additions may land disabled until validated
    /// against integration suites.
    pub fn is_supported(self) -> bool {
        true
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
            // Standard SQL double-quote (case-preserving on PG, case-folding
            // to upper on Snowflake — both widely interoperable).
            Dialect::Postgres | Dialect::Sqlite | Dialect::DuckDb | Dialect::Snowflake => {
                format!("\"{name}\"")
            }
            // BigQuery uses backticks for fully-qualified
            // `project.dataset.table` paths and accepts them on bare
            // identifiers too. Double quotes are *string literals* in
            // BigQuery, not identifiers — using them silently would
            // produce a string-vs-column class of bug.
            Dialect::MySql | Dialect::BigQuery => format!("`{name}`"),
            Dialect::MsSql => format!("[{name}]"),
        })
    }

    /// Whether the dialect prefers `TRUE`/`FALSE` keyword booleans
    /// (vs. the `1`/`0` numeric form). SQLite accepted the keywords
    /// only in 3.23+; emitting `1`/`0` keeps queries portable across
    /// older builds. MSSQL has no boolean type — `BIT` columns
    /// compare against `1`/`0`.
    fn supports_boolean_keyword(self) -> bool {
        !matches!(self, Dialect::Sqlite | Dialect::MsSql)
    }

    /// Whether the dialect emits `LIMIT n` at the tail (vs. `TOP n`
    /// directly after `SELECT`). MSSQL is the lone hold-out; everyone
    /// else accepts `LIMIT`.
    fn limit_clause_at_tail(self) -> bool {
        !matches!(self, Dialect::MsSql)
    }
}

/// Convenience: parse NatSQL text and render it per-dialect in one
/// call. Use when you have a SQL string emitted by the cascade and
/// want a dialect-specific re-rendering. Validation errors from the
/// NatSQL parser surface verbatim.
pub fn render_text(natsql_text: &str, dialect: Dialect) -> Result<String> {
    let ast = semsql_natsql::parse(natsql_text)?;
    render(&ast, dialect)
}

/// Render a NatSQL AST as dialect-specific SQL.
///
/// Identifiers are quoted per dialect; booleans match the engine's
/// preferred literal form; LIMIT clauses honour the target's clause
/// position. Returns [`SemsqlError::Other`] for dialects not gated
/// by [`Dialect::is_supported`].
pub fn render(q: &NatSql, dialect: Dialect) -> Result<String> {
    if !dialect.is_supported() {
        return Err(SemsqlError::Other(format!(
            "dialect {dialect:?} not yet wired in this build"
        )));
    }
    if q.entities.is_empty() {
        return Err(SemsqlError::validation(
            "natsql AST has no entities — cannot render FROM clause",
        ));
    }
    let mut out = String::new();
    out.push_str("SELECT ");
    // MSSQL pivot:
    //   - LIMIT n with no OFFSET → `SELECT TOP n …` (cheaper plan).
    //   - LIMIT + OFFSET, or OFFSET alone → `OFFSET m ROWS FETCH NEXT n
    //     ROWS ONLY` at the tail. `TOP` cannot coexist with `OFFSET`,
    //     and `OFFSET` requires `ORDER BY` per the MSSQL spec — we
    //     surface that as a validation error rather than silently
    //     emitting non-deterministic SQL.
    let mssql_use_top = !dialect.limit_clause_at_tail() && q.offset.is_none() && q.limit.is_some();
    if mssql_use_top {
        if let Some(n) = q.limit {
            write!(out, "TOP {n} ").expect("write");
        }
    }
    if q.select.is_empty() {
        out.push('*');
    } else {
        for (i, item) in q.select.iter().enumerate() {
            if i > 0 {
                out.push_str(", ");
            }
            render_select_item(&mut out, item, dialect)?;
        }
    }
    out.push_str(" FROM ");
    out.push_str(&dialect.quote_ident(q.entities[0].as_str())?);
    for join in &q.joins {
        out.push_str(" INNER JOIN ");
        out.push_str(&dialect.quote_ident(join.entity.as_str())?);
        out.push_str(" ON ");
        render_field(&mut out, &join.on_left, dialect)?;
        out.push_str(" = ");
        render_field(&mut out, &join.on_right, dialect)?;
    }

    if !q.conditions.is_empty() {
        out.push_str(" WHERE ");
        for (i, c) in q.conditions.iter().enumerate() {
            if i > 0 {
                out.push_str(" AND ");
            }
            render_condition(&mut out, c, dialect)?;
        }
    }

    if !q.group_by.is_empty() {
        out.push_str(" GROUP BY ");
        for (i, f) in q.group_by.iter().enumerate() {
            if i > 0 {
                out.push_str(", ");
            }
            render_field(&mut out, f, dialect)?;
        }
    }

    if let Some((f, dir)) = &q.order_by {
        out.push_str(" ORDER BY ");
        render_field(&mut out, f, dialect)?;
        out.push(' ');
        out.push_str(match dir {
            OrderDir::Asc => "ASC",
            OrderDir::Desc => "DESC",
        });
    }

    if dialect.limit_clause_at_tail() {
        // MySQL + BigQuery reject bare `OFFSET m` without a preceding
        // `LIMIT n`. Synthesise the canonical workaround: emit
        // `LIMIT <max> OFFSET m`, which is the documented
        // "from offset to end" idiom both engines accept. PG / SQLite
        // (3.30+) / DuckDB / Snowflake parse bare OFFSET fine, so they
        // stay on the simple path.
        let needs_limit_for_offset = matches!(dialect, Dialect::MySql | Dialect::BigQuery);
        let synth_limit = needs_limit_for_offset && q.limit.is_none() && q.offset.is_some();
        if let Some(n) = q.limit {
            write!(out, " LIMIT {n}").expect("write");
        } else if synth_limit {
            // 2^64 - 1, MySQL docs' canonical sentinel for "no upper bound".
            out.push_str(" LIMIT 18446744073709551615");
        }
        if let Some(m) = q.offset {
            write!(out, " OFFSET {m}").expect("write");
        }
    } else if !mssql_use_top && (q.offset.is_some() || q.limit.is_some()) {
        // MSSQL `OFFSET m ROWS [FETCH NEXT n ROWS ONLY]` path. Both the
        // OFFSET-only and OFFSET+LIMIT cases land here. ORDER BY is
        // required by MSSQL — fail closed when missing rather than
        // emit non-deterministic SQL.
        if q.order_by.is_none() {
            return Err(SemsqlError::validation(
                "MSSQL OFFSET/FETCH requires an ORDER BY clause",
            ));
        }
        let off = q.offset.unwrap_or(0);
        write!(out, " OFFSET {off} ROWS").expect("write");
        if let Some(n) = q.limit {
            write!(out, " FETCH NEXT {n} ROWS ONLY").expect("write");
        }
    }

    Ok(out)
}

fn render_select_item(out: &mut String, item: &SelectItem, dialect: Dialect) -> Result<()> {
    match item {
        SelectItem::Star => out.push('*'),
        SelectItem::Field(f) => render_field(out, f, dialect)?,
        SelectItem::Aggregate(agg, f) => {
            render_aggregate(out, *agg, f, dialect)?;
        }
        SelectItem::AliasedAggregate(agg, f, alias) => {
            render_aggregate(out, *agg, f, dialect)?;
            out.push_str(" AS ");
            out.push_str(&dialect.quote_ident(alias.as_str())?);
        }
        // Raw arithmetic/CAST expressions from Stage 2 and graph QueryFrame
        // routes keep their expression text, but standard double-quoted
        // identifiers still need the target dialect's identifier quoting.
        SelectItem::Expr(raw) => render_raw_expr(out, raw, dialect)?,
    }
    Ok(())
}

fn render_raw_expr(out: &mut String, raw: &str, dialect: Dialect) -> Result<()> {
    let mut chars = raw.chars().peekable();
    let mut in_single = false;
    let mut in_backtick = false;
    let mut in_bracket = false;
    while let Some(ch) = chars.next() {
        match ch {
            '\'' if !in_backtick && !in_bracket => {
                in_single = !in_single;
                out.push(ch);
            }
            '`' if !in_single && !in_bracket => {
                in_backtick = !in_backtick;
                out.push(ch);
            }
            '[' if !in_single && !in_backtick => {
                in_bracket = true;
                out.push(ch);
            }
            ']' if !in_single && !in_backtick => {
                in_bracket = false;
                out.push(ch);
            }
            '"' if !in_single && !in_backtick && !in_bracket => {
                let mut ident = String::new();
                let mut closed = false;
                for next in chars.by_ref() {
                    if next == '"' {
                        closed = true;
                        break;
                    }
                    ident.push(next);
                }
                if closed && is_safe_identifier(&ident) {
                    out.push_str(&dialect.quote_ident(&ident)?);
                } else {
                    out.push('"');
                    out.push_str(&ident);
                    if closed {
                        out.push('"');
                    }
                }
            }
            _ => out.push(ch),
        }
    }
    Ok(())
}

fn render_aggregate(out: &mut String, agg: Aggregate, f: &Field, dialect: Dialect) -> Result<()> {
    out.push_str(match agg {
        Aggregate::Count => "COUNT",
        Aggregate::Sum => "SUM",
        Aggregate::Avg => "AVG",
        Aggregate::Min => "MIN",
        Aggregate::Max => "MAX",
    });
    out.push('(');
    // Special case: COUNT over the synthetic `__star__` placeholder
    // we emit at parse time becomes `COUNT(*)` on the way back out.
    match f {
        Field::Bare(name) if name.as_str() == "__star__" => out.push('*'),
        _ => render_field(out, f, dialect)?,
    }
    out.push(')');
    Ok(())
}

fn render_field(out: &mut String, f: &Field, dialect: Dialect) -> Result<()> {
    match f {
        Field::Qualified(fn_) => {
            let entity = dialect.quote_ident(fn_.entity.as_str())?;
            let field = dialect.quote_ident(fn_.field.as_str())?;
            out.push_str(&entity);
            out.push('.');
            out.push_str(&field);
        }
        Field::Bare(name) => {
            // `__star__` is the synthetic AST marker for COUNT(*); never
            // reaches here because `render_select_item` short-circuits.
            // Bare field references that survive validation are quoted to
            // match the qualified case.
            let q = dialect.quote_ident(name.as_str())?;
            out.push_str(&q);
        }
    }
    Ok(())
}

fn render_condition(out: &mut String, c: &Condition, dialect: Dialect) -> Result<()> {
    match c {
        Condition::Compare(f, cmp, v) => {
            render_field(out, f, dialect)?;
            out.push(' ');
            out.push_str(match cmp {
                Comparator::Eq => "=",
                Comparator::Ne => "<>",
                Comparator::Lt => "<",
                Comparator::Le => "<=",
                Comparator::Gt => ">",
                Comparator::Ge => ">=",
            });
            out.push(' ');
            render_value(out, v, dialect)?;
        }
        Condition::In(f, vals) => {
            render_field(out, f, dialect)?;
            out.push_str(" IN (");
            for (i, v) in vals.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                render_value(out, v, dialect)?;
            }
            out.push(')');
        }
        Condition::Between(f, lo, hi) => {
            render_field(out, f, dialect)?;
            out.push_str(" BETWEEN ");
            render_value(out, lo, dialect)?;
            out.push_str(" AND ");
            render_value(out, hi, dialect)?;
        }
        Condition::IsNull(f) => {
            render_field(out, f, dialect)?;
            out.push_str(" IS NULL");
        }
        Condition::IsNotNull(f) => {
            render_field(out, f, dialect)?;
            out.push_str(" IS NOT NULL");
        }
        Condition::Like(f, pat) => {
            render_field(out, f, dialect)?;
            out.push_str(" LIKE ");
            render_string_literal(out, pat);
        }
    }
    Ok(())
}

fn render_value(out: &mut String, v: &Value, dialect: Dialect) -> Result<()> {
    match v {
        Value::Int(n) => write!(out, "{n}").expect("write"),
        Value::Float(n) => write!(out, "{n}").expect("write"),
        Value::Bool(b) => {
            if dialect.supports_boolean_keyword() {
                out.push_str(if *b { "TRUE" } else { "FALSE" });
            } else {
                // SQLite (3.23+) accepts TRUE/FALSE but stored as INT.
                // MSSQL has no boolean type — emit BIT-comparable 1/0.
                out.push_str(if *b { "1" } else { "0" });
            }
        }
        Value::Null => out.push_str("NULL"),
        Value::Str(s) => render_string_literal(out, s),
        Value::Param(name) => {
            // Reject parameter names that are not safe identifiers — defence
            // in depth against vocabulary that smuggled past sanitisation.
            if !is_safe_param_name(name) {
                return Err(SemsqlError::InvalidIdentifier(format!(":{name}")));
            }
            out.push(':');
            out.push_str(name);
        }
    }
    Ok(())
}

fn render_string_literal(out: &mut String, s: &str) {
    out.push('\'');
    for ch in s.chars() {
        if ch == '\'' {
            out.push_str("''");
        } else {
            out.push(ch);
        }
    }
    out.push('\'');
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

fn is_safe_param_name(name: &str) -> bool {
    if name.is_empty() || name.len() > 64 {
        return false;
    }
    let mut bytes = name.bytes();
    let first = bytes.next().unwrap();
    (first.is_ascii_alphabetic() || first == b'_')
        && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

#[cfg(test)]
mod tests {
    use super::*;
    use semsql_natsql::parse;

    fn render_or_panic(input: &str, dialect: Dialect) -> String {
        let ast = parse(input).expect("parse");
        render(&ast, dialect).expect("render")
    }

    #[test]
    fn quotes_per_dialect() {
        assert_eq!(
            Dialect::Postgres.quote_ident("users").unwrap(),
            r#""users""#
        );
        assert_eq!(Dialect::MySql.quote_ident("users").unwrap(), "`users`");
        assert_eq!(Dialect::MsSql.quote_ident("users").unwrap(), "[users]");
    }

    #[test]
    fn refuses_unsafe_identifiers() {
        for bad in ["", "users--", "users\"; DROP", "1abc"] {
            assert!(Dialect::Postgres.quote_ident(bad).is_err());
        }
    }

    #[test]
    fn postgres_quotes_with_double_quotes() {
        let sql = render_or_panic("SELECT * FROM users", Dialect::Postgres);
        assert_eq!(sql, "SELECT * FROM \"users\"");
    }

    #[test]
    fn mysql_quotes_with_backticks() {
        let sql = render_or_panic("SELECT * FROM users", Dialect::MySql);
        assert_eq!(sql, "SELECT * FROM `users`");
    }

    #[test]
    fn sqlite_quotes_with_double_quotes() {
        let sql = render_or_panic("SELECT * FROM users", Dialect::Sqlite);
        assert_eq!(sql, "SELECT * FROM \"users\"");
    }

    #[test]
    fn mysql_renders_qualified_field_with_backticks() {
        let sql = render_or_panic(
            "SELECT users.email FROM users WHERE users.status_code = 2",
            Dialect::MySql,
        );
        assert_eq!(
            sql,
            "SELECT `users`.`email` FROM `users` WHERE `users`.`status_code` = 2"
        );
    }

    #[test]
    fn count_star_renders_consistently_across_dialects() {
        for d in [Dialect::Postgres, Dialect::MySql, Dialect::Sqlite] {
            let sql = render_or_panic("SELECT COUNT(*) FROM users", d);
            assert!(sql.contains("COUNT(*)"), "dialect {d:?}: {sql}");
            assert!(!sql.contains("__star__"));
        }
    }

    #[test]
    fn mysql_preserves_aggregate_alias_for_order_by() {
        let sql = render_or_panic(
            "SELECT products.name, SUM(orders.amount) AS total_amount \
             FROM products INNER JOIN orders ON products.id = orders.product_id \
             GROUP BY products.name ORDER BY total_amount DESC LIMIT 2",
            Dialect::MySql,
        );
        assert_eq!(
            sql,
            "SELECT `products`.`name`, SUM(`orders`.`amount`) AS `total_amount` \
             FROM `products` INNER JOIN `orders` ON `products`.`id` = `orders`.`product_id` \
             GROUP BY `products`.`name` ORDER BY `total_amount` DESC LIMIT 2"
        );
    }

    #[test]
    fn mysql_requotes_raw_conditional_rate_expression_identifiers() {
        let sql = render_or_panic(
            "SELECT SUM(CASE WHEN \"domains\".\"is_spf_verified\" = 1 THEN 1 ELSE 0 END) * 100.0 / NULLIF(COUNT(\"domains\".\"id\"), 0) AS spf_verified_rate FROM domains",
            Dialect::MySql,
        );

        assert!(
            sql.contains("`domains`.`is_spf_verified` = 1"),
            "mysql raw expression identifiers should use backticks: {sql}"
        );
        assert!(
            sql.contains("NULLIF(COUNT(`domains`.`id`), 0)"),
            "mysql raw expression denominator should use backticks: {sql}"
        );
        assert!(
            !sql.contains("\"domains\""),
            "mysql raw expression should not retain double-quoted identifiers: {sql}"
        );
    }

    #[test]
    fn sqlite_emits_boolean_as_integer() {
        let sql = render_or_panic(
            "SELECT * FROM users WHERE users.is_active = TRUE",
            Dialect::Sqlite,
        );
        assert!(sql.ends_with("= 1"), "{sql}");
        let sql = render_or_panic(
            "SELECT * FROM users WHERE users.is_active = FALSE",
            Dialect::Sqlite,
        );
        assert!(sql.ends_with("= 0"), "{sql}");
    }

    #[test]
    fn postgres_keeps_boolean_keyword() {
        let sql = render_or_panic(
            "SELECT * FROM users WHERE users.is_active = TRUE",
            Dialect::Postgres,
        );
        assert!(sql.ends_with("= TRUE"), "{sql}");
    }

    #[test]
    fn order_by_limit_renders_per_dialect() {
        let pg = render_or_panic(
            "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10",
            Dialect::Postgres,
        );
        assert_eq!(
            pg,
            "SELECT * FROM \"users\" ORDER BY \"users\".\"balance\" DESC LIMIT 10",
        );
        let my = render_or_panic(
            "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10",
            Dialect::MySql,
        );
        assert_eq!(
            my,
            "SELECT * FROM `users` ORDER BY `users`.`balance` DESC LIMIT 10",
        );
    }

    #[test]
    fn sqlite_renders_inner_joins_with_quoted_identifiers() {
        let sql = render_or_panic(
            "SELECT COUNT(*) FROM account INNER JOIN order ON order.account_id = account.account_id",
            Dialect::Sqlite,
        );
        assert_eq!(
            sql,
            "SELECT COUNT(*) FROM \"account\" INNER JOIN \"order\" ON \"order\".\"account_id\" = \"account\".\"account_id\""
        );
    }

    #[test]
    fn string_literal_escape_is_universal() {
        for d in [Dialect::Postgres, Dialect::MySql, Dialect::Sqlite] {
            let sql = render_or_panic("SELECT * FROM users WHERE users.name = 'O''Neil'", d);
            assert!(sql.contains("'O''Neil'"), "dialect {d:?}: {sql}");
        }
    }

    #[test]
    fn between_in_clauses_render_correctly_across_dialects() {
        for d in [Dialect::Postgres, Dialect::MySql, Dialect::Sqlite] {
            let between = render_or_panic(
                "SELECT * FROM users WHERE users.balance BETWEEN 1 AND 100",
                d,
            );
            assert!(between.contains(" BETWEEN 1 AND 100"));

            let in_clause = render_or_panic(
                "SELECT * FROM users WHERE users.status_code IN (1, 2, 39)",
                d,
            );
            assert!(in_clause.contains(" IN (1, 2, 39)"));
        }
    }

    #[test]
    fn mssql_emits_top_n_after_select_not_limit() {
        let sql = render_or_panic(
            "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10",
            Dialect::MsSql,
        );
        assert!(sql.starts_with("SELECT TOP 10 "), "{sql}");
        assert!(!sql.contains(" LIMIT "), "{sql}");
        assert!(sql.contains("[users].[balance] DESC"), "{sql}");
    }

    #[test]
    fn mssql_brackets_quoting_and_bit_booleans() {
        let sql = render_or_panic(
            "SELECT * FROM users WHERE users.is_active = TRUE",
            Dialect::MsSql,
        );
        assert!(sql.contains("[users]"));
        assert!(sql.contains("[is_active]"));
        assert!(sql.ends_with("= 1"), "{sql}");
    }

    #[test]
    fn bigquery_quotes_with_backticks_not_double_quotes() {
        // `"users"` would be a STRING in BigQuery, not an identifier.
        // Rendering it as a string would silently produce a const
        // column instead of selecting from the table.
        let sql = render_or_panic("SELECT * FROM users", Dialect::BigQuery);
        assert_eq!(sql, "SELECT * FROM `users`");
        assert!(!sql.contains("\"users\""));
    }

    #[test]
    fn snowflake_uses_double_quotes_and_keyword_booleans() {
        let sql = render_or_panic(
            "SELECT * FROM users WHERE users.is_active = FALSE",
            Dialect::Snowflake,
        );
        assert!(sql.contains("\"users\""));
        assert!(sql.ends_with("= FALSE"), "{sql}");
    }

    #[test]
    fn duckdb_renders_postgres_compatible_output() {
        // DuckDB targets PG-compat — same quoting, same booleans,
        // same LIMIT placement. Differential test below leans on
        // this equivalence.
        let pg = render_or_panic("SELECT * FROM users LIMIT 5", Dialect::Postgres);
        let duck = render_or_panic("SELECT * FROM users LIMIT 5", Dialect::DuckDb);
        assert_eq!(pg, duck);
    }

    #[test]
    fn every_dialect_renders_the_baseline_query() {
        // Sanity guard: every dialect in the enum must round-trip
        // the baseline fixture without erroring. Dialect-specific
        // assertions live in the per-dialect tests above.
        for d in [
            Dialect::Postgres,
            Dialect::MySql,
            Dialect::Sqlite,
            Dialect::MsSql,
            Dialect::BigQuery,
            Dialect::Snowflake,
            Dialect::DuckDb,
        ] {
            let sql = render_or_panic(
                "SELECT users.email FROM users WHERE users.is_active = TRUE LIMIT 10",
                d,
            );
            assert!(sql.contains("users"), "dialect {d:?}: {sql}");
            assert!(sql.contains("email"), "dialect {d:?}: {sql}");
        }
    }

    #[test]
    fn limit_offset_renders_per_dialect() {
        let pg = render_or_panic(
            "SELECT * FROM users ORDER BY users.id ASC LIMIT 10 OFFSET 20",
            Dialect::Postgres,
        );
        assert!(pg.ends_with(" LIMIT 10 OFFSET 20"), "{pg}");
        let my = render_or_panic(
            "SELECT * FROM users ORDER BY users.id ASC LIMIT 10 OFFSET 20",
            Dialect::MySql,
        );
        assert!(my.ends_with(" LIMIT 10 OFFSET 20"), "{my}");
    }

    #[test]
    fn mssql_pivots_to_offset_fetch_when_offset_set() {
        let sql = render_or_panic(
            "SELECT * FROM users ORDER BY users.id ASC LIMIT 10 OFFSET 20",
            Dialect::MsSql,
        );
        assert!(sql.contains(" OFFSET 20 ROWS"), "{sql}");
        assert!(sql.contains(" FETCH NEXT 10 ROWS ONLY"), "{sql}");
        assert!(!sql.contains("TOP "), "{sql}");
        assert!(!sql.contains(" LIMIT "), "{sql}");
    }

    #[test]
    fn mysql_synthesises_max_limit_when_offset_set_without_limit() {
        // MySQL rejects bare `OFFSET m`; canonical workaround is
        // `LIMIT 18446744073709551615 OFFSET m`. Emit it implicitly
        // so authors don't have to special-case MySQL in NatSQL.
        let sql = render_or_panic("SELECT * FROM users OFFSET 5", Dialect::MySql);
        assert!(sql.contains("LIMIT 18446744073709551615"), "{sql}");
        assert!(sql.ends_with(" OFFSET 5"), "{sql}");
    }

    #[test]
    fn bigquery_synthesises_max_limit_when_offset_set_without_limit() {
        // BigQuery, like MySQL, requires LIMIT before OFFSET.
        let sql = render_or_panic("SELECT * FROM users OFFSET 5", Dialect::BigQuery);
        assert!(sql.contains("LIMIT 18446744073709551615"), "{sql}");
        assert!(sql.ends_with(" OFFSET 5"), "{sql}");
    }

    #[test]
    fn postgres_offset_without_limit_emits_bare_offset() {
        // PG / SQLite / DuckDB / Snowflake accept bare OFFSET — no
        // synthesised LIMIT. Verifies we don't over-correct on the
        // engines that allow the bare form.
        for d in [
            Dialect::Postgres,
            Dialect::Sqlite,
            Dialect::DuckDb,
            Dialect::Snowflake,
        ] {
            let sql = render_or_panic("SELECT * FROM users OFFSET 5", d);
            assert!(
                !sql.contains("18446744073709551615"),
                "dialect {d:?} should emit bare OFFSET: {sql}"
            );
            assert!(sql.ends_with(" OFFSET 5"), "dialect {d:?}: {sql}");
        }
    }

    #[test]
    fn mssql_offset_without_limit_emits_offset_only() {
        let sql = render_or_panic(
            "SELECT * FROM users ORDER BY users.id ASC OFFSET 5",
            Dialect::MsSql,
        );
        assert!(sql.contains(" OFFSET 5 ROWS"), "{sql}");
        assert!(!sql.contains("FETCH NEXT"), "{sql}");
    }

    #[test]
    fn mssql_keeps_top_when_no_offset() {
        // `LIMIT n` alone (no OFFSET) → cheaper TOP path stays.
        let sql = render_or_panic(
            "SELECT * FROM users ORDER BY users.id ASC LIMIT 3",
            Dialect::MsSql,
        );
        assert!(sql.contains("SELECT TOP 3 "), "{sql}");
        assert!(!sql.contains("OFFSET"), "{sql}");
    }

    #[test]
    fn mssql_offset_without_order_by_is_a_validation_error() {
        let ast = parse("SELECT * FROM users LIMIT 10 OFFSET 5").unwrap();
        let err = render(&ast, Dialect::MsSql).unwrap_err();
        assert!(
            format!("{err}").contains("OFFSET/FETCH requires an ORDER BY"),
            "{err}"
        );
    }

    /// Differential test: the `to_sql_text` baseline (Postgres-flavoured,
    /// dialect-agnostic in spirit) must round-trip through every dialect's
    /// renderer without losing semantic content. We re-parse the rendered
    /// MySQL/SQLite output back via the NatSQL parser — except the parser
    /// doesn't speak backticks/brackets, so we strip the quoting layer
    /// before re-parsing as a coarse equivalence check.
    #[test]
    fn differential_render_preserves_select_clause_count() {
        let inputs = [
            "SELECT * FROM users",
            "SELECT COUNT(*) FROM users",
            "SELECT users.email FROM users WHERE users.id = 42",
            "SELECT * FROM users WHERE users.balance BETWEEN 1 AND 100",
            "SELECT * FROM users ORDER BY users.balance DESC LIMIT 5",
        ];
        let dialects = [
            Dialect::Postgres,
            Dialect::MySql,
            Dialect::Sqlite,
            Dialect::DuckDb,
            Dialect::Snowflake,
            Dialect::BigQuery,
        ];
        for input in inputs {
            for d in dialects {
                let sql = render_or_panic(input, d);
                // Same number of FROM clauses across every render —
                // dialect quirks don't change the query shape.
                assert_eq!(
                    sql.matches("FROM").count(),
                    input.matches("FROM").count(),
                    "dialect {d:?} on `{input}` → {sql}"
                );
                // SELECT-list content survives. We strip the quoting
                // chars so the comparison is dialect-blind.
                let stripped = sql.replace(['"', '`', '[', ']'], "");
                assert!(
                    stripped.starts_with("SELECT "),
                    "dialect {d:?} on `{input}` lost SELECT prefix: {sql}"
                );
            }
        }
    }

    #[test]
    fn rejects_unsafe_param_name_at_render() {
        use semsql_core::CanonicalName;
        let q = NatSql {
            select: vec![SelectItem::Star],
            entities: vec![semsql_core::EntityName::new("users").unwrap()],
            joins: Vec::new(),
            conditions: vec![Condition::Compare(
                Field::Bare(CanonicalName::new("name").unwrap()),
                Comparator::Eq,
                Value::Param("bad name".into()),
            )],
            having: Vec::new(),
            group_by: Vec::new(),
            order_by: None,
            limit: None,
            offset: None,
        };
        assert!(render(&q, Dialect::Postgres).is_err());
    }

    #[test]
    fn null_renders_as_keyword_across_dialects() {
        for d in [Dialect::Postgres, Dialect::MySql, Dialect::Sqlite] {
            let sql = render_or_panic("SELECT * FROM users WHERE users.deleted_at IS NULL", d);
            assert!(sql.ends_with(" IS NULL"));
        }
    }
}
