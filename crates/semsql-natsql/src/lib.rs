//! NatSQL — the intermediate representation Stage 2 generates.
//!
//! NatSQL strips JOIN ON / HAVING and disallows nested subqueries — the
//! published research ([Findings of EMNLP
//! 2021](https://aclanthology.org/2021.findings-emnlp.174/)) shows this
//! materially improves NL→SQL accuracy on small models.
//!
//! ## Two surfaces
//!
//! - **Skeleton NatSQL** — placeholder names like `@entity1`, `@field2`,
//!   `@val3`. Stage 2 emits this; llguidance constrains it.
//! - **Concrete NatSQL** — every placeholder filled by Stage 3. The
//!   transpiler accepts this and emits a SQL string.
//!
//! The v0.2 cut accepts single-entity queries (one FROM table, no JOIN, no
//! subquery, no HAVING). Multi-entity joins via relationship-graph
//! inference land in v0.5.
//!
//! Parsing piggy-backs on `sqlparser-rs` because NatSQL at v0.2 is a strict
//! subset of standard SQL. We re-validate the AST against our grammar
//! constraints (no JOINs, no subqueries, single FROM) and emit a typed
//! [`NatSql`] for downstream consumers.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use semsql_core::{Result, SemsqlError};
use sqlparser::ast as sql_ast;
use sqlparser::dialect::GenericDialect;
use sqlparser::parser::Parser;

pub mod ast;
pub mod transpile;

pub use ast::{Aggregate, Comparator, Condition, Field, NatSql, OrderDir, SelectItem, Value};

/// Parse a concrete NatSQL string into the typed AST.
///
/// Errors out on any construct outside the v0.2 NatSQL subset (multi-table
/// FROM, JOIN, HAVING, subqueries, set operations).
pub fn parse(text: &str) -> Result<NatSql> {
    let dialect = GenericDialect {};
    let stmts = Parser::parse_sql(&dialect, text)
        .map_err(|e| SemsqlError::validation(format!("natsql parse: {e}")))?;
    if stmts.len() != 1 {
        return Err(SemsqlError::validation(format!(
            "expected exactly one statement, got {}",
            stmts.len()
        )));
    }
    let query = match &stmts[0] {
        sql_ast::Statement::Query(q) => q.as_ref(),
        other => {
            return Err(SemsqlError::validation(format!(
                "natsql expects a SELECT, got {}",
                other
            )))
        }
    };
    if query.with.is_some() {
        return Err(SemsqlError::validation("natsql v0.2 does not support CTEs"));
    }
    let select = match query.body.as_ref() {
        sql_ast::SetExpr::Select(s) => s.as_ref(),
        sql_ast::SetExpr::Query(_)
        | sql_ast::SetExpr::SetOperation { .. }
        | sql_ast::SetExpr::Values(_)
        | sql_ast::SetExpr::Insert(_)
        | sql_ast::SetExpr::Update(_)
        | sql_ast::SetExpr::Table(_) => {
            return Err(SemsqlError::validation(
                "natsql v0.2 supports only single-statement SELECT (no UNION/CTE/etc.)",
            ));
        }
    };
    if select.having.is_some() {
        return Err(SemsqlError::validation("natsql v0.2 does not support HAVING"));
    }

    convert::query_to_natsql(query, select)
}

mod convert {
    //! sqlparser AST → typed NatSQL AST.

    use super::*;
    use semsql_core::{CanonicalName, EntityName, FieldName};

    pub(super) fn query_to_natsql(
        query: &sql_ast::Query,
        select: &sql_ast::Select,
    ) -> Result<NatSql> {
        let entities = collect_entities(select)?;
        let select_items = select
            .projection
            .iter()
            .map(select_item_from)
            .collect::<Result<Vec<_>>>()?;
        let conditions = match &select.selection {
            Some(expr) => flatten_and(expr)?,
            None => Vec::new(),
        };
        let group_by = match &select.group_by {
            sql_ast::GroupByExpr::Expressions(exprs, _modifiers) => exprs
                .iter()
                .map(field_from_expr)
                .collect::<Result<Vec<_>>>()?,
            sql_ast::GroupByExpr::All(_) => Vec::new(),
        };
        let order_by = match query.order_by.as_ref() {
            Some(ob) => order_by_from(ob)?,
            None => None,
        };
        let limit = limit_from(query.limit.as_ref())?;

        Ok(NatSql {
            select: select_items,
            entities,
            conditions,
            group_by,
            order_by,
            limit,
        })
    }

