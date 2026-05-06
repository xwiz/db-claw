//! Stage 0a — Vocabulary Pre-resolver.
//!
//! Deterministic. <1 ms. Reads the SemanticGraph's vocabulary, plural
//! labels, and enum values; tries to resolve the NL into a NatSQL string
//! without touching any model.
//!
//! Patterns handled at v0.2:
//!
//! 1. **Bare entity** — `"students"` → `SELECT * FROM users`.
//! 2. **Verb + entity** — `"show students"` → `SELECT * FROM users`.
//! 3. **Count entity** — `"count students"` / `"how many students"` →
//!    `SELECT COUNT(*) FROM users`.
//! 4. **Enum + entity** — `"active students"` →
//!    `SELECT * FROM users WHERE users.status_code = 2`.
//!
//! These four patterns alone account for ~30–40 % of real queries we've
//! seen in pilot Filament apps. Anything more complex falls through to
//! the model stages (currently unimplemented in v0.2 — the cascade
//! returns a clarification request instead of guessing).

use crate::normalize;
use ahash::{AHashMap, AHashSet};
use semsql_core::Result;
use semsql_graph::read::{enums, plural_label_index, vocabulary};
use std::path::Path;

/// What Stage 0a returns to the orchestrator.
#[derive(Clone, Debug, PartialEq)]
pub enum PreResolveOutcome {
    /// Confidence ≥ threshold. Caller should jump straight to Stage 4
    /// (NatSQL → SQL) and skip the model stages.
    Resolved {
        /// NatSQL string produced deterministically.
        natsql: String,
        /// Per-token confidence — minimum across resolved spans.
        confidence: f32,
    },
    /// Resolver couldn't pin every token; the model stages must run.
    NeedsModel,
}

/// Snapshot of the SemanticGraph data Stage 0a depends on.
///
/// Loaded once per graph file; cheap to keep around for the runtime's
/// lifetime. The orchestrator calls [`PreResolverIndex::load`] at start-up
/// and then re-uses the index per query.
///
/// `plural_labels` is a multi-map: when a term resolves to two or more
/// canonical entities, the resolver treats the term as **ambiguous** and
/// falls through to the model stages instead of silently picking a winner
/// — which would be a deterministic-but-wrong answer in a multi-tenant
/// codebase.
#[derive(Clone, Debug)]
pub struct PreResolverIndex {
    /// `entity_label_lower → [entity_canonical_name, …]`. Length > 1
    /// flags ambiguity and disqualifies Stage 0a from resolving the term.
    pub plural_labels: AHashMap<String, Vec<String>>,
    /// `enum_label_lower → (entity_canonical, field_canonical, raw_value)`.
    pub enum_labels: AHashMap<String, (String, String, String)>,
    /// Set of canonical entity names — used for direct-match short-circuit.
    pub entity_canonicals: AHashSet<String>,
}

impl PreResolverIndex {
    /// Load the index from a `.semsql` file.
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let mut plural_labels: AHashMap<String, Vec<String>> = plural_label_index(path)?
            .into_iter()
            .collect();

        // Build enum-label → (entity, field, raw_value) by joining the enum
        // map with the vocabulary table. Enum canonical names follow the
        // convention `entity.field` (e.g. `users.status_code`).
        let mut enum_labels: AHashMap<String, (String, String, String)> = AHashMap::new();
        for e in enums(path)? {
            let (entity, field) = match e.canonical_name.split_once('.') {
                Some(p) => p,
                None => continue,
            };
            for (raw, label) in &e.values {
                enum_labels.insert(
                    label.to_lowercase(),
                    (entity.to_string(), field.to_string(), raw.clone()),
                );
            }
        }

