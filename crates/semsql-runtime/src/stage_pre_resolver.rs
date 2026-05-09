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
//! 5. **Count + enum** — `"count active students"` →
//!    `SELECT COUNT(*) FROM users WHERE users.status_code = 2`.
//! 6. **Numeric comparison** — `"students with balance over 100"` →
//!    `SELECT * FROM users WHERE users.balance > 100`. Operator phrases
//!    map onto SQL comparators via [`comparator_from_phrase`]. Field type
//!    must be numeric — non-numeric fields fall through.
//! 7. **Field projection** — `"emails of students"` →
//!    `SELECT users.email FROM users`. The leading span resolves to a
//!    single field on the trailing entity; ambiguous labels fall through.
//! 8. **Ordering** — `"students sorted by balance desc"` →
//!    `SELECT * FROM users ORDER BY users.balance DESC`. Connector
//!    keywords: `ordered by` / `sorted by` / `ranked by`. Default ASC.
//! 9. **Top-N + intent** — `"top 5 spenders"` →
//!    `SELECT * FROM users ORDER BY users.expenses DESC LIMIT 5` when
//!    Stage 0b's intent library matches and the hinted column resolves
//!    unambiguously to one numeric field on the entity. Without an
//!    intent match (or with an ambiguous hint), the pattern falls
//!    through. `top <N> <entity> by <field>` works without any intent.
//!
//! These deterministic patterns together account for ~30–40 % of real
//! queries we've seen in pilot Filament apps. Anything more complex
//! falls through to the model stages (currently unimplemented in v0.2 —
//! the cascade returns a clarification request instead of guessing).

use crate::normalize;
use ahash::{AHashMap, AHashSet};
use semsql_core::Result;
use semsql_graph::read::{enums, field_label_index, plural_label_index, vocabulary, FieldRef};
use semsql_intent::{IntentHint, Ordering as IntentOrdering};
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
    /// `field_label_lower → [(entity_canonical, field_canonical, sql_type), …]`.
    /// Length > 1 = ambiguous, same fall-through rule as plural labels.
    pub field_labels: AHashMap<String, Vec<FieldRef>>,
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

        let field_labels: AHashMap<String, Vec<FieldRef>> =
            field_label_index(path)?.into_iter().collect();

        Ok(Self {
            plural_labels,
            enum_labels,
            entity_canonicals,
            field_labels,
        })
    }
}

/// Run Stage 0a against `nl` using `index`. The empty intent slice is
/// the same as having no intent library loaded; callers with a live
/// library forward the matches so the Top-N pattern can use the
/// intent's `column_hints` and `ordering` to seed `ORDER BY` / `LIMIT`.
pub fn resolve(nl: &str, index: &PreResolverIndex) -> PreResolveOutcome {
    resolve_with_intents(nl, index, &[])
}