    fn collect_entities(select: &sql_ast::Select) -> Result<Vec<EntityName>> {
        if select.from.is_empty() {
            return Ok(Vec::new());
        }
        if select.from.len() > 1 {
            return Err(SemsqlError::validation(
                "natsql v0.2 supports a single FROM table; multi-entity joins land in v0.5",
            ));
        }
        let twj = &select.from[0];
        if !twj.joins.is_empty() {
            return Err(SemsqlError::validation(
                "natsql v0.2 does not support JOIN; relationship-graph join inference lands in v0.5",
            ));
        }
        match &twj.relation {
            sql_ast::TableFactor::Table { name, .. } => {
                let last = name
                    .0
                    .last()
                    .ok_or_else(|| SemsqlError::validation("empty table name in FROM"))?;
                Ok(vec![EntityName::new(strip_quotes(&last.to_string()))?])
            }
            sql_ast::TableFactor::Derived { .. } => Err(SemsqlError::validation(
                "natsql v0.2 does not support subqueries in FROM",
            )),
            other => Err(SemsqlError::validation(format!(
                "unsupported FROM table-factor: {other:?}"
            ))),
        }
    }

    fn select_item_from(item: &sql_ast::SelectItem) -> Result<SelectItem> {
        match item {
            sql_ast::SelectItem::Wildcard(_) => Ok(SelectItem::Star),
            sql_ast::SelectItem::UnnamedExpr(e) | sql_ast::SelectItem::ExprWithAlias { expr: e, .. } => {
                if let Some((agg, inner)) = aggregate_from(e) {
                    let f = field_from_expr(&inner)?;
                    return Ok(SelectItem::Aggregate(agg, f));
                }
                Ok(SelectItem::Field(field_from_expr(e)?))
            }
            sql_ast::SelectItem::QualifiedWildcard(_, _) => Err(SemsqlError::validation(
                "natsql v0.2 does not support qualified wildcards",
            )),
        }
    }

    fn aggregate_from(e: &sql_ast::Expr) -> Option<(Aggregate, sql_ast::Expr)> {
        let func = match e {
            sql_ast::Expr::Function(f) => f,
            _ => return None,
        };
        let name = func.name.0.last()?.to_string().to_uppercase();
        let agg = match name.as_str() {
            "COUNT" => Aggregate::Count,
            "SUM" => Aggregate::Sum,
            "AVG" => Aggregate::Avg,
            "MIN" => Aggregate::Min,
            "MAX" => Aggregate::Max,
            _ => return None,
        };
        let args = match &func.args {
            sql_ast::FunctionArguments::List(list) => &list.args,
            _ => return None,
        };
        let inner = match args.first()? {
            sql_ast::FunctionArg::Unnamed(sql_ast::FunctionArgExpr::Expr(e)) => e.clone(),
            sql_ast::FunctionArg::Unnamed(sql_ast::FunctionArgExpr::Wildcard) => {
                // COUNT(*) is `Aggregate(Count, Field::Bare("*"))` in our IR.
                let name = CanonicalName::new("__star__").ok()?;
                return Some((agg, sql_ast::Expr::Identifier(sql_ast::Ident::new(name.as_str()))));
            }
            _ => return None,
        };
        Some((agg, inner))
    }

    fn field_from_expr(e: &sql_ast::Expr) -> Result<Field> {
        match e {
            sql_ast::Expr::Identifier(id) => Ok(Field::Bare(CanonicalName::new(strip_quotes(
                id.value.as_str(),
            ))?)),
            sql_ast::Expr::CompoundIdentifier(parts) => {
                if parts.len() != 2 {
                    return Err(SemsqlError::validation(format!(
                        "natsql expects entity.field, got {} parts",
                        parts.len()
                    )));
                }
                Ok(Field::Qualified(FieldName {
                    entity: EntityName::new(strip_quotes(parts[0].value.as_str()))?,
                    field: CanonicalName::new(strip_quotes(parts[1].value.as_str()))?,
                }))
            }
            other => Err(SemsqlError::validation(format!(
                "natsql expects an identifier or `entity.field`, got {other}"
            ))),
        }
    }

