//! NatSQL AST — output of Stage 3, input to the transpiler.
//!
//! v0.3 additions over v0.2:
//! - `joins`: up to 3 INNER JOIN clauses with ON field = field.
//! - `having`: HAVING conditions (same structure as WHERE).
//! - `SelectItem::Expr`: raw SQL expression for arithmetic / CAST that the
//!   typed AST does not model in detail.

use semsql_core::{CanonicalName, EntityName, FieldName};
use serde::{Deserialize, Serialize};

/// A complete NatSQL query.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct NatSql {
    /// Items in the SELECT clause.
    pub select: Vec<SelectItem>,
    /// Primary FROM entity (first entry) plus any joined entities.
    pub entities: Vec<EntityName>,
    /// INNER JOIN clauses — v0.3 supports up to 3 chains.
    #[serde(default)]
    pub joins: Vec<JoinClause>,
    /// WHERE conditions, joined by AND.
    pub conditions: Vec<Condition>,
    /// HAVING conditions, joined by AND (applied after GROUP BY aggregation).
    #[serde(default)]
    pub having: Vec<Condition>,
    /// GROUP BY (NatSQL form: optional).
    pub group_by: Vec<Field>,
    /// ORDER BY (single key for tiny-model scope; nested ORDERs are rare).
    pub order_by: Option<(Field, OrderDir)>,
    /// LIMIT.
    pub limit: Option<u32>,
    /// OFFSET (0 == omit).
    #[serde(default)]
    pub offset: Option<u32>,
}

/// One INNER JOIN in a v0.3 NatSQL query.
///
/// Represents `INNER JOIN {entity} ON {on_left} = {on_right}`. The
/// transpiler renders these verbatim; the `on_left` and `on_right` fields
/// must be fully-qualified (`entity.field`) to avoid ambiguity.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct JoinClause {
    /// The joined entity (table).
    pub entity: EntityName,
    /// Left side of the ON condition.
    pub on_left: Field,
    /// Right side of the ON condition.
    pub on_right: Field,
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
    /// An aggregate over a field with a SELECT alias.
    AliasedAggregate(Aggregate, Field, CanonicalName),
    /// A raw SQL expression for arithmetic, CAST, or other complex constructs
    /// that the tiny-model cascade can generate but the typed AST does not
    /// model in detail (e.g. `CAST(t.col AS REAL) / t.total`).
    /// The transpiler emits this verbatim; the cascade's column-name
    /// rewriter handles identifier substitution on the final SQL string.
    Expr(String),
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