        // Vocabulary table also carries entity / field aliases — fold them in
        // for the plural-label index. Only `entity` and `enum_value` kinds
        // contribute here; field-level aliases land in v0.5 alongside the
        // multi-entity templates.
        for v in vocabulary(path)? {
            match v.canonical_kind.as_str() {
                "entity" => {
                    let entry = plural_labels.entry(v.term.clone()).or_default();
                    if !entry.iter().any(|c| c == &v.canonical_value) {
                        entry.push(v.canonical_value.clone());
                    }
                }
                "enum_value" => {
                    if let Some((canonical, raw)) = v.canonical_value.split_once(':') {
                        if let Some((entity, field)) = canonical.split_once('.') {
                            enum_labels.insert(
                                v.term.clone(),
                                (entity.to_string(), field.to_string(), raw.to_string()),
                            );
                        }
                    }
                }
                _ => {}
            }
        }

        let entity_canonicals: AHashSet<String> = plural_labels
            .values()
            .flat_map(|v| v.iter().cloned())
            .collect();

        Ok(Self {
            plural_labels,
            enum_labels,
            entity_canonicals,
        })
    }
}

/// Run Stage 0a against `nl` using `index`.
pub fn resolve(nl: &str, index: &PreResolverIndex) -> PreResolveOutcome {
    let normalised = normalize::normalize(nl);
    let tokens: Vec<&str> = normalised.split_whitespace().collect();
    if tokens.is_empty() {
        return PreResolveOutcome::NeedsModel;
    }

    // Try the longest-substring match first — n-grams up to 3 words.
    // Greedy: as soon as a span resolves to an entity, anchor on it.
    let entity_match = match find_entity(&tokens, index) {
        Some(m) => m,
        None => return PreResolveOutcome::NeedsModel,
    };

    let leading: Vec<&str> = tokens[..entity_match.start].to_vec();
    let trailing: Vec<&str> = tokens[entity_match.end..].to_vec();

    // Pattern: count of entity — `"count <entity>"` or `"how many <entity>"`.
    if matches!(leading.as_slice(), ["count"] | ["how", "many"]) && trailing.is_empty() {
        return PreResolveOutcome::Resolved {
            natsql: format!("SELECT COUNT(*) FROM {}", entity_match.canonical),
            confidence: 1.0,
        };
    }

    // Pattern: enum + entity — `"active students"`. The enum label is the
    // span just before the entity; we accept a single optional verb prefix.
    if let Some(stripped) = strip_optional_verb(&leading) {
        if let Some(label) = stripped.last() {
            if let Some((entity, field, raw)) = index.enum_labels.get(label as &str) {
                if entity == &entity_match.canonical {
                    let lit = if raw.parse::<i64>().is_ok() || raw.parse::<f64>().is_ok() {
                        raw.clone()
                    } else {
                        format!("'{}'", raw.replace('\'', "''"))
                    };
                    return PreResolveOutcome::Resolved {
                        natsql: format!(
                            "SELECT * FROM {} WHERE {}.{} = {}",
                            entity_match.canonical, entity_match.canonical, field, lit
                        ),
                        confidence: 1.0,
                    };
                }
            }
        }
    }

    // Pattern: verb + entity — `"show students"`, or bare entity.
    if leading.iter().all(|t| is_fetch_verb(t)) && trailing.is_empty() {
        return PreResolveOutcome::Resolved {
            natsql: format!("SELECT * FROM {}", entity_match.canonical),
            confidence: 1.0,
        };
    }

    PreResolveOutcome::NeedsModel
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

struct EntityMatch {
    canonical: String,
    start: usize,
    end: usize,
}

fn find_entity(tokens: &[&str], index: &PreResolverIndex) -> Option<EntityMatch> {
    // Try 3-, 2-, 1-token spans, longest first, scanning right-to-left so
    // queries like "count students" anchor on the entity at the tail.
    //
    // Ambiguity policy: a span that resolves to >1 distinct canonical
    // entities is treated as **unresolved**. The cascade then escalates to
    // the model stages rather than picking a deterministic-but-wrong
    // winner. This is the safe behaviour in multi-tenant codebases where
    // two resources can legitimately share a label.
    for span in (1..=3.min(tokens.len())).rev() {
        for start in (0..=tokens.len() - span).rev() {
            let phrase = tokens[start..start + span].join(" ");
            if let Some(canonicals) = index.plural_labels.get(&phrase) {
                if canonicals.len() == 1 {
                    return Some(EntityMatch {
                        canonical: canonicals[0].clone(),
                        start,
                        end: start + span,
                    });
                }
                // Ambiguous: bail out of the resolver entirely so the
                // caller falls through to the model stages.
                return None;
            }
        }
    }
    None
}

fn is_fetch_verb(token: &str) -> bool {
    matches!(
        token,
        "show"
            | "list"
            | "get"
            | "find"
            | "display"
            | "fetch"
            | "return"
            | "give"
            | "pull"
            | "all"
    )
}

fn strip_optional_verb<'a>(leading: &'a [&'a str]) -> Option<Vec<&'a str>> {
    if leading.is_empty() {
        return Some(Vec::new());
    }
    if is_fetch_verb(leading[0]) {
        return Some(leading[1..].to_vec());
    }
    Some(leading.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;
    use ahash::AHashMap;

    fn fixture_index() -> PreResolverIndex {
        let mut plural: AHashMap<String, Vec<String>> = AHashMap::new();
        plural.insert("students".to_string(), vec!["users".to_string()]);
        plural.insert("user".to_string(), vec!["users".to_string()]);
        plural.insert("organizations".to_string(), vec!["tenants".to_string()]);

        let mut enum_labels = AHashMap::new();
        enum_labels.insert(
            "active".to_string(),
            ("users".to_string(), "status_code".to_string(), "2".to_string()),
        );
        enum_labels.insert(
            "pending".to_string(),
            ("users".to_string(), "status_code".to_string(), "1".to_string()),
        );

        let entity_canonicals: AHashSet<_> =
            ["users", "tenants"].iter().map(|s| s.to_string()).collect();

        PreResolverIndex {
            plural_labels: plural,
            enum_labels,
            entity_canonicals,
        }
    }

    #[test]
    fn resolves_bare_entity() {
        let idx = fixture_index();
        match resolve("students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(natsql, "SELECT * FROM users");
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_show_entity() {
        let idx = fixture_index();
        match resolve("show students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(natsql, "SELECT * FROM users");
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_count() {
        let idx = fixture_index();
        match resolve("count students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(natsql, "SELECT COUNT(*) FROM users");
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_how_many() {
        let idx = fixture_index();
        match resolve("how many students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(natsql, "SELECT COUNT(*) FROM users");
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_enum_subject() {
        let idx = fixture_index();
        match resolve("active students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT * FROM users WHERE users.status_code = 2"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_show_active_students() {
        let idx = fixture_index();
        match resolve("show active students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT * FROM users WHERE users.status_code = 2"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn falls_through_when_unknown() {
        let idx = fixture_index();
        assert_eq!(resolve("show ferrets", &idx), PreResolveOutcome::NeedsModel);
    }

    #[test]
    fn falls_through_on_complex_filter() {
        let idx = fixture_index();
        // Operator + value — out of scope for v0.2 pre-resolver.
        assert_eq!(
            resolve("students with balance over 100", &idx),
            PreResolveOutcome::NeedsModel
        );
    }

    #[test]
    fn ambiguous_label_falls_through_to_model_stages() {
        // Two distinct entities both labelled "Students" — the resolver
        // must NOT pick a winner. Picking deterministically here would be
        // a multi-tenant correctness bug (see read.rs::plural_label_index).
        let mut plural: AHashMap<String, Vec<String>> = AHashMap::new();
        plural.insert(
            "students".to_string(),
            vec!["users".to_string(), "archived_users".to_string()],
        );
        let idx = PreResolverIndex {
            plural_labels: plural,
            enum_labels: AHashMap::new(),
            entity_canonicals: ["users", "archived_users"]
                .iter()
                .map(|s| s.to_string())
                .collect(),
        };
        assert_eq!(resolve("students", &idx), PreResolveOutcome::NeedsModel);
        assert_eq!(
            resolve("show students", &idx),
            PreResolveOutcome::NeedsModel
        );
    }
}
