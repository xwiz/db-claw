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

use crate::ast::{Aggregate, Comparator, Condition, Field, NatSql, OrderDir, SelectItem, Value};
use semsql_core::{Result, SemsqlError};
use std::fmt::Write;

/// Render a NatSQL AST as a SQL string.
pub fn to_sql_text(q: &NatSql) -> Result<String> {
    if q.entities.is_empty() {
        return Err(SemsqlError::validation(
            "natsql AST has no entities — cannot render FROM clause",
        ));
    }
    if q.entities.len() > 1 {
        return Err(SemsqlError::validation(
            "natsql v0.2 transpile supports a single FROM entity",
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

    Ok(out)
}

fn render_select_item(out: &mut String, item: &SelectItem) -> Result<()> {
    match item {
        SelectItem::Star => out.push('*'),
        SelectItem::Field(f) => render_field(out, f),
        SelectItem::Aggregate(agg, f) => {
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
    }
    Ok(())
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
    fn rejects_unsafe_param_name() {
        // Construct a NatSql AST with a hostile parameter name and try to
        // render — the renderer must refuse rather than emit `:bad-name`.
        use semsql_core::CanonicalName;
        let q = NatSql {
            select: vec![SelectItem::Star],
            entities: vec![semsql_core::EntityName::new("users").unwrap()],
            conditions: vec![Condition::Compare(
                Field::Bare(CanonicalName::new("name").unwrap()),
                Comparator::Eq,
                Value::Param("bad name".into()),
            )],
            group_by: Vec::new(),
            order_by: None,
            limit: None,
        };
        assert!(to_sql_text(&q).is_err());
    }
}
