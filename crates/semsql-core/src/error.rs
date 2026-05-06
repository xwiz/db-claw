//! Single error type re-exported by every SemanticSQL crate.

use thiserror::Error;

/// Crate-wide result alias.
pub type Result<T, E = SemsqlError> = std::result::Result<T, E>;

/// Every error emitted by any SemanticSQL crate.
///
/// Variants are added additively. Avoid breaking changes once a variant is
/// public — pin the discriminant by writing `#[error(...)]` on each.
#[derive(Debug, Error)]
pub enum SemsqlError {
    /// A vocabulary fragment failed sanitisation and was rejected.
    #[error("invalid identifier `{0}` (failed allow-list)")]
    InvalidIdentifier(String),

    /// A canonical reference points at a name that does not exist in the graph.
    #[error("unknown canonical name `{0}`")]
    UnknownCanonical(String),

    /// The SemanticGraph schema version is newer than this binary supports.
    #[error("graph schema version {found} is newer than supported version {supported}")]
    SchemaVersionMismatch {
        /// Schema version found in the graph file.
        found: u32,
        /// Highest schema version this build understands.
        supported: u32,
    },

    /// A SQL-level invariant the validator depends on was violated.
    #[error("validation: {0}")]
    Validation(String),

    /// The generated SQL would touch an entity without applying its mandatory
    /// scope predicates. **Security-critical** — never silently ignore.
    #[error("scope leak: entity `{entity}` referenced without mandatory scope")]
    ScopeLeak {
        /// Canonical entity name that was referenced unscoped.
        entity: String,
    },

    /// Two parsers disagreed on the post-rewrite SQL — fail closed.
    #[error("parser disagreement: {detail}")]
    ParserDisagreement {
        /// Human-readable explanation of the disagreement.
        detail: String,
    },

    /// A wrapped I/O error.
    #[error(transparent)]
    Io(#[from] std::io::Error),

    /// A wrapped protobuf decode error.
    #[error(transparent)]
    ProtoDecode(#[from] prost::DecodeError),

    /// A wrapped protobuf encode error.
    #[error(transparent)]
    ProtoEncode(#[from] prost::EncodeError),

    /// Catch-all for non-recoverable failures with context.
    #[error("{0}")]
    Other(String),
}

impl SemsqlError {
    /// Convenience constructor for ad-hoc validation failures.
    pub fn validation(msg: impl Into<String>) -> Self {
        SemsqlError::Validation(msg.into())
    }

    /// Convenience constructor for scope leaks.
    pub fn scope_leak(entity: impl Into<String>) -> Self {
        SemsqlError::ScopeLeak {
            entity: entity.into(),
        }
    }
}
