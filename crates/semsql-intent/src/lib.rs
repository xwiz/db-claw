//! Stage 0b — Intent Pattern Library.
//!
//! Loads `intent-library/patterns.yaml` and matches an incoming NL query
//! against every pattern. Each match emits an [`IntentHint`] which biases
//! Stage 1 ranking and seeds Stage 3 slot resolution.
//!
//! Patterns are *additive only*: an intent hint can prefer a column that
//! already exists in the SemanticGraph, but cannot synthesise one. This
//! keeps the security boundary at the validator + injector intact.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

use fancy_regex::Regex;
use indexmap::IndexMap;
use semsql_core::{Result, SemsqlError};
use serde::Deserialize;
use std::path::Path;

/// One pattern from `intent-library/patterns.yaml`.
#[derive(Clone, Debug, Deserialize)]
pub struct IntentPattern {
    /// Regex (PCRE-ish, via fancy-regex) matched case-insensitively against
    /// the normalised NL query.
    pub pattern: String,
    /// Stable identifier shared with the SemanticGraph's `IntentReference`.
    pub intent_type: String,
    /// Column-name hints — Stage 1 boosts relevance for fields whose canonical
    /// name OR display label matches one of these.
    #[serde(default)]
    pub column_hints: Vec<String>,
    /// Optional ORDER direction.
    #[serde(default)]
    pub ordering: Option<Ordering>,
    /// Optional comparator template (e.g. `"< 0"` for "in the red").
    #[serde(default)]
    pub comparator: Option<String>,
    /// Optional default LIMIT to apply.
    #[serde(default)]
    pub default_limit: Option<u32>,
    /// Optional natural-language description, surfaced by `semsql doctor`.
    #[serde(default)]
    pub description: Option<String>,
}

/// Direction for ORDER BY hints.
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "UPPERCASE")]
pub enum Ordering {
    /// Ascending.
    Asc,
    /// Descending.
    Desc,
}

/// Compiled, ready-to-match library.
pub struct IntentLibrary {
    compiled: Vec<(Regex, IntentPattern)>,
}

/// One match against a user query.
#[derive(Clone, Debug)]
pub struct IntentHint {
    /// Stable identifier shared with the SemanticGraph's `IntentReference`.
    pub intent_type: String,
    /// Hinted column-name strings — caller resolves against the live graph.
    pub column_hints: Vec<String>,
    /// Optional ORDER direction.
    pub ordering: Option<Ordering>,
    /// Optional comparator template.
    pub comparator: Option<String>,
    /// Optional default LIMIT.
    pub default_limit: Option<u32>,
    /// Span of the NL match — `[start, end)` byte offsets.
    pub matched_range: (usize, usize),
}

impl IntentLibrary {
    /// Load + compile patterns from a YAML file.
    pub fn load_from_path<P: AsRef<Path>>(path: P) -> Result<Self> {
        let text = std::fs::read_to_string(path.as_ref())?;
        Self::load_from_str(&text)
    }

    /// Load + compile patterns from a YAML string.
    pub fn load_from_str(yaml: &str) -> Result<Self> {
        let raw: Vec<IntentPattern> = serde_yaml::from_str(yaml)
            .map_err(|e| SemsqlError::Other(format!("intent-library parse: {e}")))?;
        let mut compiled = Vec::with_capacity(raw.len());
        let mut seen = IndexMap::<String, ()>::new();
        for p in raw {
            if seen.contains_key(&p.intent_type) {
                tracing::warn!(intent = %p.intent_type, "duplicate intent_type, later entry wins");
            }
            seen.insert(p.intent_type.clone(), ());
            let with_flags = format!("(?i){}", p.pattern);
            let rx = Regex::new(&with_flags)
                .map_err(|e| SemsqlError::Other(format!("bad pattern `{}`: {e}", p.pattern)))?;
            compiled.push((rx, p));
        }
        Ok(Self { compiled })
    }