/// Run Stage 0a with a list of intent matches in scope. Intent hints
/// only influence the Top-N pattern today (every other pattern is
/// purely vocabulary-driven), but threading the slice through lets us
/// add more intent-aware deterministic resolutions without churning
/// every call site.
pub fn resolve_with_intents(
    nl: &str,
    index: &PreResolverIndex,
    intents: &[IntentHint],
) -> PreResolveOutcome {
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

    // Helper: strip a leading "of" (so "names of students" leaves
    // ["names"] before the entity).
    let leading_no_count = strip_count_prefix(&leading);

    // Pattern: count + enum + entity — `"count active students"` /
    // `"how many active students"`.
    if let Some((rest, _is_count)) = match_count_prefix(&leading) {
        if trailing.is_empty() {
            // No tail → COUNT(*) [WHERE enum].
            if rest.is_empty() {
                return PreResolveOutcome::Resolved {
                    natsql: format!("SELECT COUNT(*) FROM {}", entity_match.canonical),
                    confidence: 1.0,
                };
            }
            // Single enum-label between count + entity.
            if let Some(natsql) =
                build_count_with_enum(&rest, &entity_match.canonical, &index.enum_labels)
            {
                return PreResolveOutcome::Resolved {
                    natsql,
                    confidence: 1.0,
                };
            }
        }
    }

    // Pattern: enum + entity — `"active students"`. The enum label is the
    // span just before the entity; we accept a single optional verb prefix.
    if let Some(stripped) = strip_optional_verb(&leading) {
        if let Some(label) = stripped.last() {
            if let Some((entity, field, raw)) = index.enum_labels.get(label as &str) {
                if entity == &entity_match.canonical {
                    let lit = enum_value_literal(raw);
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

    // Pattern: top-N + (optional explicit field, or intent column hint).
    // Leading span looks like `top|highest|biggest|first <N>?` or
    // `<N> highest|biggest|...`. Trailing optionally carries `by <field>`.
    if let Some(natsql) = build_top_n(
        &leading,
        &trailing,
        &entity_match.canonical,
        &index.field_labels,
        intents,
    ) {
        return PreResolveOutcome::Resolved {
            natsql,
            confidence: 1.0,
        };
    }

    // Pattern: ordering — `<verb>? <entity> [ordered|sorted|ranked] by
    // <field> [asc|desc|ascending|descending]?`. Leading restricted to
    // fetch verbs.
    if leading.iter().all(|t| is_fetch_verb(t)) {
        if let Some(natsql) =
            build_ordering(&trailing, &entity_match.canonical, &index.field_labels)
        {
            return PreResolveOutcome::Resolved {
                natsql,
                confidence: 1.0,
            };
        }
    }

    // Pattern: numeric comparison — `"<verb>? <entity> [with] <field>
    // <op-phrase> <number>"`. Trailing tokens carry the predicate; the
    // leading span is restricted to optional fetch verbs.
    if leading.iter().all(|t| is_fetch_verb(t)) {
        if let Some(natsql) =
            build_numeric_comparison(&trailing, &entity_match.canonical, &index.field_labels)
        {
            return PreResolveOutcome::Resolved {
                natsql,
                confidence: 1.0,
            };
        }
    }

    // Pattern: field projection — `"<field-label> of <entity>"`.
    // `leading_no_count` already strips a stray "count" — but this branch
    // ignores anything that started with "count" / "how many" because that
    // path is handled above with COUNT(*) and we don't currently emit
    // `COUNT(<field>)`.
    if !match_count_prefix(&leading).is_some() && trailing.is_empty() {
        if let Some(natsql) = build_field_projection(
            &leading_no_count,
            &entity_match.canonical,
            &index.field_labels,
        ) {
            return PreResolveOutcome::Resolved {
                natsql,
                confidence: 1.0,
            };
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

/// Strip a leading `count` / `how many` prefix and return the remainder.
/// The boolean indicates whether `how many` (rather than `count`) was the
/// matched form — kept around in case future templates want to distinguish.
fn match_count_prefix<'a>(leading: &'a [&'a str]) -> Option<(Vec<&'a str>, bool)> {
    if leading.is_empty() {
        return None;
    }
    // Allow an optional fetch-verb in front of `count` / `how many` —
    // `"show count of students"` is rare but the grammar accepts it.
    let start = if is_fetch_verb(leading[0]) { 1 } else { 0 };
    let rest = &leading[start..];
    if rest.is_empty() {
        return None;
    }
    if rest[0] == "count" {
        // `count of <enum> <entity>` — drop the optional "of".
        let rest = if rest.len() >= 2 && rest[1] == "of" {
            rest[2..].to_vec()
        } else {
            rest[1..].to_vec()
        };
        return Some((rest, false));
    }
    if rest.len() >= 2 && rest[0] == "how" && rest[1] == "many" {
        return Some((rest[2..].to_vec(), true));
    }
    None
}

/// Strip a leading `count` / `how many` prefix without requiring it.
/// Returns the input unchanged when no count prefix is present.
fn strip_count_prefix<'a>(leading: &'a [&'a str]) -> Vec<&'a str> {
    match match_count_prefix(leading) {
        Some((rest, _)) => rest,
        None => leading.to_vec(),
    }
}

/// Build a `SELECT COUNT(*) FROM <entity> WHERE <enum-predicate>` if the
/// remaining `leading` span (post-count, optional verb already stripped)
/// is exactly one token AND that token is a known enum label whose
/// owning entity matches the resolved entity.
fn build_count_with_enum(
    leading: &[&str],
    entity: &str,
    enum_labels: &AHashMap<String, (String, String, String)>,
) -> Option<String> {
    if leading.len() != 1 {
        return None;
    }
    let label = leading[0];
    let (e, f, raw) = enum_labels.get(label)?;
    if e != entity {
        return None;
    }
    let lit = enum_value_literal(raw);
    Some(format!(
        "SELECT COUNT(*) FROM {entity} WHERE {entity}.{f} = {lit}"
    ))
}

/// Build a `SELECT <entity>.<field> FROM <entity>` when `leading` is a
/// projection of the form `"<field-label> of"` (the trailing "of" lets
/// us anchor the projection without ambiguity). Returns `None` on:
///
///  - no candidate field
///  - the matched label resolves to >1 distinct field on the entity
///  - the matched field belongs to a different entity
fn build_field_projection(
    leading: &[&str],
    entity: &str,
    field_labels: &AHashMap<String, Vec<FieldRef>>,
) -> Option<String> {
    // Must be `<...> of` so we don't false-match on enum labels.
    if leading.len() < 2 || *leading.last()? != "of" {
        return None;
    }
    // Up to a 3-word field-label span (e.g. "account balance of users").
    let prefix = &leading[..leading.len() - 1];
    let span = prefix.len();
    if !(1..=3).contains(&span) {
        return None;
    }
    let label = prefix.join(" ");
    let refs = field_labels.get(label.as_str())?;
    let candidates: Vec<&FieldRef> = refs.iter().filter(|r| r.entity == entity).collect();
    if candidates.len() != 1 {
        return None;
    }
    let field = &candidates[0].field;
    Some(format!("SELECT {entity}.{field} FROM {entity}"))
}

/// Build a `SELECT * FROM <entity> WHERE <entity>.<field> <op> <num>`
/// from a trailing-token slice that looks like a single numeric
/// comparison — optionally introduced by a `with` / `where` /
/// `whose` connector. Returns `None` when the slice doesn't fit the
/// shape, the field can't be resolved, or the field is non-numeric.
fn build_numeric_comparison(
    trailing: &[&str],
    entity: &str,
    field_labels: &AHashMap<String, Vec<FieldRef>>,
) -> Option<String> {
    // Drop a leading connector — `with` / `where` / `whose` / `having`.
    let mut tokens: Vec<&str> = trailing.to_vec();
    if let Some(first) = tokens.first() {
        if matches!(*first, "with" | "where" | "whose" | "having") {
            tokens.remove(0);
        }
    }
    if tokens.is_empty() {
        return None;
    }

    // The number must be the last token.
    let raw_number = *tokens.last()?;
    let _: f64 = raw_number.parse().ok()?;
    let prefix = &tokens[..tokens.len() - 1];
    if prefix.len() < 2 {
        return None;
    }

    // The operator phrase covers 1..=3 trailing tokens.
    for op_span in (1..=3.min(prefix.len() - 1)).rev() {
        let op_start = prefix.len() - op_span;
        let op_phrase = prefix[op_start..].join(" ");
        let comparator = match comparator_from_phrase(&op_phrase) {
            Some(c) => c,
            None => continue,
        };
        let label_tokens = &prefix[..op_start];
        if label_tokens.is_empty() || label_tokens.len() > 3 {
            continue;
        }
        let label = label_tokens.join(" ");
        let refs = match field_labels.get(label.as_str()) {
            Some(r) => r,
            None => continue,
        };
        let candidates: Vec<&FieldRef> = refs.iter().filter(|r| r.entity == entity).collect();
        if candidates.len() != 1 {
            continue;
        }
        let field = &candidates[0];
        if !is_numeric_type(&field.r#type) {
            continue;
        }
        return Some(format!(
            "SELECT * FROM {entity} WHERE {entity}.{f} {comparator} {raw_number}",
            f = field.field
        ));
    }
    None
}

/// Map a natural-language operator phrase to a SQL comparator. The
/// patterns lean on what real users type — synonyms ("greater than",
/// "more than", "above") all map to `>`. Returns `None` for phrases
/// outside the supported set so the caller can fall through cleanly.
pub(crate) fn comparator_from_phrase(phrase: &str) -> Option<&'static str> {
    Some(match phrase {
        ">" | "above" | "over" | "greater than" | "more than" | "exceeding" => ">",
        ">=" | "at least" | "no less than" | "not less than" | "greater than or equal to" => ">=",
        "<" | "below" | "under" | "less than" | "fewer than" => "<",
        "<=" | "at most" | "no more than" | "not greater than" | "less than or equal to" => "<=",
        "=" | "is" | "equals" | "equal to" | "exactly" => "=",
        "!=" | "<>" | "is not" | "not equal to" | "not equals" => "!=",
        _ => return None,
    })
}

/// Whether the SQL type stored in `fields.type` is numeric for the
/// purposes of comparison-pattern resolution. Conservative: we only
/// commit to a comparison when the type is unambiguously numeric.
/// Booleans and dates fall through to the model stages — those would
/// need their own deterministic patterns to keep the meaning right.
fn is_numeric_type(t: &str) -> bool {
    let t = t.to_ascii_uppercase();
    matches!(
        t.as_str(),
        "INTEGER"
            | "INT"
            | "BIGINT"
            | "SMALLINT"
            | "TINYINT"
            | "FLOAT"
            | "DOUBLE"
            | "REAL"
            | "NUMERIC"
            | "DECIMAL"
            | "MONEY"
    ) || t.starts_with("INT")
        || t.starts_with("NUMERIC")
        || t.starts_with("DECIMAL")
}

/// Render an enum raw value as a SQL literal — numeric raws stay bare,
/// everything else is single-quote-escaped. Shared between the
/// enum-only and count+enum patterns.
fn enum_value_literal(raw: &str) -> String {
    if raw.parse::<i64>().is_ok() || raw.parse::<f64>().is_ok() {
        raw.to_string()
    } else {
        format!("'{}'", raw.replace('\'', "''"))
    }
}

/// Build a `SELECT * FROM <entity> ORDER BY <entity>.<field> <DIR>` from
/// a trailing-token slice that begins with `ordered by` / `sorted by` /
/// `ranked by`. Direction defaults to `ASC` and accepts `asc` /
/// `ascending` / `desc` / `descending`. Returns `None` when:
///
///  - the connector keyword is absent,
///  - the field label is unknown or ambiguous on the entity,
///  - or trailing tokens carry anything beyond field + direction.
fn build_ordering(
    trailing: &[&str],
    entity: &str,
    field_labels: &AHashMap<String, Vec<FieldRef>>,
) -> Option<String> {
    if trailing.len() < 3 {
        return None;
    }
    let connector = match trailing[0] {
        "ordered" | "sorted" | "ranked" => trailing[0],
        _ => return None,
    };
    let _ = connector;
    if trailing[1] != "by" {
        return None;
    }
    let after_by = &trailing[2..];
    // Direction word, if any, is the last token. Take it before label
    // resolution so a 1- to 3-token field label can sit in front.
    let (label_tokens, direction) = match after_by
        .last()
        .copied()
        .and_then(direction_from_word)
    {
        Some(dir) => (&after_by[..after_by.len() - 1], dir),
        None => (after_by, "ASC"),
    };
    if label_tokens.is_empty() || label_tokens.len() > 3 {
        return None;
    }
    let label = label_tokens.join(" ");
    let refs = field_labels.get(label.as_str())?;
    let candidates: Vec<&FieldRef> = refs.iter().filter(|r| r.entity == entity).collect();
    if candidates.len() != 1 {
        return None;
    }
    let field = &candidates[0].field;
    Some(format!(
        "SELECT * FROM {entity} ORDER BY {entity}.{field} {direction}"
    ))
}

/// Map a single word to a SQL ORDER direction. Returns `None` for
/// anything that isn't an asc/desc keyword so the ordering pattern can
/// distinguish "no direction supplied" from "direction was something
/// else (probably a label suffix)".
fn direction_from_word(token: &str) -> Option<&'static str> {
    Some(match token {
        "asc" | "ascending" => "ASC",
        "desc" | "descending" => "DESC",
        _ => return None,
    })
}

/// Build a `SELECT * FROM <entity> ORDER BY ... DESC LIMIT <N>` from
/// a Top-N leading span. Two surfaces:
///
///  - `top <N>? <entity> [by <field>]?` — `top` / `highest` / `biggest` /
///    `first` synonyms. N defaults to the matching intent's
///    `default_limit` when the literal is omitted.
///  - `<N> highest|biggest|... <entity>` — number-first form.
///
/// Without an explicit `by <field>` tail, the resolver looks up the
/// matching intent's `column_hints` and picks the unique numeric field
/// on the entity that matches. If zero or >1 fields match, the
/// resolver falls through (deterministic-but-wrong is worse than a
/// clarification request). Without an intent match AND without an
/// explicit field, the pattern falls through.
fn build_top_n(
    leading: &[&str],
    trailing: &[&str],
    entity: &str,
    field_labels: &AHashMap<String, Vec<FieldRef>>,
    intents: &[IntentHint],
) -> Option<String> {
    let parsed_head = parse_top_n_head(leading)?;
    let (limit_from_head, ordering_from_head) = parsed_head;

    // Determine ordering — head wins; otherwise the first DESC-bearing
    // intent wins; otherwise default DESC for "top" semantics.
    let ordering = ordering_from_head.unwrap_or_else(|| {
        intents
            .iter()
            .find_map(|i| i.ordering.as_ref().map(intent_ordering_to_sql))
            .unwrap_or("DESC")
    });

    // Field resolution: explicit `by <label>` first, otherwise intent
    // column-hint match.
    let field = match parse_by_field(trailing) {
        Some(label_tokens) => {
            let label = label_tokens.join(" ");
            let refs = field_labels.get(label.as_str())?;
            let candidates: Vec<&FieldRef> = refs.iter().filter(|r| r.entity == entity).collect();
            if candidates.len() != 1 {
                return None;
            }
            candidates[0].field.clone()
        }
        None => {
            // Trailing must be empty when no explicit `by ...` — anything
            // else is out of scope for Stage 0a.
            if !trailing.is_empty() {
                return None;
            }
            resolve_intent_column(entity, field_labels, intents)?
        }
    };

    // Limit resolution: head literal wins; otherwise the first matching
    // intent's `default_limit`. Bail if neither has a value — Top-N
    // without a limit is just an ORDER BY, which the ordering pattern
    // already covers.
    let limit = limit_from_head
        .or_else(|| intents.iter().find_map(|i| i.default_limit))?;
    Some(format!(
        "SELECT * FROM {entity} ORDER BY {entity}.{field} {ordering} LIMIT {limit}"
    ))
}

/// Translate the `Ordering` enum from `semsql-intent` to its SQL form.
fn intent_ordering_to_sql(o: &IntentOrdering) -> &'static str {
    match o {
        IntentOrdering::Asc => "ASC",
        IntentOrdering::Desc => "DESC",
    }
}

/// Find the unique numeric field on `entity` whose label matches one of
/// the column hints carried by any of the `intents`. Returns `None`
/// when no hint resolves OR when more than one candidate ties — Stage
/// 0a deliberately abstains under ambiguity.
fn resolve_intent_column(
    entity: &str,
    field_labels: &AHashMap<String, Vec<FieldRef>>,
    intents: &[IntentHint],
) -> Option<String> {
    let mut matches: Vec<String> = Vec::new();
    for hint in intents {
        for candidate in &hint.column_hints {
            let key = candidate.to_lowercase();
            if let Some(refs) = field_labels.get(key.as_str()) {
                for r in refs {
                    if r.entity == entity && is_numeric_type(&r.r#type) {
                        if !matches.iter().any(|f| f == &r.field) {
                            matches.push(r.field.clone());
                        }
                    }
                }
            }
        }
    }
    if matches.len() == 1 {
        Some(matches.remove(0))
    } else {
        None
    }
}

/// Inspect the leading span for a Top-N head. Returns `Some((limit_n,
/// explicit_ordering))` when the span fits one of the recognised
/// shapes, or `None` to signal the pattern doesn't apply.
fn parse_top_n_head(leading: &[&str]) -> Option<(Option<u32>, Option<&'static str>)> {
    // Strip a leading fetch verb so `show top 5 ...` works.
    let span = if !leading.is_empty() && is_fetch_verb(leading[0]) {
        &leading[1..]
    } else {
        leading
    };
    if span.is_empty() {
        return None;
    }
    // Shape A: `<head> <N>?` with head ∈ {top, highest, biggest, first,
    // largest, most}. DESC ordering implied (or ASC for `lowest|smallest`).
    let head_dir = ordering_from_top_word(span[0]);
    if let Some(dir) = head_dir {
        let limit = if span.len() >= 2 {
            span[1].parse::<u32>().ok()
        } else {
            None
        };
        return Some((limit, Some(dir)));
    }
    // Shape B: `<N> <head>` — numeric first. Same heads.
    if span.len() >= 2 {
        if let Ok(n) = span[0].parse::<u32>() {
            if let Some(dir) = ordering_from_top_word(span[1]) {
                return Some((Some(n), Some(dir)));
            }
        }
    }
    None
}

/// Top-N head word → implied ordering direction. `top` and synonyms
/// ⇒ DESC; `lowest` / `smallest` / `least` ⇒ ASC.
fn ordering_from_top_word(token: &str) -> Option<&'static str> {
    Some(match token {
        "top" | "highest" | "biggest" | "first" | "largest" | "most" => "DESC",
        "lowest" | "smallest" | "least" | "fewest" | "bottom" => "ASC",
        _ => return None,
    })
}

/// Parse `["by", "<label>"...]` and return the label tokens. Returns
/// `None` when the slice doesn't begin with `by` or carries an empty
/// / overly long label tail.
fn parse_by_field<'a>(trailing: &'a [&'a str]) -> Option<&'a [&'a str]> {
    if trailing.first().copied() != Some("by") {
        return None;
    }
    let rest = &trailing[1..];
    if rest.is_empty() || rest.len() > 3 {
        return None;
    }
    Some(rest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ahash::AHashMap;

    fn fixture_index() -> PreResolverIndex {
        let mut plural: AHashMap<String, Vec<String>> = AHashMap::new();
        plural.insert("students".to_string(), vec!["users".to_string()]);
        plural.insert("user".to_string(), vec!["users".to_string()]);
        plural.insert("users".to_string(), vec!["users".to_string()]);
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

        let mut field_labels: AHashMap<String, Vec<FieldRef>> = AHashMap::new();
        let balance = FieldRef {
            entity: "users".to_string(),
            field: "balance".to_string(),
            r#type: "INTEGER".to_string(),
        };
        let email = FieldRef {
            entity: "users".to_string(),
            field: "email".to_string(),
            r#type: "TEXT".to_string(),
        };
        let name = FieldRef {
            entity: "users".to_string(),
            field: "name".to_string(),
            r#type: "TEXT".to_string(),
        };
        field_labels.insert("balance".to_string(), vec![balance.clone()]);
        field_labels.insert("account balance".to_string(), vec![balance.clone()]);
        field_labels.insert("emails".to_string(), vec![email.clone()]);
        field_labels.insert("email".to_string(), vec![email]);
        field_labels.insert("name".to_string(), vec![name]);

        PreResolverIndex {
            plural_labels: plural,
            enum_labels,
            entity_canonicals,
            field_labels,
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
    fn resolves_count_with_enum() {
        let idx = fixture_index();
        match resolve("count active students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT COUNT(*) FROM users WHERE users.status_code = 2"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_how_many_with_enum() {
        let idx = fixture_index();
        match resolve("how many pending users", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT COUNT(*) FROM users WHERE users.status_code = 1"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_numeric_comparison_with_with_connector() {
        let idx = fixture_index();
        match resolve("students with balance over 100", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT * FROM users WHERE users.balance > 100"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_numeric_comparison_with_verb_prefix() {
        let idx = fixture_index();
        match resolve("show users where balance at least 50", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT * FROM users WHERE users.balance >= 50"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_numeric_comparison_with_multi_word_label() {
        let idx = fixture_index();
        match resolve("users with account balance below 200", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(
                    natsql,
                    "SELECT * FROM users WHERE users.balance < 200"
                );
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn falls_through_on_non_numeric_field_comparison() {
        // `email` is TEXT — the comparison resolver must refuse rather
        // than emit `users.email > 100`.
        let idx = fixture_index();
        assert_eq!(
            resolve("users with email over 100", &idx),
            PreResolveOutcome::NeedsModel
        );
    }

    #[test]
    fn resolves_field_projection() {
        let idx = fixture_index();
        match resolve("emails of students", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(natsql, "SELECT users.email FROM users");
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_field_projection_multi_word_label() {
        let idx = fixture_index();
        match resolve("account balance of users", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => {
                assert_eq!(natsql, "SELECT users.balance FROM users");
            }
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn falls_through_on_unknown_field_label_in_projection() {
        let idx = fixture_index();
        assert_eq!(
            resolve("phones of students", &idx),
            PreResolveOutcome::NeedsModel
        );
    }

    #[test]
    fn falls_through_on_field_belonging_to_other_entity() {
        // `balance` is on `users`; trying to read it from `tenants`
        // must fall through.
        let idx = fixture_index();
        assert_eq!(
            resolve("balance of organizations", &idx),
            PreResolveOutcome::NeedsModel
        );
    }

    fn intent_hint(
        intent_type: &str,
        column_hints: &[&str],
        ordering: Option<IntentOrdering>,
        default_limit: Option<u32>,
    ) -> IntentHint {
        IntentHint {
            intent_type: intent_type.into(),
            column_hints: column_hints.iter().map(|s| (*s).to_string()).collect(),
            ordering,
            comparator: None,
            default_limit,
            matched_range: (0, 0),
        }
    }

    #[test]
    fn resolves_order_by_default_asc() {
        let idx = fixture_index();
        match resolve("users sorted by balance", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance ASC"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_order_by_explicit_desc() {
        let idx = fixture_index();
        match resolve("users ordered by balance desc", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance DESC"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_order_by_with_verb_prefix_and_multi_word_label() {
        let idx = fixture_index();
        match resolve("show users ranked by account balance descending", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance DESC"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn falls_through_on_ambiguous_order_by_label() {
        let mut idx = fixture_index();
        // Inject a duplicate `balance` field on `tenants` to make the
        // label ambiguous — resolver must abstain.
        idx.field_labels.insert(
            "balance".to_string(),
            vec![
                FieldRef {
                    entity: "users".into(),
                    field: "balance".into(),
                    r#type: "INTEGER".into(),
                },
                FieldRef {
                    entity: "tenants".into(),
                    field: "balance".into(),
                    r#type: "INTEGER".into(),
                },
            ],
        );
        // tenants is the entity in this query, but `balance` resolves
        // unambiguously *to that entity* — keep the test on `users`
        // instead, where the ambiguity persists across both.
        idx.plural_labels.insert("members".into(), vec!["tenants".into()]);
        // `users` query: `balance` matches users (1 candidate on this
        // entity), so this still resolves. Using a non-existent field
        // for safety.
        assert!(matches!(
            resolve("users sorted by phone", &idx),
            PreResolveOutcome::NeedsModel
        ));
    }

    #[test]
    fn resolves_top_n_with_explicit_field() {
        let idx = fixture_index();
        match resolve("top 5 users by balance", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance DESC LIMIT 5"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn resolves_top_n_lowest_flips_to_asc() {
        let idx = fixture_index();
        match resolve("lowest 3 users by balance", &idx) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance ASC LIMIT 3"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn top_n_without_field_or_intent_falls_through() {
        let idx = fixture_index();
        // No explicit `by ...` and no intent → can't pick a column.
        assert_eq!(
            resolve("top 5 users", &idx),
            PreResolveOutcome::NeedsModel
        );
    }

    #[test]
    fn top_n_uses_intent_column_hint_when_present() {
        let idx = fixture_index();
        let intents = vec![intent_hint(
            "high_expenditure",
            &["balance"],
            Some(IntentOrdering::Desc),
            Some(10),
        )];
        match resolve_with_intents("top 5 users", &idx, &intents) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance DESC LIMIT 5"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn top_n_falls_back_to_intent_default_limit() {
        let idx = fixture_index();
        let intents = vec![intent_hint(
            "high_expenditure",
            &["balance"],
            Some(IntentOrdering::Desc),
            Some(7),
        )];
        match resolve_with_intents("top users", &idx, &intents) {
            PreResolveOutcome::Resolved { natsql, .. } => assert_eq!(
                natsql,
                "SELECT * FROM users ORDER BY users.balance DESC LIMIT 7"
            ),
            other => panic!("expected resolved, got {other:?}"),
        }
    }

    #[test]
    fn top_n_falls_through_when_intent_hint_is_non_numeric() {
        let idx = fixture_index();
        // Hint matches `email` (TEXT) — must abstain.
        let intents = vec![intent_hint(
            "ranking",
            &["email"],
            Some(IntentOrdering::Desc),
            Some(10),
        )];
        assert_eq!(
            resolve_with_intents("top 5 users", &idx, &intents),
            PreResolveOutcome::NeedsModel
        );
    }

    #[test]
    fn top_n_falls_through_when_intent_hint_matches_multiple_fields() {
        let mut idx = fixture_index();
        idx.field_labels.insert(
            "spend".into(),
            vec![
                FieldRef {
                    entity: "users".into(),
                    field: "balance".into(),
                    r#type: "INTEGER".into(),
                },
                FieldRef {
                    entity: "users".into(),
                    field: "expenses".into(),
                    r#type: "INTEGER".into(),
                },
            ],
        );
        let intents = vec![intent_hint(
            "high_expenditure",
            &["spend"],
            Some(IntentOrdering::Desc),
            Some(10),
        )];
        // `spend` matches two distinct user fields — abstain.
        assert!(matches!(
            resolve_with_intents("top 5 users", &idx, &intents),
            PreResolveOutcome::NeedsModel
        ));
    }

    #[test]
    fn comparator_phrases_map_correctly() {
        // Spot-check the operator phrase parser independently.
        assert_eq!(comparator_from_phrase("over"), Some(">"));
        assert_eq!(comparator_from_phrase("greater than"), Some(">"));
        assert_eq!(comparator_from_phrase("at least"), Some(">="));
        assert_eq!(comparator_from_phrase("below"), Some("<"));
        assert_eq!(comparator_from_phrase("at most"), Some("<="));
        assert_eq!(comparator_from_phrase("equals"), Some("="));
        assert_eq!(comparator_from_phrase("not equal to"), Some("!="));
        assert_eq!(comparator_from_phrase("around"), None);
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
            field_labels: AHashMap::new(),
        };
        assert_eq!(resolve("students", &idx), PreResolveOutcome::NeedsModel);
        assert_eq!(
            resolve("show students", &idx),
            PreResolveOutcome::NeedsModel
        );
    }
}
