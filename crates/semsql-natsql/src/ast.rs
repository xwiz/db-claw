//! NatSQL AST — output of Stage 3, input to the transpiler.

use semsql_core::{CanonicalName, EntityName, FieldName};
use serde::{Deserialize, Serialize};

/// A complete NatSQL query.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct NatSql {
    /// Items in the SELECT clause.
    pub select: Vec<SelectItem>,
    /// Entities involved (FROM is implicit in NatSQL — derived from refs).
    pub entities: Vec<EntityName>,
    /// WHERE conditions, joined by AND/OR.
    pub conditions: Vec<Condition>,
    /// GROUP BY (NatSQL form: optional).
    pub group_by: Vec<Field>,
    /// ORDER BY (single key for tiny-model scope; nested ORDERs are rare).
    pub order_by: Option<(Field, OrderDir)>,
    /// LIMIT.
    pub limit: Option<u32>,
}

/// A SELECT item.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum SelectItem {
    /// `*` — all columns.
    Star,
    /// A bare field.
    Field(Field),
    /// An aggregate over a field.
    Aggregate(Aggregate, Field),
}

/// Aggregation function — limited to the five NatSQL canonicals.
#[derive(Copy, Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum Aggregate {
    /// `COUNT(...)`.
    Count,
    /// `SUM(...)`.
    Sum,
    /// `AVG(...)`.
    Avg,
    /// `MIN(...)`.
    Min,
    /// `MAX(...)`.
    Max,
}

/// A field reference in the AST. May be a fully-resolved [`FieldName`] or a
/// bare canonical name when the entity is unambiguous from context.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum Field {
    /// `entity.field`.
    Qualified(FieldName),
    /// `field`.
    Bare(CanonicalName),
}

/// A WHERE clause leaf.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum Condition {
    /// `field <op> value`.
    Compare(Field, Comparator, Value),
    /// `field IN (v1, v2, …)`.
    In(Field, Vec<Value>),
    /// `field BETWEEN low AND high`.
    Between(Field, Value, Value),
    /// `field IS NULL`.
    IsNull(Field),
    /// `field IS NOT NULL`.
    IsNotNull(Field),
    /// `field LIKE pattern`.
    Like(Field, String),
}

/// Comparison operator.
#[derive(Copy, Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum Comparator {
    /// `=`.
    Eq,
    /// `!=` / `<>`.
    Ne,
    /// `<`.
    Lt,
    /// `<=`.
    Le,
    /// `>`.
    Gt,
    /// `>=`.
    Ge,
}

/// ORDER BY direction.
#[derive(Copy, Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub enum OrderDir {
    /// Ascending.
    Asc,
    /// Descending.
    Desc,
}

/// A literal value. Strings are stored without quoting; the transpiler
/// quotes per-dialect.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub enum Value {
    /// Integer.
    Int(i64),
    /// Floating point — exposed as f64; transpiler renders to literal.
    Float(f64),
    /// Bound parameter name (e.g. `:tenant`).
    Param(String),
    /// String literal (un-quoted).
    Str(String),
    /// Boolean literal.
    Bool(bool),
    /// SQL `NULL`.
    Null,
}

impl Eq for Value {}
