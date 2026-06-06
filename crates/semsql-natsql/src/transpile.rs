//! NatSQL → SQL text transpiler.
//!
//! Deterministic — pure function of input AST. No schema lookup, no model.
//! This is the reproducible boundary between the cascade and the rewriter
//! pipeline.
//!
//! Output is dialect-agnostic SQL the Python rewriter (sqlglot) and the
//! Rust second-pass parser (sqlparser-rs) both accept. The dialect
//! renderer (`semsql-renderer`) handles last-mile per-engine emission
//! after validation + injection.

use crate::ast::{
    Aggregate, Comparator, Condition, Field, JoinClause, NatSql, OrderDir, SelectItem, Value,
};
use semsql_core::{Result, SemsqlError};
use std::fmt::Write;

/// Render a NatSQL AST as a SQL string.
pub fn to_sql_text(q: &NatSql) -> Result<String> {
    if q.entities.is_empty() {
        return Err(SemsqlError::validation(
            "natsql AST has no entities — cannot render FROM clause",
        ));
    }

    let mut out = String::new();
    out.push_str("SELECT ");
    if q.select.is_empty() {
        out.push('*');
    } else {
        for (i, item) in q.select.iter().enumerate() {
            if i > 0 {
                out.push_str(", ");
            }
            render_select_item(&mut out, item)?;
        }
    }
    write!(out, " FROM {}", q.entities[0].as_str()).expect("write");

    for jc in &q.joins {
        render_join(&mut out, jc);
    }

    if !q.conditions.is_empty() {
        out.push_str(" WHERE ");
        for (i, c) in q.conditions.iter().enumerate() {
            if i > 0 {
                out.push_str(" AND ");
            }
            render_condition(&mut out, c)?;
        }
    }

    if !q.group_by.is_empty() {
        out.push_str(" GROUP BY ");
        for (i, f) in q.group_by.iter().enumerate() {
            if i > 0 {
                out.push_str(", ");
            }
            render_field(&mut out, f);
        }
    }

    if !q.having.is_empty() {
        out.push_str(" HAVING ");
        for (i, c) in q.having.iter().enumerate() {
            if i > 0 {
                out.push_str(" AND ");
            }
            render_condition(&mut out, c)?;
        }
    }

    if let Some((f, dir)) = &q.order_by {
        out.push_str(" ORDER BY ");
        render_field(&mut out, f);
        out.push(' ');
        out.push_str(match dir {
            OrderDir::Asc => "ASC",
            OrderDir::Desc => "DESC",
        });
    }

    if let Some(n) = q.limit {
        write!(out, " LIMIT {n}").expect("write");
    }
    if let Some(n) = q.offset {
        write!(out, " OFFSET {n}").expect("write");
    }

    Ok(out)
}

fn render_join(out: &mut String, jc: &JoinClause) {
    write!(out, " INNER JOIN {}", jc.entity.as_str()).expect("write");
    out.push_str(" ON ");
    render_field(out, &jc.on_left);
    out.push_str(" = ");
    render_field(out, &jc.on_right);
}

fn render_select_item(out: &mut String, item: &SelectItem) -> Result<()> {
    match item {
        SelectItem::Star => out.push('*'),
        SelectItem::Field(f) => render_field(out, f),
        SelectItem::Aggregate(agg, f) => {
            render_aggregate(out, *agg, f);
        }
        SelectItem::AliasedAggregate(agg, f, alias) => {
            render_aggregate(out, *agg, f);
            write!(out, " AS {}", alias.as_str()).expect("write");
        }
        SelectItem::Expr(raw) => out.push_str(raw),
    }
    Ok(())
}

fn render_aggregate(out: &mut String, agg: Aggregate, f: &Field) {
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
        _ => render_field(out, f),
    }
    out.push(')');
}

fn render_field(out: &mut String, f: &Field) {
    match f {
        Field::Qualified(fn_) => {
            write!(out, "{}.{}", fn_.entity.as_str(), fn_.field.as_str()).expect("write")
        }
        Field::Bare(name) => out.push_str(name.as_str()),
    }
}

fn render_condition(out: &mut String, c: &Condition) -> Result<()> {
    match c {
        Condition::Compare(f, cmp, v) => {
            render_field(out, f);
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
            render_value(out, v)?;
        }
        Condition::In(f, vals) => {
            render_field(out, f);
            out.push_str(" IN (");
            for (i, v) in vals.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                render_value(out, v)?;
            }
            out.push(')');
        }
        Condition::Between(f, lo, hi) => {
            render_field(out, f);
            out.push_str(" BETWEEN ");
            render_value(out, lo)?;
            out.push_str(" AND ");
            render_value(out, hi)?;
        }
        Condition::IsNull(f) => {
            render_field(out, f);
            out.push_str(" IS NULL");
        }
        Condition::IsNotNull(f) => {
            render_field(out, f);
            out.push_str(" IS NOT NULL");
        }
        Condition::Like(f, pat) => {
            render_field(out, f);
            out.push_str(" LIKE ");
            render_string_literal(out, pat);
        }
    }
    Ok(())
}

