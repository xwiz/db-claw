//! Newtypes around canonical names.
//!
//! Every identifier flowing through SemanticSQL is one of these. Construction
//! enforces the allow-list (`[A-Za-z_][A-Za-z0-9_]{0,63}`) so a string that
//! made it past sanitisation cannot be mistaken for arbitrary text.

use crate::error::{Result, SemsqlError};
use serde::{Deserialize, Serialize};
use std::fmt;

const MAX_LEN: usize = 64;

/// Validates the allow-list rule for canonical names.
fn is_valid_canonical(s: &str) -> bool {
    if s.is_empty() || s.len() > MAX_LEN {
        return false;
    }
    let mut bytes = s.bytes();
    let first = bytes.next().unwrap();
    if !(first.is_ascii_alphabetic() || first == b'_') {
        return false;
    }
    bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// A canonical name — entity, field segment, enum, intent type. Always
/// matches `[A-Za-z_][A-Za-z0-9_]{0,63}`.
#[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct CanonicalName(String);

impl CanonicalName {
    /// Construct, returning `InvalidIdentifier` on failure. This is the only
    /// way a `CanonicalName` enters the system — sanitisation must run first.
    pub fn new(raw: impl Into<String>) -> Result<Self> {
        let raw = raw.into();
        if is_valid_canonical(&raw) {
            Ok(Self(raw))
        } else {
            Err(SemsqlError::InvalidIdentifier(raw))
        }
    }

    /// Borrow the inner string slice.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for CanonicalName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl AsRef<str> for CanonicalName {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

macro_rules! canonical_newtype {
    ($name:ident, $doc:literal) => {
        #[doc = $doc]
        #[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub CanonicalName);

        impl $name {
            /// Construct, validating the allow-list.
            pub fn new(raw: impl Into<String>) -> Result<Self> {
                CanonicalName::new(raw).map(Self)
            }
            /// Borrow the inner string slice.
            pub fn as_str(&self) -> &str {
                self.0.as_str()
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                self.0.fmt(f)
            }
        }
    };
}

canonical_newtype!(
    EntityName,
    "Entity-canonical name (matches DB table unless overridden)."
);
canonical_newtype!(EnumName, "Enum-canonical name, e.g. `users.status_code`.");
canonical_newtype!(
    IntentType,
    "Intent type key, matches an entry in `intent-library/patterns.yaml`."
);

/// A field reference: `entity.field`. Stored as two `CanonicalName`s rather
/// than a dotted string to keep validation airtight.
#[derive(Clone, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub struct FieldName {
    /// Owning entity.
    pub entity: EntityName,
    /// Field segment.
    pub field: CanonicalName,
}

impl FieldName {
    /// Construct from a dotted string `"users.created_at"`.
    pub fn parse_dotted(s: &str) -> Result<Self> {
        let (e, f) = s
            .split_once('.')
            .ok_or_else(|| SemsqlError::validation(format!("expected dotted name, got `{s}`")))?;
        Ok(Self {
            entity: EntityName::new(e)?,
            field: CanonicalName::new(f)?,
        })
    }
}

impl fmt::Display for FieldName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}", self.entity, self.field)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_valid_names() {
        assert!(CanonicalName::new("users").is_ok());
        assert!(CanonicalName::new("_private").is_ok());
        assert!(CanonicalName::new("col_42").is_ok());
        assert!(CanonicalName::new("a").is_ok());
    }

    #[test]
    fn rejects_invalid_names() {
        for bad in [
            "",
            "1users",
            "users-table",
            "active OR 1=1",
            "users; DROP",
            "users.col", // FieldName territory, not CanonicalName
            "users ",
            "very_long_name_that_exceeds_sixty_four_characters_threshold_xxxxxxxxxxx",
        ] {
            assert!(CanonicalName::new(bad).is_err(), "should reject {bad:?}");
        }
    }

    #[test]
    fn field_name_parses_dotted() {
        let fn_ = FieldName::parse_dotted("users.created_at").unwrap();
        assert_eq!(fn_.to_string(), "users.created_at");
    }

    #[test]
    fn field_name_rejects_garbage() {
        assert!(FieldName::parse_dotted("no_dot_here").is_err());
        assert!(FieldName::parse_dotted("users.bad-col").is_err());
        assert!(FieldName::parse_dotted("bad-table.col").is_err());
    }
}