    fn flatten_and(e: &sql_ast::Expr) -> Result<Vec<Condition>> {
        if let sql_ast::Expr::BinaryOp { left, op: sql_ast::BinaryOperator::And, right } = e {
            let mut out = flatten_and(left)?;
            out.extend(flatten_and(right)?);
            return Ok(out);
        }
        Ok(vec![condition_from(e)?])
    }

    fn condition_from(e: &sql_ast::Expr) -> Result<Condition> {
        use sql_ast::BinaryOperator as Op;
        match e {
            sql_ast::Expr::IsNull(inner) => Ok(Condition::IsNull(field_from_expr(inner)?)),
            sql_ast::Expr::IsNotNull(inner) => Ok(Condition::IsNotNull(field_from_expr(inner)?)),
            sql_ast::Expr::Like { negated: false, expr, pattern, .. } => {
                let f = field_from_expr(expr)?;
                let pat = match pattern.as_ref() {
                    sql_ast::Expr::Value(sql_ast::Value::SingleQuotedString(s)) => s.clone(),
                    other => return Err(SemsqlError::validation(format!("LIKE pattern must be a string literal, got {other}"))),
                };
                Ok(Condition::Like(f, pat))
            }
            sql_ast::Expr::InList { expr, list, negated: false } => {
                let f = field_from_expr(expr)?;
                let vals = list.iter().map(value_from).collect::<Result<Vec<_>>>()?;
                Ok(Condition::In(f, vals))
            }
            sql_ast::Expr::Between {
                expr,
                negated: false,
                low,
                high,
            } => {
                let f = field_from_expr(expr)?;
                Ok(Condition::Between(f, value_from(low)?, value_from(high)?))
            }
            sql_ast::Expr::BinaryOp { left, op, right } => {
                let cmp = match op {
                    Op::Eq => Comparator::Eq,
                    Op::NotEq => Comparator::Ne,
                    Op::Lt => Comparator::Lt,
                    Op::LtEq => Comparator::Le,
                    Op::Gt => Comparator::Gt,
                    Op::GtEq => Comparator::Ge,
                    other => {
                        return Err(SemsqlError::validation(format!(
                            "unsupported comparator: {other:?}"
                        )))
                    }
                };
                Ok(Condition::Compare(
                    field_from_expr(left)?,
                    cmp,
                    value_from(right)?,
                ))
            }
            other => Err(SemsqlError::validation(format!(
                "unsupported WHERE expression: {other}"
            ))),
        }
    }

    fn value_from(e: &sql_ast::Expr) -> Result<Value> {
        match e {
            sql_ast::Expr::Value(v) => match v {
                sql_ast::Value::Number(n, _) => {
                    if let Ok(i) = n.parse::<i64>() {
                        Ok(Value::Int(i))
                    } else if let Ok(f) = n.parse::<f64>() {
                        Ok(Value::Float(f))
                    } else {
                        Err(SemsqlError::validation(format!(
                            "could not parse number literal {n}"
                        )))
                    }
                }
                sql_ast::Value::SingleQuotedString(s)
                | sql_ast::Value::DoubleQuotedString(s) => Ok(Value::Str(s.clone())),
                sql_ast::Value::Boolean(b) => Ok(Value::Bool(*b)),
                sql_ast::Value::Null => Ok(Value::Null),
                sql_ast::Value::Placeholder(p) => {
                    let name = p.trim_start_matches(|c: char| c == ':' || c == '@' || c == '$' || c == '?');
                    Ok(Value::Param(name.to_string()))
                }
                other => Err(SemsqlError::validation(format!(
                    "unsupported literal: {other:?}"
                ))),
            },
            sql_ast::Expr::Identifier(id) => {
                // Treat bareword `true` / `false` / `null` as literals when
                // upstream parsers normalised them away.
                let lc = id.value.to_ascii_lowercase();
                match lc.as_str() {
                    "true" => Ok(Value::Bool(true)),
                    "false" => Ok(Value::Bool(false)),
                    "null" => Ok(Value::Null),
                    _ => Err(SemsqlError::validation(format!(
                        "expected literal value, got identifier `{}`",
                        id.value
                    ))),
                }
            }
            other => Err(SemsqlError::validation(format!(
                "expected literal, got {other}"
            ))),
        }
    }