fn render_value(out: &mut String, v: &Value) -> Result<()> {
    match v {
        Value::Int(n) => write!(out, "{n}").expect("write"),
        Value::Float(n) => write!(out, "{n}").expect("write"),
        Value::Bool(true) => out.push_str("TRUE"),
        Value::Bool(false) => out.push_str("FALSE"),
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
    use crate::parse;

    fn round_trip(input: &str, expected: &str) {
        let parsed = parse(input).expect("parse");
        let rendered = to_sql_text(&parsed).expect("render");
        assert_eq!(rendered, expected, "round-trip mismatch for {input:?}");
        // Idempotence: re-parsing the rendered output yields the same AST.
        let reparsed = parse(&rendered).expect("re-parse");
        assert_eq!(reparsed, parsed);
    }

    #[test]
    fn star_select() {
        round_trip("SELECT * FROM users", "SELECT * FROM users");
    }

    #[test]
    fn filter_eq_int() {
        round_trip(
            "SELECT * FROM users WHERE users.status_code = 2",
            "SELECT * FROM users WHERE users.status_code = 2",
        );
    }

    #[test]
    fn filter_eq_string_literal_escapes() {
        round_trip(
            "SELECT * FROM users WHERE users.name = 'O''Neil'",
            "SELECT * FROM users WHERE users.name = 'O''Neil'",
        );
    }

    #[test]
    fn filter_param() {
        round_trip(
            "SELECT * FROM users WHERE users.name = :s",
            "SELECT * FROM users WHERE users.name = :s",
        );
    }

    #[test]
    fn count_star() {
        round_trip("SELECT COUNT(*) FROM users", "SELECT COUNT(*) FROM users");
    }

    #[test]
    fn sum_field() {
        round_trip(
            "SELECT SUM(users.balance) FROM users",
            "SELECT SUM(users.balance) FROM users",
        );
    }

    #[test]
    fn order_limit() {
        round_trip(
            "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10",
            "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10",
        );
    }

    #[test]
    fn limit_offset_round_trip() {
        round_trip(
            "SELECT * FROM users LIMIT 10 OFFSET 20",
            "SELECT * FROM users LIMIT 10 OFFSET 20",
        );
    }

    #[test]
    fn offset_only_round_trip() {
        round_trip(
            "SELECT * FROM users OFFSET 5",
            "SELECT * FROM users OFFSET 5",
        );
    }

    #[test]
    fn between_clause() {
        round_trip(
            "SELECT * FROM users WHERE users.balance BETWEEN 1 AND 100",
            "SELECT * FROM users WHERE users.balance BETWEEN 1 AND 100",
        );
    }

    #[test]
    fn in_clause() {
        round_trip(
            "SELECT * FROM users WHERE users.status_code IN (1, 2, 39)",
            "SELECT * FROM users WHERE users.status_code IN (1, 2, 39)",
        );
    }

    #[test]
    fn inner_join_round_trip() {
        round_trip(
            "SELECT * FROM users INNER JOIN orders ON orders.user_id = users.id",
            "SELECT * FROM users INNER JOIN orders ON orders.user_id = users.id",
        );
    }

    #[test]
    fn two_inner_joins_round_trip() {
        round_trip(
            "SELECT * FROM users INNER JOIN orders ON orders.user_id = users.id INNER JOIN items ON items.order_id = orders.id",
            "SELECT * FROM users INNER JOIN orders ON orders.user_id = users.id INNER JOIN items ON items.order_id = orders.id",
        );
    }

    #[test]
    fn having_clause_round_trip() {
        round_trip(
            "SELECT users.status_code FROM users GROUP BY users.status_code HAVING users.status_code > 2",
            "SELECT users.status_code FROM users GROUP BY users.status_code HAVING users.status_code > 2",
        );
    }

    #[test]
    fn arithmetic_expr_round_trip() {
        round_trip(
            "SELECT CAST(frpm.free_meals AS REAL) / frpm.enrollment FROM frpm",
            "SELECT CAST(frpm.free_meals AS REAL) / frpm.enrollment FROM frpm",
        );
    }

    #[test]
    fn broader_sql_surface_accepts_valid_selects_outside_typed_ir() {
        crate::validate_select_sql_surface(
            "SELECT schools.city FROM schools GROUP BY schools.city ORDER BY COUNT(*) DESC LIMIT 1",
        )
        .expect("aggregate ORDER BY should be a valid SQL surface");
        crate::validate_select_sql_surface(
            "SELECT schools.website FROM schools WHERE NOT schools.website IS NULL",
        )
        .expect("NOT x IS NULL should be a valid SQL surface");
        crate::validate_select_sql_surface(
            "SELECT schools.county FROM schools WHERE schools.county = 'A' OR schools.county = 'B'",
        )
        .expect("OR should be a valid SQL surface");
    }

    #[test]
    fn broader_sql_surface_rejects_non_selects_and_malformed_sql() {
        assert!(crate::validate_select_sql_surface("DELETE FROM users").is_err());
        assert!(crate::validate_select_sql_surface("SELECT * FROM").is_err());
        assert!(
            crate::validate_select_sql_surface("SELECT * FROM users; SELECT * FROM orders")
                .is_err()
        );
    }

    #[test]
    fn rejects_unsafe_param_name() {
        // Construct a NatSql AST with a hostile parameter name and try to
        // render — the renderer must refuse rather than emit `:bad-name`.
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
        assert!(to_sql_text(&q).is_err());
    }
}