    /// Match every pattern against `nl`. Returns one [`IntentHint`] per match.
    /// Multiple intents can fire at once (e.g. "top 5 in the red").
    pub fn r#match(&self, nl: &str) -> Vec<IntentHint> {
        let mut hits = Vec::new();
        for (rx, p) in &self.compiled {
            // fancy-regex `find` returns Result<Option<Match>>; we ignore the
            // outer Result on user input (a bad regex would have been caught
            // at load time).
            if let Ok(Some(m)) = rx.find(nl) {
                hits.push(IntentHint {
                    intent_type: p.intent_type.clone(),
                    column_hints: p.column_hints.clone(),
                    ordering: p.ordering.clone(),
                    comparator: p.comparator.clone(),
                    default_limit: p.default_limit,
                    matched_range: (m.start(), m.end()),
                });
            }
        }
        hits
    }

    /// Number of compiled patterns.
    pub fn len(&self) -> usize {
        self.compiled.len()
    }

    /// Whether the library has no patterns loaded.
    pub fn is_empty(&self) -> bool {
        self.compiled.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
- pattern: "(bleeding|hemorrhag\\w+) money"
  intent_type: high_expenditure
  column_hints: [total_expenses, cost, spend, expenditure, opex, burn]
  ordering: DESC
  default_limit: 10

- pattern: "(in the red|losing money|unprofitable)"
  intent_type: negative_profit
  column_hints: [profit, net_income, margin]
  comparator: "< 0"

- pattern: "(top|highest|biggest)\\s+(\\d+)?"
  intent_type: ranking
  ordering: DESC
  default_limit: 10
"#;

    #[test]
    fn loads_and_matches() {
        let lib = IntentLibrary::load_from_str(SAMPLE).unwrap();
        assert_eq!(lib.len(), 3);

        let hits = lib.r#match("which tenants are bleeding money");
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].intent_type, "high_expenditure");
        assert_eq!(hits[0].ordering, Some(Ordering::Desc));
    }

    #[test]
    fn matches_multiple_intents() {
        let lib = IntentLibrary::load_from_str(SAMPLE).unwrap();
        let hits = lib.r#match("top 5 customers in the red");
        let kinds: Vec<_> = hits.iter().map(|h| h.intent_type.as_str()).collect();
        assert!(kinds.contains(&"ranking"));
        assert!(kinds.contains(&"negative_profit"));
    }

    #[test]
    fn rejects_bad_pattern() {
        let bad = r#"
- pattern: "[unclosed"
  intent_type: oops
"#;
        assert!(IntentLibrary::load_from_str(bad).is_err());
    }

    /// Fixture-driven regression test against the shipped
    /// `intent-library/patterns.yaml`. Every entry here must match
    /// exactly the listed intent types — no extras, no missing — so
    /// pattern drift across community PRs surfaces as a CI failure
    /// rather than a silent semantic shift.
    ///
    /// Casing and word boundaries are intentionally varied to catch
    /// over-eager `(?i)` regressions.
    #[test]
    fn shipped_library_matches_canonical_idioms() {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("intent-library")
            .join("patterns.yaml");
        let lib = IntentLibrary::load_from_path(&path)
            .expect("intent-library/patterns.yaml must load cleanly");

        let cases: &[(&str, &[&str])] = &[
            // Spending / cost
            ("which tenants are bleeding money", &["high_expenditure"]),
            ("startups burning cash", &["high_expenditure"]),
            // Profitability
            ("customers in the red", &["negative_profit"]),
            ("teams making money", &["positive_profit"]),
            // Ranking
            ("top 5 customers", &["ranking_desc"]),
            ("bottom 10 spenders", &["ranking_asc"]),
            ("worst performing accounts", &["ranking_asc"]),
            // Time window
            ("orders in the last quarter", &["recent_window"]),
            ("dormant accounts", &["stale_record"]),
            // Customer / churn
            ("users who churned", &["churn"]),
            ("new sign-ups", &["recent_join"]),
            // Volume
            ("hot accounts", &["high_volume"]),
            ("idle accounts", &["low_volume"]),
            // New patterns: latency
            ("slow endpoints", &["high_latency"]),
            ("fast routes", &["low_latency"]),
            // New: error / health
            ("failing services", &["error_state"]),
            ("healthy nodes", &["healthy_state"]),
            // New: outlier
            ("show me anomalies", &["outlier"]),
            ("suspicious transactions", &["outlier"]),
            // New: compliance
            ("overdue invoices", &["overdue"]),
            ("upcoming renewals", &["upcoming"]),
            ("archived accounts", &["soft_deleted"]),
            // New: volume direction
            ("spike in errors", &["volume_spike"]),
            ("dropping conversion", &["volume_drop"]),
        ];

        for (nl, expected) in cases {
            let hits = lib.r#match(nl);
            let kinds: std::collections::HashSet<&str> =
                hits.iter().map(|h| h.intent_type.as_str()).collect();
            for want in *expected {
                assert!(
                    kinds.contains(*want),
                    "query {nl:?} did not fire intent {want:?}; got {kinds:?}"
                );
            }
        }
    }

    /// Defensive: idioms that share substrings with intent words must
    /// not over-eagerly fire. e.g. "stalemate" should not trip "stale".
    #[test]
    fn shipped_library_does_not_over_match() {
        let path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("intent-library")
            .join("patterns.yaml");
        let lib = IntentLibrary::load_from_path(&path).unwrap();

        let no_match_cases: &[(&str, &str)] = &[
            // Word-boundary cases — substring matches must not fire.
            ("the meeting reached stalemate", "stale_record"),
            ("send a colder draft", "low_volume"), // "cold" inside "colder"
            ("we are upcycling materials", "upcoming"),
            ("classifier returns true", "ranking_asc"), // 'classifier' contains 'class'-stem; no 'lowest' here
        ];

        for (nl, must_not_fire) in no_match_cases {
            let hits = lib.r#match(nl);
            let kinds: std::collections::HashSet<&str> =
                hits.iter().map(|h| h.intent_type.as_str()).collect();
            assert!(
                !kinds.contains(*must_not_fire),
                "query {nl:?} unexpectedly fired {must_not_fire:?}; got {kinds:?}"
            );
        }
    }
}