    fn order_by_from(ob: &sql_ast::OrderBy) -> Result<Option<(Field, OrderDir)>> {
        let exprs = &ob.exprs;
        if exprs.is_empty() {
            return Ok(None);
        }
        if exprs.len() > 1 {
            return Err(SemsqlError::validation(
                "natsql v0.2 supports a single ORDER BY key",
            ));
        }
        let item = &exprs[0];
        let field = field_from_expr(&item.expr)?;
        let dir = match item.asc {
            Some(true) | None => OrderDir::Asc,
            Some(false) => OrderDir::Desc,
        };
        Ok(Some((field, dir)))
    }

    fn limit_from(clause: Option<&sql_ast::Expr>) -> Result<Option<u32>> {
        let lim = match clause {
            Some(e) => e,
            None => return Ok(None),
        };
        match lim {
            sql_ast::Expr::Value(sql_ast::Value::Number(n, _)) => n
                .parse::<u32>()
                .map(Some)
                .map_err(|_| {
                    SemsqlError::validation(format!(
                        "LIMIT must be a non-negative integer, got `{n}`"
                    ))
                }),
            other => Err(SemsqlError::validation(format!(
                "LIMIT must be a literal, got {other}"
            ))),
        }
    }

    fn strip_quotes(s: &str) -> String {
        let s = s.trim();
        let bytes = s.as_bytes();
        if bytes.len() >= 2 {
            let first = bytes[0];
            let last = bytes[bytes.len() - 1];
            if (first == b'"' || first == b'`' || first == b'[') && first == last
                || (first == b'[' && last == b']')
            {
                return s[1..s.len() - 1].to_string();
            }
        }
        s.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_fetch() {
        let q = parse("SELECT * FROM users").unwrap();
        assert_eq!(q.entities.len(), 1);
        assert_eq!(q.entities[0].as_str(), "users");
        assert_eq!(q.select.len(), 1);
        assert!(matches!(q.select[0], SelectItem::Star));
        assert!(q.conditions.is_empty());
    }

    #[test]
    fn parses_filter_eq() {
        let q = parse("SELECT * FROM users WHERE users.status_code = 2").unwrap();
        assert_eq!(q.conditions.len(), 1);
        match &q.conditions[0] {
            Condition::Compare(_, Comparator::Eq, Value::Int(2)) => {}
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn parses_filter_param() {
        let q = parse("SELECT * FROM users WHERE users.name = :s").unwrap();
        match &q.conditions[0] {
            Condition::Compare(_, Comparator::Eq, Value::Param(name)) if name == "s" => {}
            other => panic!("unexpected: {other:?}"),
        }
    }

    #[test]
    fn parses_aggregate() {
        let q = parse("SELECT COUNT(*) FROM users").unwrap();
        assert!(matches!(q.select[0], SelectItem::Aggregate(Aggregate::Count, _)));
    }

    #[test]
    fn parses_order_limit() {
        let q = parse("SELECT * FROM users ORDER BY users.balance DESC LIMIT 10").unwrap();
        assert!(matches!(q.order_by, Some((_, OrderDir::Desc))));
        assert_eq!(q.limit, Some(10));
    }

    #[test]
    fn rejects_join() {
        let r = parse("SELECT * FROM users JOIN posts ON posts.author_id = users.id");
        assert!(r.is_err());
    }

    #[test]
    fn rejects_cte() {
        let r = parse("WITH x AS (SELECT 1) SELECT * FROM x");
        assert!(r.is_err());
    }

    #[test]
    fn rejects_union() {
        let r = parse("SELECT id FROM users UNION SELECT id FROM users");
        assert!(r.is_err());
    }

    #[test]
    fn rejects_having() {
        let r = parse(
            "SELECT users.status_code, COUNT(*) FROM users GROUP BY users.status_code HAVING COUNT(*) > 1",
        );
        assert!(r.is_err());
    }

    #[test]
    fn rejects_subquery_in_from() {
        let r = parse("SELECT * FROM (SELECT * FROM users) u");
        assert!(r.is_err());
    }

    #[test]
    fn rejects_dml() {
        let r = parse("DELETE FROM users");
        assert!(r.is_err());
    }
}
