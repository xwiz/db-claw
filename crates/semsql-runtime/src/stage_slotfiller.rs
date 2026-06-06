//! Stage 3 — Slot Filler + IntentResolver (~5M params).
//!
//! Stage 2 emits a NatSQL skeleton with placeholder slots: `@entity1`,
//! `@field1`, `@val1`, etc. Stage 3 picks the right candidate for every
//! slot from a tight short-list produced by Stage 1 (typically <10 per
//! slot). The scoring model is the same cross-encoder shape as Stage 1
//! — `(NL + skeleton, candidate)` pairs ranked by class-1 probability —
//! so the implementation reuses `OnnxCrossEncoder` directly.
//!
//! The **IntentResolver** is the deterministic logic alongside the
//! model:
//!
//!  1. If a slot has multiple candidates AND intent hints fired in
//!     Stage 0b, prefer candidates matching `column_hints`. The bias
//!     is large enough to override small score gaps but small enough
//!     to lose against a clearly-better non-hint candidate (mirrors
//!     Stage 1's `intent_bias`).
//!  2. If a slot has no candidates, or the best candidate is below the
//!     escalate to clarification — the resolver records the slot in
//!     [`SlotFillerOutput::escalations`] instead of guessing.
//!  3. Enum + unit resolution is downstream of slot filling and lives
//!     in `semsql-natsql`'s transpile pass; Stage 3 just picks the
//!     canonical name.
//!
//! Like Stage 1, the model-backed `SlotFiller` is gated behind
//! `--features onnx` so the default build stays dep-light. The
//! deterministic helpers (`apply_intent_preference`, `pick_top1`) are
//! always available.

#![cfg_attr(not(feature = "onnx"), allow(dead_code))]

#[cfg(feature = "onnx")]
use crate::onnx::OnnxCrossEncoder;
use semsql_core::Result;
use serde::Serialize;
#[cfg(feature = "onnx")]
use std::path::Path;

/// One slot to fill: which placeholder, what candidates Stage 1
/// produced for it, and an optional list of intent column-hint tokens
/// that bias the selection.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SlotInput {
    /// Slot placeholder, e.g. `"@entity1"`, `"@field2"`.
    pub slot_name: String,
    /// Candidate canonical values (entity names, `entity.field` strings,
    /// or value literals).
    pub candidates: Vec<String>,
    /// For value slots, source fields that supplied each candidate via DB
    /// sample-value retrieval. Empty entries are NL-extracted literals or
    /// candidates without field provenance.
    pub candidate_source_fields: Vec<Vec<String>>,
    /// For value slots, the predicate field placeholder this value is attached
    /// to, e.g. `@field4` in `WHERE @field4 = @val1`.
    pub predicate_field_slot: Option<String>,
    /// Skeleton-derived role for this slot, e.g. `join_key`,
    /// `projection_field`, `predicate_field`, or `predicate_value`.
    pub slot_role: String,
    /// Predicate operator associated with this slot, when applicable.
    pub predicate_operator: Option<String>,
    /// Small skeleton slice around the slot for trace/debug reports.
    pub context_window: String,
    /// Allow source-free NL literals to survive a strict DB-sample source
    /// filter. This is only set for slots whose SQL context calls for a
    /// numeric literal, such as `> @val1`, `BETWEEN @val1`, or `LIMIT @val1`.
    pub preserve_source_empty_literals: bool,
    /// Intent hints that fired in Stage 0b for this query. The
    /// resolver prefers candidates whose canonical-tail substring
    /// matches one of these.
    pub intent_hints: Vec<String>,
}

/// Final NatSQL after slot resolution.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct SlotFillerOutput {
    /// Concrete NatSQL — every `@slot` resolved.
    pub concrete_natsql: String,
    /// Mean per-slot top-1 confidence — used for routing.
    pub mean_confidence: f32,
    /// Slots that escalated to clarification telemetry. Candidate-backed
    /// slots are still filled with the best available value; no-candidate
    /// slots remain unresolved.
    pub escalations: Vec<String>,
    /// Per-slot model decisions for benchmark diagnosis.
    pub decisions: Vec<SlotDecision>,
}

/// One candidate scored for one slot, surfaced for benchmark diagnosis.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct SlotDecisionCandidate {
    /// Candidate value exactly as supplied to the slot-filler model.
    pub value: String,
    /// Raw class-1 probability from the cross-encoder.
    pub score: f32,
    /// Score after deterministic intent bias.
    pub biased_score: f32,
    /// DB sample-value source fields, if any.
    pub source_fields: Vec<String>,
}

/// One resolved slot, including the candidate competition that led to it.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct SlotDecision {
    /// Slot placeholder, e.g. `"@field2"`.
    pub slot_name: String,
    /// Coarse kind: `entity`, `field`, `value`, or `other`.
    pub slot_kind: String,
    /// Skeleton-derived slot role.
    pub slot_role: String,
    /// Skeleton context passed to the cross-encoder for this slot.
    pub context_skeleton: String,
    /// Small skeleton slice around the slot.
    pub context_window: String,
    /// Predicate field slot for values, e.g. `@field4`.
    pub predicate_field_slot: Option<String>,
    /// Predicate operator associated with this slot, when applicable.
    pub predicate_operator: Option<String>,
    /// Resolved predicate field for values, when available.
    pub predicate_field: Option<String>,
    /// Number of candidates before deterministic filters.
    pub original_candidate_count: usize,
    /// Candidates actually scored by the model.
    pub candidates: Vec<SlotDecisionCandidate>,
    /// Picked value, if a candidate was selected.
    pub picked: Option<String>,
    /// Picked index in the scored candidate list.
    pub picked_index: Option<usize>,
    /// Whether this slot was marked as an escalation.
    pub escalated: bool,
}

/// One scored candidate, returned by [`pick_top1`] for diagnostics.
#[derive(Clone, Debug, PartialEq)]
pub struct ScoredCandidate {
    /// The candidate string.
    pub value: String,
    /// `[0.0, 1.0]` — biased and re-normalised score.
    pub score: f32,
    /// True iff the score includes an intent-preference bias.
    pub from_intent_hint: bool,
}

/// Threshold below which a slot is marked as an escalation while still
/// committing the model's top pick. The current value keeps BIRD diagnostic
/// runs observable without turning every low-confidence but executable slot
/// into a hard abstention.
pub const ABSTAIN_THRESHOLD: f32 = 0.1;

/// Bias added to candidates whose canonical tail matches an intent
/// hint. Smaller than Stage 1's `intent_bias` (0.3) because Stage 3
/// scores are already tightly bounded — too large a bump would
/// over-rule the model on every hinted slot.
pub const INTENT_PREFERENCE_BIAS: f32 = 0.2;

const FIELD_REUSE_PENALTY: f32 = 0.90;
const JOIN_ENDPOINT_CONTENT_PENALTY: f32 = 0.75;
const PREDICATE_VALUE_SOURCE_FIELD_BIAS: f32 = 0.70;
const SELECTED_VALUE_SOURCE_BIAS: f32 = 0.70;

/// Typed prior for boolean-like value fields. The current slot model has
/// a strong false-default on some BIRD boolean columns; field semantics are
/// more reliable than that local score when the natural language is positive
/// or explicitly negative.
pub const BOOLEAN_VALUE_ROLE_BIAS: f32 = 1.0;

const MAX_VALUE_SCORING_CANDIDATES: usize = 32;

/// Apply the intent-hint preference bias to a parallel
/// `(candidate, score)` vector. Mirrors `stage_linker::apply_intent_bias`
/// with a smaller default bias.
///
/// Match rule: a candidate matches a hint `h` iff the lower-cased hint
/// is a substring of the candidate's lower-cased canonical name OR the
/// candidate's tail (the segment after the last `.`).
pub fn apply_intent_preference(
    candidates: &[String],
    scores: &[f32],
    hints: &[String],
    bias: f32,
) -> Vec<f32> {
    if hints.is_empty() || candidates.len() != scores.len() {
        return scores.to_vec();
    }
    let normalised: Vec<String> = hints.iter().map(|h| h.to_lowercase()).collect();
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(c, &s)| {
            let lower = c.to_lowercase();
            let tail = lower.rsplit_once('.').map(|(_, t)| t).unwrap_or(&lower);
            let matched = normalised
                .iter()
                .any(|h| lower.contains(h.as_str()) || tail.contains(h.as_str()));
            if matched {
                s + bias
            } else {
                s
            }
        })
        .collect()
}

/// Select the top-1 candidate with intent preference applied. Returns
/// `None` if `candidates` is empty or every score is `NaN`.
pub fn pick_top1(
    candidates: &[String],
    scores: &[f32],
    hints: &[String],
) -> Option<ScoredCandidate> {
    if candidates.is_empty() || candidates.len() != scores.len() {
        return None;
    }
    let biased = apply_intent_preference(candidates, scores, hints, INTENT_PREFERENCE_BIAS);
    let mut best_idx: Option<usize> = None;
    let mut best_score = f32::NEG_INFINITY;
    for (i, &s) in biased.iter().enumerate() {
        if s.is_nan() {
            continue;
        }
        if s > best_score {
            best_score = s;
            best_idx = Some(i);
        }
    }
    best_idx.map(|i| {
        let original = scores[i];
        ScoredCandidate {
            value: candidates[i].clone(),
            // Re-normalise the surfaced score to the original
            // [0,1] range so downstream consumers don't see
            // post-bias scores > 1.0 — confusing in dashboards.
            score: original.clamp(0.0, 1.0),
            from_intent_hint: (biased[i] - original).abs() > f32::EPSILON,
        }
    })
}

fn cap_candidates(indexes: &mut Vec<usize>, candidates: &mut Vec<String>, max_len: usize) {
    if max_len == 0 {
        indexes.clear();
        candidates.clear();
        return;
    }
    if candidates.len() <= max_len {
        return;
    }
    let pairs: Vec<(usize, String)> = indexes
        .iter()
        .copied()
        .zip(candidates.iter().cloned())
        .collect();
    let mut capped: Vec<(usize, String)> = pairs
        .iter()
        .filter(|(idx, _)| *idx == usize::MAX)
        .take(max_len)
        .cloned()
        .collect();
    if capped.len() < max_len {
        for pair in pairs {
            if pair.0 == usize::MAX {
                continue;
            }
            capped.push(pair);
            if capped.len() == max_len {
                break;
            }
        }
    }
    indexes.clear();
    candidates.clear();
    for (idx, candidate) in capped {
        indexes.push(idx);
        candidates.push(candidate);
    }
}

fn ensure_scoring_candidate(indexes: &mut Vec<usize>, candidates: &mut Vec<String>, value: &str) {
    if candidates.iter().any(|candidate| candidate == value) {
        return;
    }
    indexes.push(usize::MAX);
    candidates.push(value.to_string());
}

fn ensure_typed_value_candidates(
    nl: &str,
    selected_field: &str,
    indexes: &mut Vec<usize>,
    candidates: &mut Vec<String>,
) {
    let field_tail = selected_field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(selected_field)
        .to_ascii_lowercase();
    if field_tail == "street"
        || field_tail.contains("street")
        || field_tail.contains("address")
        || field_tail.contains("mailing")
    {
        for value in po_box_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &format!("'{value}'"));
        }
    }
    if field_tail.contains("academic_year") || field_tail == "year" {
        for year in four_digit_year_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &year);
        }
    }
    if field_tail == "forename" || field_tail.ends_with("_forename") {
        for (first, _) in person_name_literal_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &sql_string_literal(&first));
        }
    }
    if field_tail == "surname" || field_tail.ends_with("_surname") {
        for (_, last) in person_name_literal_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &sql_string_literal(&last));
        }
    }
    if field_tail == "year" {
        for year in four_digit_year_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &year);
        }
    }
    if text_literal_field_tail(&field_tail) {
        for value in quoted_phrase_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &sql_string_literal(&value));
        }
        for value in named_text_mentions(nl, &field_tail) {
            ensure_scoring_candidate(indexes, candidates, &sql_string_literal(&value));
        }
    }
    if id_like_field_tail(&field_tail) {
        for value in id_numeric_mentions(nl) {
            ensure_scoring_candidate(indexes, candidates, &value);
        }
    }
    if card_number_field_tail(&field_tail) {
        for value in numbered_card_mentions(nl) {
            let literal = if value.chars().all(|ch| ch.is_ascii_digit()) {
                value
            } else {
                sql_string_literal(&value)
            };
            ensure_scoring_candidate(indexes, candidates, &literal);
        }
    }
    if !numeric_field_context_aliases(&field_tail).is_empty() {
        for value in numeric_mentions_near_field(nl, &field_tail) {
            ensure_scoring_candidate(indexes, candidates, &value);
        }
    }
}

fn augment_value_candidates_for_selected_field(
    nl: &str,
    slot: &SlotInput,
    selected_field: &str,
    indexes: &mut Vec<usize>,
    candidates: &mut Vec<String>,
) {
    if !slot.slot_name.starts_with("@val") {
        return;
    }
    if boolean_like_field(selected_field) {
        ensure_scoring_candidate(indexes, candidates, "1");
        ensure_scoring_candidate(indexes, candidates, "0");
    }
    ensure_typed_value_candidates(nl, selected_field, indexes, candidates);
}

fn phrase_is_mentioned(haystack_lower: &str, phrase_lower: &str) -> bool {
    if phrase_lower.contains('-') {
        let spaced = phrase_lower.replace('-', " ");
        return phrase_is_mentioned(haystack_lower, &spaced)
            || haystack_lower.contains(phrase_lower);
    }
    let phrase_tokens: Vec<&str> = phrase_lower.split_whitespace().collect();
    if phrase_tokens.is_empty() {
        return false;
    }
    let tokens: Vec<&str> = haystack_lower
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| !token.is_empty())
        .collect();
    tokens
        .windows(phrase_tokens.len())
        .any(|window| window == phrase_tokens.as_slice())
}

fn po_box_mentions(nl: &str) -> Vec<String> {
    let tokens: Vec<&str> = nl
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '.'))
        .filter(|token| !token.is_empty())
        .collect();
    let mut out = Vec::new();
    for window in tokens.windows(3) {
        if window[0].eq_ignore_ascii_case("po")
            && window[1].eq_ignore_ascii_case("box")
            && window[2].chars().all(|ch| ch.is_ascii_digit())
        {
            out.push(format!("PO Box {}", window[2]));
        }
    }
    out
}

fn numeric_literal_mentions(nl: &str) -> Vec<String> {
    let mut out = Vec::new();
    for token in nl.split(|ch: char| !(ch.is_ascii_digit() || ch == ',' || ch == '.')) {
        let trimmed = token.trim_matches(|ch: char| ch == ',' || ch == '.');
        if trimmed.is_empty() || !trimmed.chars().any(|ch| ch.is_ascii_digit()) {
            continue;
        }
        let normalized = trimmed.replace(',', "");
        if normalized
            .chars()
            .all(|ch| ch.is_ascii_digit() || ch == '.')
            && !out.iter().any(|existing| existing == &normalized)
        {
            out.push(normalized);
        }
    }
    out
}

fn numeric_mentions_near_field(nl: &str, field_tail: &str) -> Vec<String> {
    let aliases = numeric_field_context_aliases(field_tail);
    if aliases.is_empty() {
        return Vec::new();
    }
    let nl_lower = nl.to_ascii_lowercase();
    let mut out = Vec::new();
    for alias in aliases {
        let alias_lower = alias.to_ascii_lowercase();
        let mut offset = 0;
        while let Some(relative) = nl_lower[offset..].find(&alias_lower) {
            let phrase_start = offset + relative;
            let phrase_end = phrase_start + alias_lower.len();
            let window_start = phrase_start.saturating_sub(36);
            let window_end = (phrase_end + 28).min(nl.len());
            let before = trim_to_last_clause_boundary(&nl[window_start..phrase_start]);
            let after = trim_to_first_clause_boundary(&nl[phrase_end..window_end]);
            let window = format!("{before}{}{after}", &nl[phrase_start..phrase_end]);
            for value in numeric_literal_mentions(&window) {
                if !out.iter().any(|existing| existing == &value) {
                    out.push(value);
                }
            }
            offset = phrase_end;
        }
    }
    out
}

fn trim_to_last_clause_boundary(text: &str) -> &str {
    let punctuation = text
        .char_indices()
        .rev()
        .find(|(_, ch)| matches!(ch, ',' | ';' | '?' | '!'))
        .map(|(idx, ch)| idx + ch.len_utf8());
    let conjunction = text.to_ascii_lowercase().rfind(" and ").map(|idx| idx + 5);
    match (punctuation, conjunction) {
        (Some(a), Some(b)) => &text[a.max(b)..],
        (Some(idx), None) | (None, Some(idx)) => &text[idx..],
        (None, None) => text,
    }
}

fn trim_to_first_clause_boundary(text: &str) -> &str {
    let punctuation = text
        .char_indices()
        .find(|(_, ch)| matches!(ch, ',' | ';' | '?' | '!'))
        .map(|(idx, _)| idx);
    let conjunction = text.to_ascii_lowercase().find(" and ");
    match (punctuation, conjunction) {
        (Some(a), Some(b)) => &text[..a.min(b)],
        (Some(idx), None) | (None, Some(idx)) => &text[..idx],
        (None, None) => text,
    }
}

fn numeric_field_context_aliases(field_tail: &str) -> Vec<String> {
    let tail = field_tail.to_ascii_lowercase();
    let mut aliases = Vec::new();
    let mut push = |value: &str| {
        if !aliases.iter().any(|existing: &String| existing == value) {
            aliases.push(value.to_string());
        }
    };
    match tail.as_str() {
        "upvotes" => {
            push("upvotes");
            push("up votes");
            push("upvote");
        }
        "downvotes" => {
            push("downvotes");
            push("down votes");
            push("downvote");
        }
        "viewcount" | "view_count" => {
            push("view count");
            push("view counts");
            push("viewed");
            push("views");
        }
        "score" => {
            push("score");
            push("rating score");
        }
        "favoritecount" | "favorite_count" => {
            push("favorite count");
            push("favorite counts");
            push("favorite amount");
            push("favorites");
        }
        "answercount" | "answer_count" => {
            push("answer count");
            push("answers");
        }
        "commentcount" | "comment_count" => {
            push("comment count");
            push("comments");
        }
        "bountyamount" | "bounty_amount" => {
            push("bounty amount");
            push("bounty");
        }
        "reputation" => {
            push("reputation");
        }
        _ => {}
    }
    aliases
}

fn four_digit_year_mentions(nl: &str) -> Vec<String> {
    nl.split(|ch: char| !ch.is_ascii_digit())
        .filter(|token| token.len() == 4 && (token.starts_with("19") || token.starts_with("20")))
        .map(str::to_string)
        .collect()
}

fn sql_string_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

fn text_literal_field_tail(field_tail: &str) -> bool {
    field_tail.contains("title")
        || field_tail.contains("name")
        || field_tail.contains("displayname")
        || field_tail.contains("event")
        || field_tail.contains("major")
        || field_tail.contains("artist")
}

fn id_like_field_tail(field_tail: &str) -> bool {
    field_tail == "id"
        || field_tail.ends_with("_id")
        || field_tail.ends_with("id")
        || field_tail.contains("userid")
        || field_tail.contains("user_id")
        || field_tail.contains("parentid")
        || field_tail.contains("parent_id")
}

fn card_number_field_tail(field_tail: &str) -> bool {
    field_tail == "number" || field_tail == "cardnumber" || field_tail == "card_number"
}

fn quoted_phrase_mentions(nl: &str) -> Vec<String> {
    let mut out = Vec::new();
    for quote in ['"', '\''] {
        let mut offset = 0;
        while let Some(relative_start) = nl[offset..].find(quote) {
            let start = offset + relative_start;
            if quote == '\''
                && start > 0
                && nl[..start]
                    .chars()
                    .next_back()
                    .is_some_and(|ch| ch.is_ascii_alphanumeric())
            {
                offset = start + quote.len_utf8();
                continue;
            }
            let content_start = start + quote.len_utf8();
            let Some(relative_end) = nl[content_start..].find(quote) else {
                break;
            };
            let end = content_start + relative_end;
            if quote == '\''
                && nl[end + quote.len_utf8()..]
                    .chars()
                    .next()
                    .is_some_and(|ch| ch.is_ascii_alphanumeric())
            {
                offset = end + quote.len_utf8();
                continue;
            }
            if let Some(cleaned) = clean_text_literal(&nl[content_start..end]) {
                if !out.iter().any(|existing| existing == &cleaned) {
                    out.push(cleaned);
                }
            }
            offset = end + quote.len_utf8();
        }
    }
    out
}

fn named_text_mentions(nl: &str, field_tail: &str) -> Vec<String> {
    let mut out = Vec::new();
    if field_tail.contains("displayname") || field_tail.contains("user") {
        for marker in ["display name is ", "display name ", "username of "] {
            collect_text_after_marker(nl, marker, &mut out);
        }
        for marker in ["user ", "by "] {
            collect_text_after_marker(nl, marker, &mut out);
        }
    }
    if field_tail.contains("artist") {
        collect_text_after_marker(nl, "illustrated by ", &mut out);
        collect_text_after_marker(nl, "artist ", &mut out);
    }
    if field_tail.contains("name") || field_tail.contains("title") {
        for marker in ["named ", "called "] {
            collect_text_after_marker(nl, marker, &mut out);
        }
    }
    if field_tail.contains("major") {
        for marker in ["majored ", "major of "] {
            collect_text_after_marker(nl, marker, &mut out);
        }
        collect_text_between_markers(nl, "under the ", " major", &mut out);
    }
    out
}

fn collect_text_after_marker(nl: &str, marker: &str, out: &mut Vec<String>) {
    let lower = nl.to_ascii_lowercase();
    let marker_lower = marker.to_ascii_lowercase();
    let mut offset = 0;
    while let Some(relative) = lower[offset..].find(&marker_lower) {
        let start = offset + relative + marker.len();
        let tail = &nl[start..];
        let end = tail.find(['?', '.', ',', ';']).unwrap_or(tail.len());
        let mut text = &tail[..end];
        for stop in [
            " numbered ",
            " number ",
            " with ",
            " which ",
            " that ",
            " who ",
            " sorted ",
            " obtained ",
            " obtained",
            " illustrated ",
            " illustrated",
            " about ",
            " about",
            " took ",
            " took",
            " gave ",
            " gave",
        ] {
            if let Some(stop_at) = text.to_ascii_lowercase().find(stop) {
                text = &text[..stop_at];
            }
        }
        if let Some(cleaned) = clean_text_literal(text) {
            let lower_cleaned = cleaned.to_ascii_lowercase();
            if !VALUE_LEXICAL_STOP.contains(&lower_cleaned.as_str())
                && !matches!(
                    lower_cleaned.as_str(),
                    "who" | "which" | "what" | "where" | "when" | "how" | "the" | "a" | "an"
                )
                && !lower_cleaned.starts_with("whose ")
                && !lower_cleaned.starts_with("who ")
                && !lower_cleaned.starts_with("with ")
                && !lower_cleaned.starts_with("is ")
                && !out.iter().any(|existing| existing == &cleaned)
            {
                out.push(cleaned);
            }
        }
        offset = start;
    }
}

fn clean_text_literal(text: &str) -> Option<String> {
    let cleaned = text
        .trim_matches(|ch: char| ch.is_whitespace() || ch == '"' || ch == '\'')
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    (cleaned.len() >= 2 && cleaned.chars().any(|ch| ch.is_ascii_alphanumeric())).then_some(cleaned)
}

fn collect_text_between_markers(
    nl: &str,
    start_marker: &str,
    end_marker: &str,
    out: &mut Vec<String>,
) {
    let lower = nl.to_ascii_lowercase();
    let start_lower = start_marker.to_ascii_lowercase();
    let end_lower = end_marker.to_ascii_lowercase();
    let mut offset = 0;
    while let Some(relative_start) = lower[offset..].find(&start_lower) {
        let start = offset + relative_start + start_marker.len();
        let Some(relative_end) = lower[start..].find(&end_lower) else {
            break;
        };
        let end = start + relative_end;
        if let Some(cleaned) = clean_text_literal(&nl[start..end]) {
            let lower_cleaned = cleaned.to_ascii_lowercase();
            if !VALUE_LEXICAL_STOP.contains(&lower_cleaned.as_str())
                && !out.iter().any(|existing| existing == &cleaned)
            {
                out.push(cleaned);
            }
        }
        offset = end + end_marker.len();
    }
}

fn id_numeric_mentions(nl: &str) -> Vec<String> {
    let mut out = Vec::new();
    for marker in [
        "no.",
        "no ",
        "id ",
        "id '",
        "patient id ",
        "user no.",
        "user no ",
        "parent id ",
    ] {
        collect_number_after_marker(nl, marker, false, &mut out);
    }
    out
}

fn numbered_card_mentions(nl: &str) -> Vec<String> {
    let mut out = Vec::new();
    for marker in ["numbered ", "number "] {
        collect_number_after_marker(nl, marker, true, &mut out);
    }
    out
}

fn collect_number_after_marker(
    nl: &str,
    marker: &str,
    allow_alpha_suffix: bool,
    out: &mut Vec<String>,
) {
    let lower = nl.to_ascii_lowercase();
    let marker_lower = marker.to_ascii_lowercase();
    let mut offset = 0;
    while let Some(relative) = lower[offset..].find(&marker_lower) {
        let mut pos = offset + relative + marker.len();
        if pos >= nl.len() {
            break;
        }
        while let Some(ch) = nl[pos..].chars().next() {
            if ch.is_whitespace() || ch == '\'' || ch == '"' || ch == ':' || ch == '#' {
                pos += ch.len_utf8();
            } else {
                break;
            }
        }
        let mut end = pos;
        for ch in nl[pos..].chars() {
            if ch.is_ascii_digit() || (allow_alpha_suffix && ch.is_ascii_alphabetic()) {
                end += ch.len_utf8();
            } else {
                break;
            }
        }
        if end > pos {
            let value = nl[pos..end].to_string();
            if value.chars().any(|ch| ch.is_ascii_digit())
                && !out.iter().any(|existing| existing == &value)
            {
                out.push(value);
            }
        }
        let next_offset = pos.saturating_add(1);
        if next_offset >= lower.len() {
            break;
        }
        offset = next_offset;
    }
}

fn softly_retain_filtered_field_candidates(
    candidate_indexes: &[usize],
    candidates: &[String],
    used_content_fields: &std::collections::BTreeSet<String>,
    used_join_fields: &std::collections::BTreeSet<String>,
    filter_join_endpoints: bool,
) -> (Vec<usize>, Vec<String>) {
    let mut fresh: Vec<(usize, String)> = Vec::new();
    let mut retained: Vec<(usize, String)> = Vec::new();
    for (idx, candidate) in candidates.iter().enumerate() {
        let original_idx = candidate_indexes.get(idx).copied().unwrap_or(idx);
        let filtered = used_content_fields.contains(candidate.as_str())
            || (filter_join_endpoints && used_join_fields.contains(candidate.as_str()));
        if filtered {
            retained.push((original_idx, candidate.clone()));
        } else {
            fresh.push((original_idx, candidate.clone()));
        }
    }
    if fresh.is_empty() || retained.is_empty() {
        return (candidate_indexes.to_vec(), candidates.to_vec());
    }
    fresh.extend(retained);
    let indexes = fresh.iter().map(|(idx, _)| *idx).collect();
    let values = fresh.into_iter().map(|(_, candidate)| candidate).collect();
    (indexes, values)
}

fn slot_resolution_rank(slot: &SlotInput) -> u8 {
    if slot.slot_name.starts_with("@entity") {
        return 0;
    }
    if slot.slot_name.starts_with("@field") {
        return match slot.slot_role.as_str() {
            "join_key" => 1,
            "projection_field" => 2,
            "order_field" | "group_field" => 3,
            "predicate_field" => 4,
            _ => 5,
        };
    }
    if slot.slot_name.starts_with("@val") {
        return 6;
    }
    7
}

fn apply_value_role_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    predicate_field: Option<&str>,
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@val") || candidates.len() != scores.len() {
        return scores.to_vec();
    }
    let Some(field) = predicate_field else {
        return scores.to_vec();
    };
    if !boolean_like_field(field) {
        return scores.to_vec();
    }
    let prefer_true = boolean_preference_for_field(nl, field);
    candidates
        .iter()
        .zip(scores.iter())
        .map(
            |(candidate, score)| match (prefer_true, candidate.as_str()) {
                (true, "1") | (false, "0") => score + BOOLEAN_VALUE_ROLE_BIAS,
                _ => *score,
            },
        )
        .collect()
}

fn boolean_preference_for_field(nl: &str, field: &str) -> bool {
    let _ = field;
    boolean_preference_is_true(nl)
}

fn apply_value_lexical_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    predicate_field: Option<&str>,
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@val") || candidates.len() != scores.len() {
        return scores.to_vec();
    }
    if predicate_field.is_some_and(value_lexical_bias_skip_field) {
        return scores.to_vec();
    }
    let nl_tokens = role_content_tokens(nl);
    if nl_tokens.is_empty() {
        return scores.to_vec();
    }
    let nl_norm = normalised_alnum(nl);
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            let bare = candidate.trim_matches('\'');
            let cand_norm = normalised_alnum(bare);
            let phrase_bonus = if cand_norm.len() >= 4 && nl_norm.contains(&cand_norm) {
                0.40 + (cand_norm.len().min(24) as f32 * 0.015)
            } else {
                0.0
            };
            let cand_tokens: Vec<String> = role_content_tokens(bare)
                .into_iter()
                .filter(|token| !VALUE_LEXICAL_STOP.contains(&token.as_str()))
                .collect();
            if cand_tokens.is_empty() {
                return score + phrase_bonus;
            }
            let matched = cand_tokens
                .iter()
                .filter(|cand_token| {
                    nl_tokens
                        .iter()
                        .any(|nl_token| value_token_matches(nl_token, cand_token))
                })
                .count();
            let coverage = matched as f32 / cand_tokens.len() as f32;
            let token_bonus = if matched > 0 && coverage >= 0.5 {
                (0.22 * coverage).min(0.22)
            } else {
                0.0
            };
            score + phrase_bonus + token_bonus
        })
        .collect()
}

fn apply_value_field_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    predicate_field: Option<&str>,
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@val") || candidates.len() != scores.len() {
        return scores.to_vec();
    }
    let Some(field) = predicate_field else {
        return scores.to_vec();
    };
    let field_tail = field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(field)
        .to_ascii_lowercase();
    let nl_lower = nl.to_ascii_lowercase();
    let names = person_name_parts(nl);
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            let bare = candidate.trim_matches('\'');
            let bare_lower = bare.to_ascii_lowercase();
            let mut adjusted = *score;
            if field_tail.contains("fname")
                || field_tail.contains("first_name")
                || field_tail == "forename"
            {
                let is_first = names.iter().any(|(first, _)| bare_lower == *first);
                let is_last = names.iter().any(|(_, last)| bare_lower == *last);
                let is_full_name = names
                    .iter()
                    .any(|(first, last)| bare_lower == format!("{first} {last}"));
                if is_first && !is_last {
                    adjusted += 1.35;
                } else if is_full_name {
                    adjusted -= 1.75;
                } else if is_last || bare_lower.contains(' ') {
                    adjusted -= 0.50;
                }
            }
            if field_tail.contains("lname")
                || field_tail.contains("last_name")
                || field_tail == "surname"
            {
                let is_first = names.iter().any(|(first, _)| bare_lower == *first);
                let is_last = names.iter().any(|(_, last)| bare_lower == *last);
                let is_shorter_particle_surname = names.iter().any(|(_, last)| {
                    last.contains(' ') && last.ends_with(&bare_lower) && bare_lower != *last
                });
                if is_last && !is_first {
                    adjusted += if bare_lower.contains(' ') { 1.55 } else { 1.35 };
                    if is_shorter_particle_surname {
                        adjusted -= 0.45;
                    }
                } else if is_first || bare_lower.contains(' ') {
                    adjusted -= 0.50;
                }
            }
            if let Some((expected_name, is_first_name)) =
                expected_person_name_for_value_slot(slot, &field_tail, &names)
            {
                if bare_lower == expected_name {
                    adjusted += 1.45;
                } else if is_first_name {
                    if names.iter().any(|(first, _)| bare_lower == *first) {
                        adjusted -= 0.35;
                    }
                } else if names.iter().any(|(_, last)| bare_lower == *last) {
                    adjusted -= 0.35;
                }
            }
            if (field_tail == "city" || field_tail.contains("mailcity"))
                && named_location_matches_city_context(&nl_lower, &bare_lower)
            {
                adjusted += 1.05;
            }
            if (field_tail == "county" || field_tail.contains("county_name"))
                && named_location_matches_county_context(&nl_lower, &bare_lower)
            {
                adjusted += 0.85;
            }
            if text_literal_field_tail(&field_tail) {
                let text_match = quoted_phrase_mentions(nl)
                    .into_iter()
                    .chain(named_text_mentions(nl, &field_tail))
                    .any(|value| bare.eq_ignore_ascii_case(&value));
                if text_match {
                    adjusted += 1.15;
                }
            }
            if id_like_field_tail(&field_tail) {
                if id_numeric_mentions(nl)
                    .iter()
                    .any(|value| bare.trim_matches('\'') == value)
                {
                    adjusted += 1.20;
                } else if bare_lower.starts_with("no.") || bare_lower.starts_with("no ") {
                    adjusted -= 0.50;
                }
            }
            if card_number_field_tail(&field_tail)
                && numbered_card_mentions(nl)
                    .iter()
                    .any(|value| bare.trim_matches('\'').eq_ignore_ascii_case(value))
            {
                adjusted += 1.05;
            }
            let numeric_context_values = numeric_mentions_near_field(nl, &field_tail);
            if !numeric_context_values.is_empty()
                && numeric_context_values
                    .iter()
                    .any(|value| bare.trim_matches('\'') == value)
            {
                adjusted += 1.25;
            }
            if field_tail == "year" {
                if four_digit_year_mentions(nl).contains(&bare_lower) {
                    adjusted += 1.20;
                } else if bare.chars().any(|ch| ch.is_ascii_alphabetic()) {
                    adjusted -= 0.75;
                }
            }
            if field_tail.contains("county") && bare_lower.ends_with(" county") {
                if let Some(trimmed) = bare_lower.strip_suffix(" county") {
                    let has_trimmed_peer = candidates
                        .iter()
                        .any(|other| other.trim_matches('\'').eq_ignore_ascii_case(trimmed));
                    if has_trimmed_peer {
                        adjusted -= 0.15;
                    }
                }
            }
            if field_tail.contains("date") || field_tail == "birthday" {
                let nl_has_time = text_mentions_clock_time(nl);
                let candidate_has_time = text_mentions_clock_time(bare);
                if candidate_date_parts(bare).is_some_and(|parts| nl_mentions_date(nl, parts)) {
                    if !nl_has_time || candidate_has_time {
                        adjusted += 1.25;
                    } else {
                        adjusted -= 0.35;
                    }
                }
            }
            adjusted
        })
        .collect()
}

fn apply_selected_value_source_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    candidate_indexes: &[usize],
    predicate_field: Option<&str>,
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@val") || candidates.len() != scores.len() {
        return scores.to_vec();
    }
    let Some(field) = predicate_field else {
        return scores.to_vec();
    };
    let selected_key = canonical_field_key(field);
    if selected_key.is_empty() {
        return scores.to_vec();
    }
    candidates
        .iter()
        .enumerate()
        .zip(scores.iter())
        .map(|((idx, candidate), score)| {
            let original_idx = candidate_indexes.get(idx).copied().unwrap_or(idx);
            let sources = slot
                .candidate_source_fields
                .get(original_idx)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let source_matches = sources
                .iter()
                .any(|source| canonical_field_key(source) == selected_key);
            if source_matches && value_candidate_source_evidence_weight(nl, candidate).is_some() {
                score + SELECTED_VALUE_SOURCE_BIAS
            } else {
                *score
            }
        })
        .collect()
}

fn scored_candidate_source_fields(
    slot: &SlotInput,
    original_idx: usize,
    predicate_field: Option<&str>,
) -> Vec<String> {
    if original_idx == usize::MAX {
        return predicate_field
            .map(|field| vec![field.to_string()])
            .unwrap_or_default();
    }
    slot.candidate_source_fields
        .get(original_idx)
        .cloned()
        .unwrap_or_default()
}

fn apply_between_boundary_value_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@val")
        || candidates.len() != scores.len()
        || slot.predicate_operator.as_deref() != Some("BETWEEN")
    {
        return scores.to_vec();
    }
    let Some(boundary) = between_boundary_for_slot(slot) else {
        return scores.to_vec();
    };
    let mut mentioned_numbers: Vec<f64> = candidates
        .iter()
        .filter(|candidate| value_candidate_mentioned_in_nl(nl, candidate))
        .filter_map(|candidate| parse_numeric_candidate_value(candidate))
        .collect();
    let large_mentions: Vec<f64> = mentioned_numbers
        .iter()
        .copied()
        .filter(|value| value.abs() >= 100.0)
        .collect();
    if large_mentions.len() >= 2 {
        mentioned_numbers = large_mentions;
    }
    if mentioned_numbers.len() < 2 {
        return scores.to_vec();
    }
    let lower = mentioned_numbers
        .iter()
        .copied()
        .fold(f64::INFINITY, f64::min);
    let upper = mentioned_numbers
        .iter()
        .copied()
        .fold(f64::NEG_INFINITY, f64::max);
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            let Some(value) = parse_numeric_candidate_value(candidate) else {
                return *score;
            };
            let is_boundary = match boundary {
                BetweenBoundary::Lower => (value - lower).abs() < f64::EPSILON,
                BetweenBoundary::Upper => (value - upper).abs() < f64::EPSILON,
            };
            if is_boundary {
                score + 0.75
            } else {
                *score
            }
        })
        .collect()
}

fn apply_comparison_mentioned_value_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    predicate_field: Option<&str>,
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@val")
        || candidates.len() != scores.len()
        || !matches!(
            slot.predicate_operator.as_deref(),
            Some(">") | Some("<") | Some(">=") | Some("<=")
        )
    {
        return scores.to_vec();
    }
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            let mentioned = value_candidate_mentioned_in_nl(nl, candidate);
            let numeric_threshold = parse_numeric_candidate_value(candidate).is_some();
            let date_threshold = predicate_field.is_some_and(field_tail_looks_date_like);
            if mentioned && (numeric_threshold || date_threshold) {
                score + 0.55
            } else {
                *score
            }
        })
        .collect()
}

fn field_tail_looks_date_like(field: &str) -> bool {
    let tail = field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(field)
        .to_ascii_lowercase();
    tail.contains("date") || tail.contains("open") || tail.contains("close")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BetweenBoundary {
    Lower,
    Upper,
}

fn between_boundary_for_slot(slot: &SlotInput) -> Option<BetweenBoundary> {
    let upper = slot.context_window.to_ascii_uppercase();
    let slot_upper = slot.slot_name.to_ascii_uppercase();
    let slot_pos = upper.find(&slot_upper)?;
    let before = &upper[..slot_pos];
    let between_pos = before.rfind(" BETWEEN ")?;
    let between_to_slot = &before[between_pos + " BETWEEN ".len()..];
    if between_to_slot.contains(" AND ") {
        Some(BetweenBoundary::Upper)
    } else {
        Some(BetweenBoundary::Lower)
    }
}

fn parse_numeric_candidate_value(candidate: &str) -> Option<f64> {
    let bare = candidate.trim().trim_matches('\'').replace(',', "");
    if bare.is_empty()
        || !bare
            .chars()
            .all(|ch| ch.is_ascii_digit() || ch == '.' || ch == '-' || ch == '+')
    {
        return None;
    }
    bare.parse::<f64>().ok()
}

fn named_location_matches_city_context(nl_lower: &str, value_lower: &str) -> bool {
    if value_lower.is_empty() || VALUE_LEXICAL_STOP.contains(&value_lower) {
        return false;
    }
    nl_lower.contains(&format!("city of {value_lower}"))
        || nl_lower.contains(&format!("in {value_lower} city"))
        || nl_lower.contains(&format!(" in {value_lower}"))
        || nl_lower.contains(&format!(" at {value_lower}"))
        || nl_lower.contains(&format!("located in the city of {value_lower}"))
        || nl_lower.contains(&format!("active in {value_lower} city"))
}

fn named_location_matches_county_context(nl_lower: &str, value_lower: &str) -> bool {
    if value_lower.is_empty() || VALUE_LEXICAL_STOP.contains(&value_lower) {
        return false;
    }
    nl_lower.contains(&format!("{value_lower} county"))
        || nl_lower.contains(&format!("county of {value_lower}"))
        || nl_lower.contains(&format!("located in {value_lower}"))
}

fn person_name_parts(nl: &str) -> Vec<(String, String)> {
    person_name_literal_mentions(nl)
        .into_iter()
        .map(|(first, last)| (first.to_ascii_lowercase(), last.to_ascii_lowercase()))
        .collect()
}

fn person_name_literal_mentions(nl: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    let raw_tokens: Vec<String> = nl
        .split_whitespace()
        .filter_map(clean_person_name_token)
        .filter(|token| token.len() >= 2)
        .collect();
    for pair in raw_tokens.windows(2) {
        let first = pair[0].trim_end_matches("'s");
        let last = pair[1].trim_end_matches("'s");
        let first_is_name = first
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_uppercase());
        let last_is_name = last
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_uppercase());
        let first_lower = first.to_ascii_lowercase();
        let last_lower = last.to_ascii_lowercase();
        if first_is_name
            && last_is_name
            && !PERSON_NAME_STOP
                .iter()
                .any(|stop| stop.eq_ignore_ascii_case(first))
            && !PERSON_NAME_STOP
                .iter()
                .any(|stop| stop.eq_ignore_ascii_case(last))
            && !FIRST_LAST_STOP.contains(&first_lower.as_str())
            && !FIRST_LAST_STOP.contains(&last_lower.as_str())
            && first_lower != last_lower
        {
            push_person_name(&mut out, first, last);
        }
    }
    for triple in raw_tokens.windows(3) {
        let first = triple[0].trim_end_matches("'s");
        let particle = triple[1].trim_end_matches("'s");
        let last = triple[2].trim_end_matches("'s");
        let particle_lower = particle.to_ascii_lowercase();
        let first_is_name = first
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_uppercase());
        let last_is_name = last
            .chars()
            .next()
            .is_some_and(|ch| ch.is_ascii_uppercase());
        if first_is_name
            && last_is_name
            && matches!(
                particle_lower.as_str(),
                "da" | "de" | "del" | "di" | "la" | "le" | "van" | "von"
            )
        {
            let first_lower = first.to_ascii_lowercase();
            let last_lower = last.to_ascii_lowercase();
            if !FIRST_LAST_STOP.contains(&first_lower.as_str())
                && !FIRST_LAST_STOP.contains(&last_lower.as_str())
                && first_lower != last_lower
            {
                push_person_name(&mut out, first, &format!("{particle} {last}"));
            }
        }
    }
    out
}

fn clean_person_name_token(token: &str) -> Option<String> {
    let cleaned = token
        .trim_matches(|ch: char| !(ch.is_ascii_alphabetic() || ch == '\''))
        .trim_end_matches("'s")
        .to_string();
    (!cleaned.is_empty()).then_some(cleaned)
}

fn push_person_name(out: &mut Vec<(String, String)>, first: &str, last: &str) {
    let candidate = (first.to_string(), last.to_string());
    if !out.iter().any(|existing| existing == &candidate) {
        out.push(candidate);
    }
}

fn expected_person_name_for_value_slot<'a>(
    slot: &SlotInput,
    field_tail: &str,
    names: &'a [(String, String)],
) -> Option<(&'a str, bool)> {
    if names.len() < 2 {
        return None;
    }
    let value_position = predicate_value_slot_position(slot)?;
    let pair_index = value_position / 2;
    let (first, last) = names.get(pair_index)?;
    if field_tail.contains("fname") || field_tail.contains("first_name") || field_tail == "forename"
    {
        Some((first.as_str(), true))
    } else if field_tail.contains("lname")
        || field_tail.contains("last_name")
        || field_tail == "surname"
    {
        Some((last.as_str(), false))
    } else {
        None
    }
}

const PERSON_NAME_STOP: &[&str] = &[
    "Among", "And", "Between", "Did", "Give", "How", "In", "List", "Name", "Of", "Please", "Show",
    "State", "Tell", "The", "Was", "What", "When", "Where", "Which", "Who",
];

const FIRST_LAST_STOP: &[&str] = &[
    "national",
    "please",
    "specify",
    "center",
    "educational",
    "statistics",
    "grand",
    "prix",
    "school",
    "schools",
    "county",
    "city",
    "state",
];

const VALUE_LEXICAL_STOP: &[&str] = &[
    "address", "city", "funded", "funding", "model", "type", "school", "schools", "county",
];

fn value_token_matches(nl_token: &str, cand_token: &str) -> bool {
    nl_token == cand_token
        || (nl_token.len() >= 4 && cand_token.starts_with(nl_token))
        || (cand_token.len() >= 4 && nl_token.starts_with(cand_token))
}

fn apply_slot_role_bias(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@field")
        || candidates.len() != scores.len()
        || slot.slot_role == "join_key"
    {
        return scores.to_vec();
    }
    let nl_tokens = role_content_tokens(nl);
    if nl_tokens.is_empty() {
        return scores.to_vec();
    }
    let best_score = scores
        .iter()
        .copied()
        .filter(|score| !score.is_nan())
        .fold(f32::NEG_INFINITY, f32::max);
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            if score.is_nan() {
                return *score;
            }
            let tail = candidate
                .rsplit_once('.')
                .map(|(_, tail)| tail)
                .unwrap_or(candidate);
            let projection_bonus = projection_field_role_bonus(nl, slot, tail);
            let order_bonus = order_field_role_bonus(nl, slot.slot_role.as_str(), tail);
            let count_bonus = count_projection_field_role_bonus(slot, tail);
            let numeric_predicate_bonus = numeric_predicate_field_role_bonus(slot, tail);
            let predicate_phrase_bonus = predicate_field_phrase_role_bonus(nl, slot, tail);
            let person_name_bonus = person_name_predicate_role_bonus(nl, slot, tail);
            if *score + 0.08 < best_score
                && projection_bonus == 0.0
                && order_bonus == 0.0
                && count_bonus == 0.0
                && numeric_predicate_bonus == 0.0
                && predicate_phrase_bonus == 0.0
                && person_name_bonus == 0.0
            {
                return *score;
            }
            let tail_tokens = role_content_tokens(&tail.replace('_', " "));
            let overlap = tail_tokens
                .iter()
                .filter(|token| nl_tokens.iter().any(|nl_token| nl_token == *token))
                .count();
            let tail_norm = normalised_alnum(tail);
            let nl_norm = normalised_alnum(nl);
            let phrase_bonus = if tail_norm.len() >= 4 && nl_norm.contains(&tail_norm) {
                0.02
            } else {
                0.0
            };
            let overlap_bonus = (overlap as f32 * 0.01).min(0.03);
            score
                + phrase_bonus
                + overlap_bonus
                + projection_bonus
                + order_bonus
                + count_bonus
                + numeric_predicate_bonus
                + predicate_phrase_bonus
                + person_name_bonus
        })
        .collect()
}

fn apply_field_reuse_penalty(
    nl: &str,
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    used_content_fields: &std::collections::BTreeSet<String>,
    used_join_fields: &std::collections::BTreeSet<String>,
) -> Vec<f32> {
    if !slot.slot_name.starts_with("@field")
        || candidates.len() != scores.len()
        || slot.slot_role == "join_key"
    {
        return scores.to_vec();
    }
    let filter_join_endpoints =
        !slot_allows_join_endpoint_content(nl) && !slot_is_count_projection(slot);
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            let mut adjusted = *score;
            if used_content_fields.contains(candidate.as_str()) {
                adjusted -= FIELD_REUSE_PENALTY;
            }
            if filter_join_endpoints && used_join_fields.contains(candidate.as_str()) {
                adjusted -= JOIN_ENDPOINT_CONTENT_PENALTY;
            }
            adjusted
        })
        .collect()
}

fn predicate_value_evidence_by_field_slot(
    nl: &str,
    slots: &[SlotInput],
) -> std::collections::BTreeMap<String, std::collections::BTreeMap<String, f32>> {
    let predicate_value_slot_count = slots
        .iter()
        .filter(|slot| slot.slot_name.starts_with("@val") && slot.predicate_field_slot.is_some())
        .count();
    let mut raw: std::collections::BTreeMap<String, std::collections::BTreeMap<String, f32>> =
        std::collections::BTreeMap::new();
    let mut field_key_slots: std::collections::BTreeMap<
        String,
        std::collections::BTreeSet<String>,
    > = std::collections::BTreeMap::new();
    for slot in slots {
        if !slot.slot_name.starts_with("@val") {
            continue;
        }
        let Some(field_slot) = slot.predicate_field_slot.as_ref() else {
            continue;
        };
        for (idx, candidate) in slot.candidates.iter().enumerate() {
            let Some(weight) = value_candidate_source_evidence_weight(nl, candidate) else {
                continue;
            };
            let sources = slot
                .candidate_source_fields
                .get(idx)
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            if sources.is_empty() {
                continue;
            }
            let by_field = raw.entry(field_slot.clone()).or_default();
            for source in sources {
                if !source_field_allows_predicate_value_evidence(source, candidate) {
                    continue;
                }
                let key = canonical_field_key(source);
                if key.is_empty() {
                    continue;
                }
                let entry = by_field.entry(key.clone()).or_insert(0.0);
                *entry = (*entry).max(weight);
                field_key_slots
                    .entry(key)
                    .or_default()
                    .insert(field_slot.clone());
            }
        }
    }
    if predicate_value_slot_count <= 1 {
        return raw;
    }
    let mut filtered: std::collections::BTreeMap<String, std::collections::BTreeMap<String, f32>> =
        std::collections::BTreeMap::new();
    for (field_slot, fields) in raw {
        let retained: std::collections::BTreeMap<String, f32> = fields
            .into_iter()
            .filter(|(field_key, _)| {
                field_key_slots
                    .get(field_key)
                    .is_some_and(|owners| owners.len() == 1 && owners.contains(&field_slot))
            })
            .collect();
        if !retained.is_empty() {
            filtered.insert(field_slot, retained);
        }
    }
    filtered
}

fn source_field_allows_predicate_value_evidence(source: &str, candidate: &str) -> bool {
    let bare = candidate.trim().trim_matches('\'').replace("''", "'");
    if bare.is_empty() {
        return false;
    }
    let tail = source
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(source)
        .to_ascii_lowercase();
    if tail == "type" {
        return false;
    }
    let has_alpha = bare.chars().any(|ch| ch.is_ascii_alphabetic());
    if has_alpha {
        return true;
    }
    let has_digit = bare.chars().any(|ch| ch.is_ascii_digit());
    if !has_digit {
        return false;
    }
    let has_structural_punct = bare.contains('-') || bare.contains('/');
    let leading_zero_code = bare.len() > 1
        && bare.starts_with('0')
        && bare.chars().all(|ch| ch.is_ascii_alphanumeric());
    let field_is_numeric_code = tail.contains("code")
        || tail == "soc"
        || tail == "doc"
        || tail.ends_with("code")
        || tail.ends_with("_cd");
    let field_is_structured_code = tail.contains("zip")
        || tail.contains("code")
        || tail.contains("date")
        || tail.contains("year")
        || field_is_numeric_code;
    let short_numeric_code = field_is_numeric_code
        && (2..=6).contains(&bare.len())
        && bare.chars().all(|ch| ch.is_ascii_digit());
    field_is_structured_code && (has_structural_punct || leading_zero_code || short_numeric_code)
}

fn value_candidate_source_evidence_weight(nl: &str, candidate: &str) -> Option<f32> {
    if value_candidate_mentioned_in_nl(nl, candidate) {
        return Some(0.85);
    }
    let bare = candidate.trim().trim_matches('\'').replace("''", "'");
    if bare.is_empty() {
        return None;
    }
    let norm = normalised_alnum(&bare);
    if norm.len() < 3 {
        return None;
    }
    let exact_literal = nl_mentions_literal(nl, &bare);
    let has_alpha = bare.chars().any(|ch| ch.is_ascii_alphabetic());
    let has_digit = bare.chars().any(|ch| ch.is_ascii_digit());
    let has_structural_punct = bare.contains('-') || bare.contains('/');
    let leading_zero_code = bare.len() > 1
        && bare.starts_with('0')
        && bare.chars().all(|ch| ch.is_ascii_alphanumeric());
    if exact_literal && has_digit && (has_structural_punct || leading_zero_code || has_alpha) {
        return Some(0.85);
    }
    if !has_alpha {
        return None;
    }
    let lower = bare.to_ascii_lowercase();
    if VALUE_LEXICAL_STOP.contains(&lower.as_str()) {
        return None;
    }
    if exact_literal {
        return Some(0.70);
    }
    let nl_tokens = role_content_tokens(nl);
    let cand_tokens: Vec<String> = role_content_tokens(&bare)
        .into_iter()
        .filter(|token| !VALUE_LEXICAL_STOP.contains(&token.as_str()))
        .collect();
    if cand_tokens.is_empty() {
        return None;
    }
    let matched = cand_tokens
        .iter()
        .filter(|cand_token| {
            nl_tokens
                .iter()
                .any(|nl_token| value_token_matches(nl_token, cand_token))
        })
        .count();
    let has_distinctive_match = cand_tokens.iter().any(|cand_token| {
        cand_token.len() >= 4
            && nl_tokens
                .iter()
                .any(|nl_token| value_token_matches(nl_token, cand_token))
    });
    let coverage = matched as f32 / cand_tokens.len() as f32;
    (has_distinctive_match && coverage >= 0.5).then_some(0.45)
}

fn apply_predicate_value_source_field_bias(
    slot: &SlotInput,
    candidates: &[String],
    scores: &[f32],
    evidence: Option<&std::collections::BTreeMap<String, f32>>,
) -> Vec<f32> {
    if slot.slot_role != "predicate_field" || candidates.len() != scores.len() {
        return scores.to_vec();
    }
    let Some(evidence) = evidence else {
        return scores.to_vec();
    };
    if evidence.is_empty() {
        return scores.to_vec();
    }
    candidates
        .iter()
        .zip(scores.iter())
        .map(|(candidate, score)| {
            let key = canonical_field_key(candidate);
            let weight = evidence.get(&key).copied().unwrap_or(0.0);
            score + (PREDICATE_VALUE_SOURCE_FIELD_BIAS * weight)
        })
        .collect()
}

fn canonical_field_key(field: &str) -> String {
    normalised_alnum(field)
}

fn projection_field_role_bonus(nl: &str, slot: &SlotInput, tail: &str) -> f32 {
    if slot.slot_role != "projection_field" {
        return 0.0;
    }
    let nl_lower = nl.to_ascii_lowercase();
    let address_bonus = full_address_projection_role_bonus(&nl_lower, slot, tail);
    let contact_bonus = contact_projection_role_bonus(&nl_lower, slot, tail);
    let website_bonus = website_projection_role_bonus(&nl_lower, tail);
    let tail_lower = tail.to_ascii_lowercase();
    let date_bonus =
        if (nl_lower.contains("when") || nl_lower.contains("opened") || nl_lower.contains("open"))
            && (tail_lower.contains("date")
                || tail_lower.contains("open")
                || tail_lower.contains("close"))
        {
            0.75
        } else {
            0.0
        };
    address_bonus
        .max(contact_bonus)
        .max(website_bonus)
        .max(date_bonus)
}

fn website_projection_role_bonus(nl_lower: &str, tail: &str) -> f32 {
    let lower = tail.to_ascii_lowercase();
    if (nl_lower.contains("website") || nl_lower.contains("webpage"))
        && (lower.contains("website") || lower.contains("webpage") || lower == "url")
    {
        1.05
    } else {
        0.0
    }
}

fn contact_projection_role_bonus(nl_lower: &str, slot: &SlotInput, tail: &str) -> f32 {
    if !(nl_lower.contains("phone")
        && (nl_lower.contains("extension") || nl_lower.contains(" ext"))
        && nl_lower.contains("name"))
    {
        return 0.0;
    }
    let Some(position) = projection_slot_position(slot) else {
        return 0.0;
    };
    let lower = tail.to_ascii_lowercase();
    let matched = match position {
        0 => lower == "phone" || lower.contains("phone"),
        1 => lower == "ext" || lower.contains("extension"),
        2 => field_tail_is_name(tail),
        _ => false,
    };
    if matched {
        1.05
    } else {
        0.0
    }
}

fn full_address_projection_role_bonus(nl_lower: &str, slot: &SlotInput, tail: &str) -> f32 {
    if !full_address_projection_requested(nl_lower) {
        return 0.0;
    }
    let Some(position) = projection_slot_position(slot) else {
        return 0.0;
    };
    let asks_for_name = nl_lower.contains("name") || nl_lower.contains("names");
    let address_position = if asks_for_name {
        if position == 0 {
            return if field_tail_is_name(tail) { 0.85 } else { 0.0 };
        }
        position - 1
    } else {
        position
    };
    let expected = match address_position {
        0 => AddressProjectionPart::Street,
        1 => AddressProjectionPart::City,
        2 => AddressProjectionPart::State,
        3 => AddressProjectionPart::Zip,
        _ => return 0.0,
    };
    if field_tail_matches_address_part(tail, expected) {
        0.95
    } else {
        0.0
    }
}

#[derive(Clone, Copy)]
enum AddressProjectionPart {
    Street,
    City,
    State,
    Zip,
}

fn full_address_projection_requested(nl_lower: &str) -> bool {
    nl_lower.contains("full communication address")
        || nl_lower.contains("communication address")
        || nl_lower.contains("full address")
}

fn projection_slot_position(slot: &SlotInput) -> Option<usize> {
    let upper = slot.context_window.to_ascii_uppercase();
    let select_pos = upper.find("SELECT ")?;
    let from_pos = upper.find(" FROM ")?;
    if from_pos <= select_pos {
        return None;
    }
    let select_list = &slot.context_window[select_pos + "SELECT ".len()..from_pos];
    select_list
        .split(',')
        .position(|part| part.split_whitespace().any(|token| token == slot.slot_name))
}

fn predicate_slot_position(slot: &SlotInput) -> Option<usize> {
    let upper = slot.context_window.to_ascii_uppercase();
    let where_pos = upper.find(" WHERE ")?;
    let where_clause = &slot.context_window[where_pos + " WHERE ".len()..];
    let mut seen: Vec<&str> = Vec::new();
    for token in where_clause
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '@'))
        .filter(|token| token.starts_with("@field"))
    {
        if !seen.contains(&token) {
            seen.push(token);
        }
        if token == slot.slot_name {
            return seen.iter().position(|seen_token| *seen_token == token);
        }
    }
    None
}

fn predicate_value_slot_position(slot: &SlotInput) -> Option<usize> {
    let upper = slot.context_window.to_ascii_uppercase();
    let where_pos = upper.find(" WHERE ")?;
    let where_clause = &slot.context_window[where_pos + " WHERE ".len()..];
    let mut seen: Vec<&str> = Vec::new();
    for token in where_clause
        .split(|ch: char| !(ch.is_ascii_alphanumeric() || ch == '@'))
        .filter(|token| token.starts_with("@val"))
    {
        if !seen.contains(&token) {
            seen.push(token);
        }
        if token == slot.slot_name {
            return seen.iter().position(|seen_token| *seen_token == token);
        }
    }
    None
}

fn field_tail_matches_address_part(tail: &str, part: AddressProjectionPart) -> bool {
    let lower = tail.to_ascii_lowercase();
    match part {
        AddressProjectionPart::Street => lower.contains("street") && !lower.contains("abr"),
        AddressProjectionPart::City => lower == "city" || lower.contains("city"),
        AddressProjectionPart::State => lower == "state" || lower.ends_with("_state"),
        AddressProjectionPart::Zip => {
            lower.contains("zip") || lower.contains("postal") || lower.contains("postcode")
        }
    }
}

fn field_tail_is_name(tail: &str) -> bool {
    let lower = tail.to_ascii_lowercase();
    lower.ends_with("_name") || matches!(lower.as_str(), "name" | "title" | "label")
}

fn order_field_role_bonus(nl: &str, role: &str, tail: &str) -> f32 {
    if role != "order_field" {
        return 0.0;
    }
    let nl_lower = nl.to_ascii_lowercase();
    let tail_lower = tail.to_ascii_lowercase();
    if (nl_lower.contains(" first ") || nl_lower.starts_with("who was the first"))
        && (nl_lower.contains("paid") || nl_lower.contains("received"))
        && (tail_lower.contains("date_received") || tail_lower.contains("date"))
    {
        return 0.95;
    }
    if ![
        "highest", "largest", "most", "top", "lowest", "fewest", "smallest",
    ]
    .iter()
    .any(|needle| nl_lower.contains(needle))
    {
        return 0.0;
    }
    let nl_tokens = role_content_tokens(nl);
    let tail_tokens = role_content_tokens(&tail.replace('_', " "));
    if tail_tokens
        .iter()
        .any(|token| nl_tokens.iter().any(|nl_token| nl_token == token))
    {
        0.45
    } else {
        0.0
    }
}

fn numeric_predicate_field_role_bonus(slot: &SlotInput, tail: &str) -> f32 {
    if slot.slot_role != "predicate_field"
        || !comparison_operator_wants_numeric(slot.predicate_operator.as_deref())
    {
        return 0.0;
    }
    if field_tail_looks_numeric(tail) {
        0.75
    } else {
        0.0
    }
}

fn predicate_field_phrase_role_bonus(nl: &str, slot: &SlotInput, tail: &str) -> f32 {
    if slot.slot_role != "predicate_field" {
        return 0.0;
    }
    let nl_lower = nl.to_ascii_lowercase();
    let tail_lower = tail.to_ascii_lowercase();
    if tail_lower == "department" && nl_lower.contains("department") {
        return 0.95;
    }
    if (tail_lower.contains("date") || tail_lower.contains("open"))
        && comparison_operator_wants_numeric(slot.predicate_operator.as_deref())
        && (nl_lower.contains("after")
            || nl_lower.contains("before")
            || nl_lower.contains("opened")
            || nl_lower.contains("open date"))
    {
        return 0.95;
    }
    if tail_lower == "phone"
        && (nl_lower.contains("phone number")
            || nl_lower.contains("telephone")
            || nl_lower.contains("where phone")
            || nl_lower.contains("whose phone"))
    {
        return 0.85;
    }
    if (tail_lower == "county" || tail_lower.contains("county_name"))
        && (nl_lower.contains(" county") || nl_lower.contains("county of "))
    {
        return 0.45;
    }
    if (tail_lower == "district" || tail_lower.contains("district_name"))
        && nl_lower.contains("district")
    {
        return 0.45;
    }
    if (tail_lower == "status" || tail_lower.ends_with("_status") || tail_lower.contains("status"))
        && (phrase_is_mentioned(&nl_lower, "active")
            || phrase_is_mentioned(&nl_lower, "inactive")
            || phrase_is_mentioned(&nl_lower, "closed")
            || phrase_is_mentioned(&nl_lower, "merged"))
    {
        return 0.70;
    }
    if (tail_lower == "street" || tail_lower.contains("street") || tail_lower.contains("address"))
        && (nl_lower.contains("mailing street") || nl_lower.contains("po box"))
    {
        return 0.55;
    }
    if tail_lower.contains("nationality")
        && (nl_lower.contains("nationality") || nl_lower.contains("nationalities"))
    {
        return 0.55;
    }
    if matches!(
        tail_lower.as_str(),
        "dob" | "date_of_birth" | "birthdate" | "birth_date"
    ) && (nl_lower.contains(" born ") || nl_lower.contains("birth"))
    {
        return 0.75;
    }
    if tail_lower.contains("gender") && (nl_lower.contains("male") || nl_lower.contains("female")) {
        return 0.45;
    }
    if tail_lower == "year" && !four_digit_year_mentions(nl).is_empty() {
        return 0.65;
    }
    0.0
}

fn person_name_predicate_role_bonus(nl: &str, slot: &SlotInput, tail: &str) -> f32 {
    if slot.slot_role != "predicate_field" || person_name_parts(nl).is_empty() {
        return 0.0;
    }
    let Some(position) = predicate_slot_position(slot) else {
        return 0.0;
    };
    let lower = tail.to_ascii_lowercase();
    let matched = match position {
        0 => matches!(lower.as_str(), "first_name" | "firstname"),
        1 => matches!(lower.as_str(), "last_name" | "lastname"),
        _ => false,
    };
    if matched {
        0.95
    } else {
        0.0
    }
}

fn comparison_operator_wants_numeric(op: Option<&str>) -> bool {
    matches!(
        op,
        Some(">") | Some("<") | Some(">=") | Some("<=") | Some("BETWEEN")
    )
}

fn field_tail_looks_numeric(tail: &str) -> bool {
    let lower = tail.to_ascii_lowercase();
    let tokens: Vec<&str> = lower
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| !token.is_empty())
        .collect();
    tokens.iter().any(|token| {
        matches!(
            *token,
            "amount"
                | "avg"
                | "average"
                | "count"
                | "date"
                | "duration"
                | "grade"
                | "latitude"
                | "longitude"
                | "meal"
                | "number"
                | "percent"
                | "rate"
                | "ratio"
                | "salary"
                | "score"
                | "total"
                | "year"
        )
    }) || ["enroll", "num", "tsttakr", "scr", "latitude", "longitude"]
        .iter()
        .any(|needle| lower.contains(needle))
}

fn slot_is_count_projection(slot: &SlotInput) -> bool {
    slot.slot_role == "projection_field"
        && slot.context_window.to_ascii_uppercase().contains("COUNT(")
}

fn count_projection_field_role_bonus(slot: &SlotInput, tail: &str) -> f32 {
    if !slot_is_count_projection(slot) {
        return 0.0;
    }
    let tail_lower = tail.to_ascii_lowercase();
    if tail_lower == "id" || tail_lower.ends_with("_id") {
        1.35
    } else if tail_lower.ends_with("code")
        || tail_lower.ends_with("_code")
        || tail_lower.contains("identifier")
    {
        0.75
    } else if tail_lower.contains("number") || tail_lower.contains("num") {
        0.20
    } else {
        0.0
    }
}

fn slot_allows_join_endpoint_content(nl: &str) -> bool {
    let tokens = role_content_tokens(nl);
    if tokens
        .iter()
        .any(|token| matches!(token.as_str(), "identifier" | "identification"))
    {
        return true;
    }
    let zip_like = tokens
        .iter()
        .any(|token| matches!(token.as_str(), "zip" | "postal" | "postcode"));
    !zip_like
        && tokens
            .iter()
            .any(|token| matches!(token.as_str(), "code" | "codes" | "id" | "ids"))
}

fn role_content_tokens(text: &str) -> Vec<String> {
    const STOP: &[&str] = &[
        "the", "and", "for", "with", "from", "that", "this", "are", "is", "was", "were", "please",
        "list", "show", "give", "what", "which", "who", "whose", "where", "when",
    ];
    text.split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter_map(|raw| {
            let token = raw.trim().to_ascii_lowercase();
            ((!token.is_empty())
                && (token.len() >= 2 || token.chars().all(|ch| ch.is_ascii_digit()))
                && !STOP.contains(&token.as_str()))
            .then_some(token)
        })
        .collect()
}

fn normalised_alnum(text: &str) -> String {
    text.chars()
        .filter(|ch| ch.is_ascii_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn boolean_like_field(field: &str) -> bool {
    let tail = field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(field)
        .to_ascii_lowercase();
    tail.ends_with("_y_n")
        || tail.ends_with("_yn")
        || tail.starts_with("is_")
        || tail.starts_with("has_")
        || tail.ends_with("_flag")
        || tail == "active"
}

fn value_lexical_bias_skip_field(field: &str) -> bool {
    if boolean_like_field(field) {
        return true;
    }
    let tail = field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(field)
        .to_ascii_lowercase();
    tail.contains("frequency")
        || tail.contains("gender")
        || matches!(tail.as_str(), "year" | "time")
}

fn boolean_preference_is_true(nl: &str) -> bool {
    let lower = format!(" {} ", nl.to_ascii_lowercase().replace('-', " "));
    !(lower.contains(" not ")
        || lower.contains(" non ")
        || lower.contains(" inactive ")
        || lower.contains(" closed ")
        || lower.contains(" false ")
        || lower.contains(" without ")
        || lower.contains(" no "))
}

/// Substitute every `@slot_name` occurrence in `skeleton` with the
/// matching candidate value. Repeats with the same `@slot_name` are
/// honoured — a slot used twice in the skeleton (rare but legal in
/// some NatSQL outputs) gets the same value at both sites.
pub fn substitute_slots(skeleton: &str, picks: &[(&str, &str)]) -> String {
    // Sort by slot-name length DESC so `@field10` substitutes before
    // `@field1` — the latter would otherwise eat the prefix of the
    // former. NatSQL slot indices are typically <10 but let's be
    // robust.
    let mut sorted: Vec<&(&str, &str)> = picks.iter().collect();
    sorted.sort_by_key(|entry| std::cmp::Reverse(entry.0.len()));
    let mut out = skeleton.to_string();
    for (slot, value) in sorted {
        out = out.replace(slot, value);
    }
    out
}

// ---------------------------------------------------------------------------
// Model-backed slot filler — gated on `onnx` feature
// ---------------------------------------------------------------------------

/// Model-backed Stage 3 slot filler. Available only with
/// `--features onnx`. Default-build callers use the deterministic
/// helpers above to construct slot picks themselves.
#[cfg(feature = "onnx")]
pub struct SlotFiller {
    model: OnnxCrossEncoder,
    /// Optional per-call abstain threshold. Defaults to
    /// [`ABSTAIN_THRESHOLD`]. Configurable for ablations.
    pub abstain_threshold: f32,
    /// Bias for intent-hint matches. Defaults to
    /// [`INTENT_PREFERENCE_BIAS`].
    pub intent_bias: f32,
}

#[cfg(feature = "onnx")]
impl SlotFiller {
    /// Load a slot-filler model from the manifest's stage artifact.
    pub fn load(onnx_path: &Path, tokenizer_path: &Path) -> Result<Self> {
        let model = OnnxCrossEncoder::load(onnx_path, tokenizer_path)?;
        Ok(Self {
            model,
            abstain_threshold: ABSTAIN_THRESHOLD,
            intent_bias: INTENT_PREFERENCE_BIAS,
        })
    }

    /// Resolve every slot in `skeleton` against `slots`. Skeleton
    /// substitution happens here too — the caller gets a concrete
    /// NatSQL string ready for the Stage 4 transpiler.
    pub fn fill(&self, nl: &str, skeleton: &str, slots: &[SlotInput]) -> Result<SlotFillerOutput> {
        if slots.is_empty() {
            return Ok(SlotFillerOutput {
                concrete_natsql: skeleton.to_string(),
                mean_confidence: 1.0,
                escalations: Vec::new(),
                decisions: Vec::new(),
            });
        }

        let mut picks: Vec<(String, String)> = Vec::with_capacity(slots.len());
        let mut escalations: Vec<String> = Vec::new();
        let mut decisions: Vec<SlotDecision> = Vec::with_capacity(slots.len());
        let mut score_sum = 0.0f32;
        let mut score_count = 0usize;
        let mut used_entities: std::collections::BTreeSet<String> =
            std::collections::BTreeSet::new();
        let mut used_join_fields: std::collections::BTreeSet<String> =
            std::collections::BTreeSet::new();
        let mut used_content_fields: std::collections::BTreeSet<String> =
            std::collections::BTreeSet::new();
        let mut pick_map: std::collections::BTreeMap<String, String> =
            std::collections::BTreeMap::new();
        let predicate_value_evidence = predicate_value_evidence_by_field_slot(nl, slots);

        // Resolution order follows slot semantics, not placeholder numbering.
        //
        // Join fields are resolved before content fields so FK endpoints can
        // be removed from ordinary projection/filter/order slots. ORDER/GROUP
        // fields are resolved before predicate fields because superlative
        // measures often appear in the NL before filter adjectives, and the
        // old skeleton-order pass let predicates consume those measure fields.
        let mut ordered: Vec<usize> = (0..slots.len()).collect();
        ordered.sort_by_key(|&i| slot_resolution_rank(&slots[i]));

        // Working skeleton that grows more concrete as entity/field/value slots
        // resolve. Cross-encoder context is rebuilt from this each pass.
        let mut working_skeleton = skeleton.to_string();

        for &i in &ordered {
            let slot = &slots[i];
            let original_candidate_count = slot.candidates.len();
            let mut candidate_indexes: Vec<usize> = (0..slot.candidates.len()).collect();
            let mut candidates = slot.candidates.clone();
            if candidates.is_empty() && !slot.slot_name.starts_with("@val") {
                escalations.push(slot.slot_name.clone());
                decisions.push(SlotDecision {
                    slot_name: slot.slot_name.clone(),
                    slot_kind: slot_kind(&slot.slot_name).to_string(),
                    slot_role: slot.slot_role.clone(),
                    context_skeleton: working_skeleton.clone(),
                    context_window: slot.context_window.clone(),
                    predicate_field_slot: slot.predicate_field_slot.clone(),
                    predicate_operator: slot.predicate_operator.clone(),
                    predicate_field: None,
                    original_candidate_count: 0,
                    candidates: Vec::new(),
                    picked: None,
                    picked_index: None,
                    escalated: true,
                });
                continue;
            }
            if slot.slot_name.starts_with("@entity") {
                let filtered: Vec<(usize, String)> = candidates
                    .iter()
                    .enumerate()
                    .filter(|(_, c)| !used_entities.contains(c.as_str()))
                    .map(|(idx, c)| (idx, c.clone()))
                    .collect();
                if !filtered.is_empty() {
                    candidate_indexes = filtered.iter().map(|(idx, _)| *idx).collect();
                    candidates = filtered.into_iter().map(|(_, c)| c).collect();
                }
            } else if slot.slot_name.starts_with("@field") && slot.slot_role == "join_key" {
                let filtered: Vec<(usize, String)> = candidates
                    .iter()
                    .enumerate()
                    .filter(|(_, c)| !used_join_fields.contains(c.as_str()))
                    .map(|(idx, c)| (idx, c.clone()))
                    .collect();
                if !filtered.is_empty() {
                    candidate_indexes = filtered.iter().map(|(idx, _)| *idx).collect();
                    candidates = filtered.into_iter().map(|(_, c)| c).collect();
                }
            } else if slot.slot_name.starts_with("@field") {
                let filter_join_endpoints =
                    !slot_allows_join_endpoint_content(nl) && !slot_is_count_projection(slot);
                let (retained_indexes, retained_candidates) =
                    softly_retain_filtered_field_candidates(
                        &candidate_indexes,
                        &candidates,
                        &used_content_fields,
                        &used_join_fields,
                        filter_join_endpoints,
                    );
                candidate_indexes = retained_indexes;
                candidates = retained_candidates;
            } else if slot.slot_name.starts_with("@val") {
                if let Some(field_slot) = &slot.predicate_field_slot {
                    if let Some(selected_field) = pick_map.get(field_slot) {
                        let matching_source_indexes: std::collections::BTreeSet<usize> =
                            candidate_indexes
                                .iter()
                                .enumerate()
                                .filter_map(|(idx, original_idx)| {
                                    let sources = slot
                                        .candidate_source_fields
                                        .get(*original_idx)
                                        .map(Vec::as_slice)
                                        .unwrap_or(&[]);
                                    sources
                                        .iter()
                                        .any(|source| source.eq_ignore_ascii_case(selected_field))
                                        .then_some(idx)
                                })
                                .collect();
                        let strict_source_match = !matching_source_indexes.is_empty();
                        let filtered: Vec<(usize, String)> = candidates
                            .iter()
                            .enumerate()
                            .filter(|(idx, _)| {
                                let original_idx =
                                    candidate_indexes.get(*idx).copied().unwrap_or(*idx);
                                let sources = slot
                                    .candidate_source_fields
                                    .get(original_idx)
                                    .map(Vec::as_slice)
                                    .unwrap_or(&[]);
                                if strict_source_match {
                                    matching_source_indexes.contains(idx)
                                        || keep_source_empty_value_candidate(
                                            nl,
                                            slot,
                                            selected_field,
                                            sources,
                                            candidates.get(*idx).map(String::as_str).unwrap_or(""),
                                        )
                                } else {
                                    sources.is_empty()
                                        || sources.iter().any(|source| {
                                            source.eq_ignore_ascii_case(selected_field)
                                        })
                                }
                            })
                            .map(|(idx, c)| {
                                (
                                    candidate_indexes.get(idx).copied().unwrap_or(idx),
                                    c.clone(),
                                )
                            })
                            .collect();
                        if !filtered.is_empty() {
                            candidate_indexes = filtered.iter().map(|(idx, _)| *idx).collect();
                            candidates = filtered.into_iter().map(|(_, c)| c).collect();
                        }
                        augment_value_candidates_for_selected_field(
                            nl,
                            slot,
                            selected_field,
                            &mut candidate_indexes,
                            &mut candidates,
                        );
                    }
                }
                cap_candidates(
                    &mut candidate_indexes,
                    &mut candidates,
                    MAX_VALUE_SCORING_CANDIDATES,
                );
            }
            if candidates.is_empty() {
                escalations.push(slot.slot_name.clone());
                decisions.push(SlotDecision {
                    slot_name: slot.slot_name.clone(),
                    slot_kind: slot_kind(&slot.slot_name).to_string(),
                    slot_role: slot.slot_role.clone(),
                    context_skeleton: working_skeleton.clone(),
                    context_window: slot.context_window.clone(),
                    predicate_field_slot: slot.predicate_field_slot.clone(),
                    predicate_operator: slot.predicate_operator.clone(),
                    predicate_field: slot
                        .predicate_field_slot
                        .as_ref()
                        .and_then(|field_slot| pick_map.get(field_slot))
                        .cloned(),
                    original_candidate_count,
                    candidates: Vec::new(),
                    picked: None,
                    picked_index: None,
                    escalated: true,
                });
                continue;
            }
            let context_skeleton = working_skeleton.clone();
            let pairs: Vec<(String, String)> = candidates
                .iter()
                .map(|c| {
                    (
                        nl.to_string(),
                        format!("slot {} in [{}]: {}", slot.slot_name, context_skeleton, c),
                    )
                })
                .collect();
            let scores = self.model.score_batch(&pairs)?;
            let predicate_field = slot
                .predicate_field_slot
                .as_ref()
                .and_then(|field_slot| pick_map.get(field_slot))
                .cloned();
            let biased =
                apply_intent_preference(&candidates, &scores, &slot.intent_hints, self.intent_bias);
            let biased = apply_slot_role_bias(nl, slot, &candidates, &biased);
            let biased = apply_field_reuse_penalty(
                nl,
                slot,
                &candidates,
                &biased,
                &used_content_fields,
                &used_join_fields,
            );
            let biased = apply_predicate_value_source_field_bias(
                slot,
                &candidates,
                &biased,
                predicate_value_evidence.get(&slot.slot_name),
            );
            let biased =
                apply_value_role_bias(nl, slot, &candidates, &biased, predicate_field.as_deref());
            let biased =
                apply_value_field_bias(nl, slot, &candidates, &biased, predicate_field.as_deref());
            let biased = apply_value_lexical_bias(
                nl,
                slot,
                &candidates,
                &biased,
                predicate_field.as_deref(),
            );
            let biased = apply_selected_value_source_bias(
                nl,
                slot,
                &candidates,
                &biased,
                &candidate_indexes,
                predicate_field.as_deref(),
            );
            let biased = apply_comparison_mentioned_value_bias(
                nl,
                slot,
                &candidates,
                &biased,
                predicate_field.as_deref(),
            );
            let biased = apply_between_boundary_value_bias(nl, slot, &candidates, &biased);
            let best = pick_best_index(&biased);
            let scored_candidates: Vec<SlotDecisionCandidate> = candidates
                .iter()
                .enumerate()
                .map(|(idx, value)| {
                    let original_idx = candidate_indexes.get(idx).copied().unwrap_or(idx);
                    SlotDecisionCandidate {
                        value: value.clone(),
                        score: scores.get(idx).copied().unwrap_or(0.0),
                        biased_score: biased.get(idx).copied().unwrap_or(0.0),
                        source_fields: scored_candidate_source_fields(
                            slot,
                            original_idx,
                            predicate_field.as_deref(),
                        ),
                    }
                })
                .collect();
            match best {
                Some(idx) => {
                    let picked = candidates[idx].clone();
                    // Substitute into the working skeleton so subsequent
                    // slots score against the more-concrete context.
                    working_skeleton = substitute_slots(
                        &working_skeleton,
                        &[(slot.slot_name.as_str(), picked.as_str())],
                    );
                    picks.push((slot.slot_name.clone(), picked));
                    pick_map.insert(slot.slot_name.clone(), candidates[idx].clone());
                    if slot.slot_name.starts_with("@entity") {
                        used_entities.insert(candidates[idx].clone());
                    } else if slot.slot_name.starts_with("@field") && slot.slot_role == "join_key" {
                        used_join_fields.insert(candidates[idx].clone());
                    } else if slot.slot_name.starts_with("@field") {
                        used_content_fields.insert(candidates[idx].clone());
                    }
                    score_sum += scores[idx];
                    score_count += 1;
                    let escalated = scores[idx] < self.abstain_threshold;
                    if scores[idx] < self.abstain_threshold {
                        escalations.push(slot.slot_name.clone());
                    }
                    decisions.push(SlotDecision {
                        slot_name: slot.slot_name.clone(),
                        slot_kind: slot_kind(&slot.slot_name).to_string(),
                        slot_role: slot.slot_role.clone(),
                        context_skeleton: context_skeleton.clone(),
                        context_window: slot.context_window.clone(),
                        predicate_field_slot: slot.predicate_field_slot.clone(),
                        predicate_operator: slot.predicate_operator.clone(),
                        predicate_field,
                        original_candidate_count,
                        candidates: scored_candidates,
                        picked: Some(candidates[idx].clone()),
                        picked_index: Some(idx),
                        escalated,
                    });
                }
                _ => {
                    escalations.push(slot.slot_name.clone());
                    decisions.push(SlotDecision {
                        slot_name: slot.slot_name.clone(),
                        slot_kind: slot_kind(&slot.slot_name).to_string(),
                        slot_role: slot.slot_role.clone(),
                        context_skeleton,
                        context_window: slot.context_window.clone(),
                        predicate_field_slot: slot.predicate_field_slot.clone(),
                        predicate_operator: slot.predicate_operator.clone(),
                        predicate_field,
                        original_candidate_count,
                        candidates: scored_candidates,
                        picked: None,
                        picked_index: None,
                        escalated: true,
                    });
                }
            }
        }

        let pick_refs: Vec<(&str, &str)> = picks
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        let concrete = substitute_slots(skeleton, &pick_refs);
        let mean_confidence = if score_count > 0 {
            score_sum / score_count as f32
        } else {
            0.0
        };

        Ok(SlotFillerOutput {
            concrete_natsql: concrete,
            mean_confidence,
            escalations,
            decisions,
        })
    }
}

fn value_candidate_mentioned_in_nl(nl: &str, candidate: &str) -> bool {
    let bare = candidate.trim().trim_matches('\'').replace("''", "'");
    if bare.is_empty() {
        return false;
    }
    let needle = bare.to_ascii_lowercase();
    if needle.len() > 1 && needle.chars().all(|ch| ch == '0') {
        return false;
    }
    if let Some(parts) = candidate_date_parts(&needle) {
        if nl_mentions_date(nl, parts) {
            let nl_has_time = text_mentions_clock_time(nl);
            return !nl_has_time || text_mentions_clock_time(&needle);
        }
    }
    if !needle
        .chars()
        .all(|ch| ch.is_ascii_digit() || ch == '.' || ch == '-' || ch == '+')
    {
        return false;
    }
    let haystack = nl.to_ascii_lowercase();
    if haystack
        .split(|ch: char| !ch.is_ascii_digit() && ch != '.' && ch != '-' && ch != '+')
        .any(|token| token == needle)
    {
        return true;
    }
    // Also match human-formatted numeric ranges such as `1,900-2,000`.
    // The primary path keeps hyphens for dates/negative numbers; this
    // secondary path intentionally splits range endpoints.
    haystack
        .replace(',', "")
        .split(|ch: char| !ch.is_ascii_digit() && ch != '.')
        .any(|token| token == needle)
}

fn nl_mentions_date(nl: &str, expected: (u32, u32, u32)) -> bool {
    nl.split_whitespace().any(|token| {
        let stripped =
            token.trim_matches(|ch: char| !ch.is_ascii_digit() && ch != '-' && ch != '/');
        parse_date_parts(stripped).is_some_and(|actual| actual == expected)
    })
}

fn candidate_date_parts(text: &str) -> Option<(u32, u32, u32)> {
    parse_date_parts(text).or_else(|| {
        text.split_whitespace()
            .next()
            .and_then(|leading| leading.split('T').next())
            .and_then(parse_date_parts)
    })
}

fn text_mentions_clock_time(text: &str) -> bool {
    text.split_whitespace().any(|token| {
        let stripped =
            token.trim_matches(|ch: char| !ch.is_ascii_alphanumeric() && ch != ':' && ch != '.');
        let Some((hour, rest)) = stripped.split_once(':') else {
            return false;
        };
        !hour.is_empty()
            && hour.chars().all(|ch| ch.is_ascii_digit())
            && rest.chars().next().is_some_and(|ch| ch.is_ascii_digit())
    })
}

fn parse_date_parts(text: &str) -> Option<(u32, u32, u32)> {
    let separator = if text.contains('/') {
        '/'
    } else if text.contains('-') {
        '-'
    } else {
        return None;
    };
    let parts: Vec<&str> = text.split(separator).collect();
    if parts.len() != 3 {
        return None;
    }
    let parse = |part: &str| -> Option<u32> {
        (!part.is_empty() && part.chars().all(|ch| ch.is_ascii_digit()))
            .then(|| part.parse::<u32>().ok())
            .flatten()
    };
    let (year, month, day) = if parts[0].len() == 4 {
        (parse(parts[0])?, parse(parts[1])?, parse(parts[2])?)
    } else if parts[2].len() == 4 {
        (parse(parts[2])?, parse(parts[0])?, parse(parts[1])?)
    } else {
        return None;
    };
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    Some((year, month, day))
}

fn keep_source_empty_value_candidate(
    nl: &str,
    slot: &SlotInput,
    selected_field: &str,
    sources: &[String],
    candidate: &str,
) -> bool {
    sources.is_empty()
        && ((slot.preserve_source_empty_literals && value_candidate_mentioned_in_nl(nl, candidate))
            || field_allows_structured_literal(selected_field, candidate, nl)
            || field_allows_named_literal(selected_field, candidate, nl))
}

fn field_allows_structured_literal(field: &str, candidate: &str, nl: &str) -> bool {
    let bare = candidate.trim().trim_matches('\'').replace("''", "'");
    if bare.is_empty() {
        return false;
    }
    let tail = field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(field)
        .to_ascii_lowercase();
    if (tail.contains("date") || tail == "birthday")
        && candidate_date_parts(&bare).is_some_and(|parts| nl_mentions_date(nl, parts))
    {
        let nl_has_time = text_mentions_clock_time(nl);
        return !nl_has_time || text_mentions_clock_time(&bare);
    }
    if !nl_mentions_literal(nl, &bare) {
        return false;
    }
    let has_digit = bare.chars().any(|ch| ch.is_ascii_digit());
    let has_alpha = bare.chars().any(|ch| ch.is_ascii_alphabetic());
    let has_structural_punct = bare.contains('-') || bare.contains('/');
    let leading_zero_code = bare.len() > 1
        && bare.starts_with('0')
        && bare.chars().all(|ch| ch.is_ascii_alphanumeric());
    let field_is_code_like = tail.contains("zip")
        || tail.contains("code")
        || tail.contains("number")
        || tail.contains("num")
        || tail.contains("date")
        || tail.contains("mail")
        || tail.contains("phone")
        || tail.contains("year");
    field_is_code_like && (has_structural_punct || leading_zero_code || (has_digit && has_alpha))
}

fn field_allows_named_literal(field: &str, candidate: &str, nl: &str) -> bool {
    let bare = candidate.trim().trim_matches('\'').replace("''", "'");
    if bare.len() < 3 || !bare.chars().any(|ch| ch.is_ascii_alphabetic()) {
        return false;
    }
    if !nl_mentions_literal(nl, &bare) {
        return false;
    }
    let lower = bare.to_ascii_lowercase();
    if VALUE_LEXICAL_STOP.contains(&lower.as_str()) {
        return false;
    }
    let tail = field
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(field)
        .to_ascii_lowercase();
    tail.contains("city")
        || tail.contains("county")
        || tail.contains("district")
        || tail.contains("state")
        || tail.contains("status")
        || tail.contains("street")
        || tail.contains("region")
}

fn nl_mentions_literal(nl: &str, literal: &str) -> bool {
    let needle = normalised_alnum(literal);
    !needle.is_empty() && normalised_alnum(nl).contains(&needle)
}

fn slot_kind(slot_name: &str) -> &'static str {
    if slot_name.starts_with("@entity") {
        "entity"
    } else if slot_name.starts_with("@field") {
        "field"
    } else if slot_name.starts_with("@val") {
        "value"
    } else {
        "other"
    }
}

// Used by `SlotFiller::fill` (gated on `onnx`) and by the unit tests
// (always on). Same `cfg(any(...))` pattern as `top_k` in stage_linker.
#[cfg(any(feature = "onnx", test))]
fn pick_best_index(scores: &[f32]) -> Option<usize> {
    let mut best_idx: Option<usize> = None;
    let mut best_score = f32::NEG_INFINITY;
    for (i, &s) in scores.iter().enumerate() {
        if !s.is_nan() && s > best_score {
            best_score = s;
            best_idx = Some(i);
        }
    }
    best_idx
}

/// Backwards-compatible free-function entry point.
pub fn fill(
    _nl: &str,
    _skeleton: &str,
    _candidates_per_slot: &[Vec<String>],
    _intent_column_hints: &[String],
) -> Result<SlotFillerOutput> {
    Err(semsql_core::SemsqlError::Other(
        "stage_slotfiller::fill free function is unused once a SlotFiller is loaded — \
         call SlotFiller::fill instead"
            .into(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn intent_preference_lifts_matching_candidates_only() {
        let candidates = vec![
            "users.cost".to_string(),
            "users.email".to_string(),
            "orders.expense".to_string(),
        ];
        let scores = vec![0.40, 0.50, 0.30];
        let hints = vec!["cost".to_string(), "expense".to_string()];
        let biased = apply_intent_preference(&candidates, &scores, &hints, 0.2);
        assert!(biased[0] > scores[0]);
        assert_eq!(biased[1], scores[1]);
        assert!(biased[2] > scores[2]);
    }

    #[test]
    fn intent_preference_matches_on_tail_segment() {
        // The hint matches the column tail (`status`) even when the
        // entity prefix doesn't match.
        let candidates = vec!["users.status_code".to_string(), "orders.id".to_string()];
        let scores = vec![0.3, 0.6];
        let hints = vec!["status".to_string()];
        let biased = apply_intent_preference(&candidates, &scores, &hints, 0.2);
        assert!(biased[0] > 0.3);
        assert_eq!(biased[1], 0.6);
    }

    #[test]
    fn pick_top1_returns_none_on_empty_or_nan() {
        assert_eq!(pick_top1(&[], &[], &[]), None);
        let cands = vec!["a".to_string()];
        let scores = vec![f32::NAN];
        assert_eq!(pick_top1(&cands, &scores, &[]), None);
    }

    #[test]
    fn pick_top1_preserves_original_score_after_bias() {
        let cands = vec!["users.cost".to_string()];
        let scores = vec![0.5];
        let hints = vec!["cost".to_string()];
        let pick = pick_top1(&cands, &scores, &hints).unwrap();
        // The surfaced score is the *original* (pre-bias) score so
        // dashboards don't see > 1.0.
        assert!((pick.score - 0.5).abs() < f32::EPSILON);
        assert!(pick.from_intent_hint);
    }

    #[test]
    fn cap_candidates_preserves_index_alignment() {
        let mut indexes = vec![7, 8, 9];
        let mut candidates = vec!["a".to_string(), "b".to_string(), "c".to_string()];
        cap_candidates(&mut indexes, &mut candidates, 2);
        assert_eq!(indexes, vec![7, 8]);
        assert_eq!(candidates, vec!["a".to_string(), "b".to_string()]);
    }

    #[test]
    fn cap_candidates_keeps_synthetic_typed_literals() {
        let mut indexes = vec![0, 1, 2, usize::MAX];
        let mut candidates = vec![
            "sample-a".to_string(),
            "sample-b".to_string(),
            "sample-c".to_string(),
            "'PO Box 1040'".to_string(),
        ];
        cap_candidates(&mut indexes, &mut candidates, 2);

        assert_eq!(indexes, vec![usize::MAX, 0]);
        assert_eq!(
            candidates,
            vec!["'PO Box 1040'".to_string(), "sample-a".to_string()]
        );
    }

    #[test]
    fn id_numeric_mentions_ignores_marker_at_word_end() {
        assert!(
            id_numeric_mentions("Calculate the average overall rating of Pietro Marino.")
                .is_empty()
        );
    }

    #[test]
    fn soft_field_filter_retains_used_candidates_after_fresh_candidates() {
        let indexes = vec![0, 1, 2];
        let candidates = vec![
            "customers.phone".to_string(),
            "customers.customer".to_string(),
            "customers.ext".to_string(),
        ];
        let used_content_fields =
            std::collections::BTreeSet::from(["customers.customer".to_string()]);
        let used_join_fields = std::collections::BTreeSet::new();

        let (retained_indexes, retained_candidates) = softly_retain_filtered_field_candidates(
            &indexes,
            &candidates,
            &used_content_fields,
            &used_join_fields,
            false,
        );

        assert_eq!(retained_indexes, vec![0, 2, 1]);
        assert_eq!(
            retained_candidates,
            vec![
                "customers.phone".to_string(),
                "customers.ext".to_string(),
                "customers.customer".to_string(),
            ]
        );
    }

    #[test]
    fn field_reuse_penalty_is_soft_not_removal() {
        let slot = SlotInput {
            slot_name: "@field2".to_string(),
            slot_role: "projection_field".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customers.customer".to_string(),
            "customers.phone".to_string(),
        ];
        let scores = vec![1.00, 0.55];
        let used_content_fields =
            std::collections::BTreeSet::from(["customers.customer".to_string()]);
        let used_join_fields = std::collections::BTreeSet::new();

        let adjusted = apply_field_reuse_penalty(
            "show customer names and phones",
            &slot,
            &candidates,
            &scores,
            &used_content_fields,
            &used_join_fields,
        );

        assert!(adjusted[0] < scores[0]);
        assert_eq!(adjusted[1], scores[1]);
        assert!(adjusted[0].is_finite());
    }

    #[test]
    fn predicate_value_source_evidence_biases_attached_field_slot() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'Merged'".to_string()],
            candidate_source_fields: vec![vec!["customers.status".to_string()]],
            predicate_field_slot: Some("@field4".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence =
            predicate_value_evidence_by_field_slot("customers in merged North", &[value_slot]);
        let field_slot = SlotInput {
            slot_name: "@field4".to_string(),
            slot_role: "predicate_field".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customers.county".to_string(),
            "customers.status".to_string(),
        ];
        let biased = apply_predicate_value_source_field_bias(
            &field_slot,
            &candidates,
            &[0.60, 0.20],
            evidence.get("@field4"),
        );

        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn predicate_value_source_evidence_uses_canonical_field_keys() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'shipped'".to_string()],
            candidate_source_fields: vec![vec!["orders.order status".to_string()]],
            predicate_field_slot: Some("@field2".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence =
            predicate_value_evidence_by_field_slot("orders with status shipped", &[value_slot]);
        let field_slot = SlotInput {
            slot_name: "@field2".to_string(),
            slot_role: "predicate_field".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "orders.customer_name".to_string(),
            "orders.order_status".to_string(),
        ];
        let biased = apply_predicate_value_source_field_bias(
            &field_slot,
            &candidates,
            &[0.50, 0.05],
            evidence.get("@field2"),
        );

        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn predicate_value_source_evidence_skips_unmentioned_values() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'Merged'".to_string()],
            candidate_source_fields: vec![vec!["customers.status".to_string()]],
            predicate_field_slot: Some("@field4".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence = predicate_value_evidence_by_field_slot("customers in North", &[value_slot]);
        assert!(evidence
            .get("@field4")
            .is_none_or(|fields| fields.is_empty()));
    }

    #[test]
    fn predicate_value_source_evidence_skips_small_numeric_literals() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'1'".to_string(), "'12'".to_string()],
            candidate_source_fields: vec![
                vec!["customer_metrics.enrollment_k_12".to_string()],
                vec!["customer_metrics.high_grade".to_string()],
            ],
            predicate_field_slot: Some("@field4".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence =
            predicate_value_evidence_by_field_slot("grades 1 through 12 customers", &[value_slot]);
        assert!(evidence
            .get("@field4")
            .is_none_or(|fields| fields.is_empty()));
    }

    #[test]
    fn predicate_value_source_evidence_skips_generic_type_fields() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'OWNER'".to_string()],
            candidate_source_fields: vec![vec!["disp.type".to_string()]],
            predicate_field_slot: Some("@field4".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence =
            predicate_value_evidence_by_field_slot("customers who are Owner", &[value_slot]);
        assert!(evidence
            .get("@field4")
            .is_none_or(|fields| fields.is_empty()));
    }

    #[test]
    fn source_empty_named_literals_can_survive_for_district_and_status_fields() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };

        assert!(keep_source_empty_value_candidate(
            "lowest average score in Central Unified",
            &slot,
            "customers.district",
            &[],
            "'Central Unified'",
        ));
        assert!(keep_source_empty_value_candidate(
            "active and closed customers",
            &slot,
            "customers.status",
            &[],
            "'Closed'",
        ));
    }

    #[test]
    fn person_name_mentions_do_not_cross_and_boundaries() {
        let names = person_name_literal_mentions(
            "What is the website for the customers under the administrations of Mike Larson and Dante Alvarez?",
        );

        assert!(names.contains(&("Mike".to_string(), "Larson".to_string())));
        assert!(names.contains(&("Dante".to_string(), "Alvarez".to_string())));
        assert!(!names.contains(&("Larson".to_string(), "Dante".to_string())));
    }

    #[test]
    fn value_field_bias_keeps_multiple_person_names_in_pair_order() {
        let context = "SELECT @field1 FROM customers WHERE (@field2 = @val1 AND @field3 = @val2) OR (@field2 = @val3 AND @field3 = @val4)";
        let first_last_slot = SlotInput {
            slot_name: "@val2".to_string(),
            slot_role: "predicate_value".to_string(),
            context_window: context.to_string(),
            ..Default::default()
        };
        let second_first_slot = SlotInput {
            slot_name: "@val3".to_string(),
            slot_role: "predicate_value".to_string(),
            context_window: context.to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "'Mike'".to_string(),
            "'Larson'".to_string(),
            "'Dante'".to_string(),
            "'Alvarez'".to_string(),
        ];

        let first_last = apply_value_field_bias(
            "What is the website for the customers under the administrations of Mike Larson and Dante Alvarez?",
            &first_last_slot,
            &candidates,
            &[0.26, 0.11, 0.68, 0.59],
            Some("customers.admlname1"),
        );
        let second_first = apply_value_field_bias(
            "What is the website for the customers under the administrations of Mike Larson and Dante Alvarez?",
            &second_first_slot,
            &candidates,
            &[0.38, 0.20, 0.26, 0.28],
            Some("customers.admfname1"),
        );

        assert!(first_last[1] > first_last[3], "{first_last:?}");
        assert!(second_first[2] > second_first[0], "{second_first:?}");
    }

    #[test]
    fn predicate_value_source_evidence_skips_generic_city_token() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'Cathedral City'".to_string()],
            candidate_source_fields: vec![vec!["customers.mailing_city".to_string()]],
            predicate_field_slot: Some("@field4".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence = predicate_value_evidence_by_field_slot(
            "customer whose mailing city address is in Central",
            &[value_slot],
        );
        assert!(evidence
            .get("@field4")
            .is_none_or(|fields| fields.is_empty()));
    }

    #[test]
    fn predicate_value_source_evidence_skips_ambiguous_multi_slot_sources() {
        let slots = vec![
            SlotInput {
                slot_name: "@val1".to_string(),
                candidates: vec!["'Directly funded'".to_string()],
                candidate_source_fields: vec![vec![
                    "customer_metrics.priority_funding_type".to_string()
                ]],
                predicate_field_slot: Some("@field4".to_string()),
                slot_role: "predicate_value".to_string(),
                ..Default::default()
            },
            SlotInput {
                slot_name: "@val2".to_string(),
                candidates: vec!["'Locally funded'".to_string()],
                candidate_source_fields: vec![vec![
                    "customer_metrics.priority_funding_type".to_string()
                ]],
                predicate_field_slot: Some("@field5".to_string()),
                slot_role: "predicate_value".to_string(),
                ..Default::default()
            },
        ];
        let evidence = predicate_value_evidence_by_field_slot(
            "directly funded and locally funded customers",
            &slots,
        );
        assert!(evidence.is_empty());
    }

    #[test]
    fn predicate_value_source_evidence_accepts_unique_multi_predicate_sources() {
        let slots = vec![
            SlotInput {
                slot_name: "@val1".to_string(),
                candidates: vec!["'Breakfast Provision 2'".to_string()],
                candidate_source_fields: vec![vec![
                    "customer_metrics.nslp_provision_status".to_string()
                ]],
                predicate_field_slot: Some("@field5".to_string()),
                slot_role: "predicate_value".to_string(),
                ..Default::default()
            },
            SlotInput {
                slot_name: "@val2".to_string(),
                candidates: vec!["37".to_string()],
                candidate_source_fields: vec![vec!["customer_metrics.county_code".to_string()]],
                predicate_field_slot: Some("@field6".to_string()),
                slot_role: "predicate_value".to_string(),
                ..Default::default()
            },
        ];
        let evidence = predicate_value_evidence_by_field_slot(
            "customers with Breakfast Provision 2 in county code 37",
            &slots,
        );

        assert!(evidence
            .get("@field5")
            .is_some_and(|fields| fields.contains_key("customermetricsnslpprovisionstatus")));
        assert!(evidence
            .get("@field6")
            .is_some_and(|fields| fields.contains_key("customermetricscountycode")));

        let field_slot = SlotInput {
            slot_name: "@field6".to_string(),
            slot_role: "predicate_field".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customer_metrics.nslp_provision_status".to_string(),
            "customer_metrics.county_code".to_string(),
        ];
        let biased = apply_predicate_value_source_field_bias(
            &field_slot,
            &candidates,
            &[0.60, 0.05],
            evidence.get("@field6"),
        );

        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn predicate_value_source_evidence_accepts_partial_categorical_mentions() {
        let value_slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidates: vec!["'High customers (Public)'".to_string()],
            candidate_source_fields: vec![vec!["customer_metrics.customer_type".to_string()]],
            predicate_field_slot: Some("@field7".to_string()),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let evidence =
            predicate_value_evidence_by_field_slot("high customers in Monterey", &[value_slot]);

        assert!(evidence
            .get("@field7")
            .is_some_and(|fields| fields.contains_key("customermetricscustomertype")));
    }

    #[test]
    fn slot_resolution_rank_orders_semantic_roles() {
        let entity = SlotInput {
            slot_name: "@entity1".to_string(),
            ..Default::default()
        };
        let join = SlotInput {
            slot_name: "@field1".to_string(),
            slot_role: "join_key".to_string(),
            ..Default::default()
        };
        let projection = SlotInput {
            slot_name: "@field2".to_string(),
            slot_role: "projection_field".to_string(),
            ..Default::default()
        };
        let order = SlotInput {
            slot_name: "@field3".to_string(),
            slot_role: "order_field".to_string(),
            ..Default::default()
        };
        let predicate = SlotInput {
            slot_name: "@field4".to_string(),
            slot_role: "predicate_field".to_string(),
            ..Default::default()
        };
        let value = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        assert!(
            slot_resolution_rank(&entity) < slot_resolution_rank(&join)
                && slot_resolution_rank(&join) < slot_resolution_rank(&projection)
                && slot_resolution_rank(&projection) < slot_resolution_rank(&order)
                && slot_resolution_rank(&order) < slot_resolution_rank(&predicate)
                && slot_resolution_rank(&predicate) < slot_resolution_rank(&value)
        );
    }

    #[test]
    fn slot_role_bias_lifts_non_join_field_overlap() {
        let slot = SlotInput {
            slot_name: "@field3".to_string(),
            slot_role: "projection_field".to_string(),
            ..Default::default()
        };
        let candidates = vec!["orders.id".to_string(), "orders.total_amount".to_string()];
        let scores = vec![0.80, 0.79];
        let biased = apply_slot_role_bias("what is its total amount", &slot, &candidates, &scores);
        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn slot_role_bias_does_not_swamp_model_scores() {
        let slot = SlotInput {
            slot_name: "@field3".to_string(),
            slot_role: "projection_field".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customers.phone".to_string(),
            "order_scores.avgscrmath".to_string(),
        ];
        let scores = vec![0.90, 0.50];
        let biased = apply_slot_role_bias(
            "what is the phone number of the customer with the highest average score in math",
            &slot,
            &candidates,
            &scores,
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn slot_role_bias_lifts_open_date_projection_and_measure_order() {
        let projection = SlotInput {
            slot_name: "@field3".to_string(),
            slot_role: "projection_field".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customer_metrics.enrollment_k_12".to_string(),
            "customers.opened_on".to_string(),
        ];
        let scores = vec![0.12, 0.01];
        let biased = apply_slot_role_bias(
            "When did the customer with the largest enrollment open?",
            &projection,
            &candidates,
            &scores,
        );
        assert!(biased[1] > biased[0], "{biased:?}");

        let order = SlotInput {
            slot_name: "@field4".to_string(),
            slot_role: "order_field".to_string(),
            ..Default::default()
        };
        let scores = vec![0.69, 0.74];
        let biased = apply_slot_role_bias(
            "When did the customer with the largest enrollment open?",
            &order,
            &candidates,
            &scores,
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn slot_role_bias_maps_full_communication_address_projection_order() {
        let question =
            "State the names and full communication address of high customers in Monterey";
        let skeleton =
            "SELECT @field3, @field4, @field5, @field6, @field7 FROM @entity1 WHERE @field8 = @val1";
        let make_slot = |slot_name: &str| SlotInput {
            slot_name: slot_name.to_string(),
            slot_role: "projection_field".to_string(),
            context_window: skeleton.to_string(),
            ..Default::default()
        };
        let scores = vec![0.70, 0.40, 0.35, 0.30, 0.25];

        let first = apply_slot_role_bias(
            question,
            &make_slot("@field3"),
            &[
                "customers.state".to_string(),
                "customer_metrics.customer_name".to_string(),
                "customers.street".to_string(),
                "customers.city".to_string(),
                "customers.zip".to_string(),
            ],
            &scores,
        );
        assert!(first[1] > first[0], "{first:?}");

        let street = apply_slot_role_bias(
            question,
            &make_slot("@field4"),
            &[
                "customers.state".to_string(),
                "customer_metrics.customer_name".to_string(),
                "customers.street".to_string(),
                "customers.city".to_string(),
                "customers.zip".to_string(),
            ],
            &scores,
        );
        assert!(street[2] > street[0], "{street:?}");

        let city = apply_slot_role_bias(
            question,
            &make_slot("@field5"),
            &[
                "customers.state".to_string(),
                "customer_metrics.customer_name".to_string(),
                "customers.street".to_string(),
                "customers.city".to_string(),
                "customers.zip".to_string(),
            ],
            &scores,
        );
        assert!(city[3] > city[0], "{city:?}");

        let state = apply_slot_role_bias(
            question,
            &make_slot("@field6"),
            &[
                "customers.zip".to_string(),
                "customer_metrics.customer_name".to_string(),
                "customers.street".to_string(),
                "customers.city".to_string(),
                "customers.state".to_string(),
            ],
            &scores,
        );
        assert!(state[4] > state[0], "{state:?}");

        let zip = apply_slot_role_bias(
            question,
            &make_slot("@field7"),
            &[
                "customers.state".to_string(),
                "customer_metrics.customer_name".to_string(),
                "customers.street".to_string(),
                "customers.city".to_string(),
                "customers.zip".to_string(),
            ],
            &scores,
        );
        assert!(zip[4] > zip[0], "{zip:?}");
    }

    #[test]
    fn slot_role_bias_maps_contact_projection_order() {
        let question =
            "What is the phone number and extension number for the customer? Indicate the customer's name.";
        let skeleton = "SELECT @field1, @field2, @field3 FROM @entity1";
        let make_slot = |slot_name: &str| SlotInput {
            slot_name: slot_name.to_string(),
            slot_role: "projection_field".to_string(),
            context_window: skeleton.to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customers.customer".to_string(),
            "customers.phone".to_string(),
            "customers.ext".to_string(),
        ];
        let scores = vec![0.75, 0.40, 0.30];

        let phone = apply_slot_role_bias(question, &make_slot("@field1"), &candidates, &scores);
        assert!(phone[1] > phone[0], "{phone:?}");
        let ext = apply_slot_role_bias(question, &make_slot("@field2"), &candidates, &scores);
        assert!(ext[2] > ext[0], "{ext:?}");
        let name = apply_slot_role_bias(question, &make_slot("@field3"), &candidates, &scores);
        assert!(name[0] > name[1], "{name:?}");
    }

    #[test]
    fn slot_role_bias_lifts_count_identifier_projection() {
        let slot = SlotInput {
            slot_name: "@field1".to_string(),
            slot_role: "projection_field".to_string(),
            context_window: "SELECT COUNT(@field1) FROM @entity1 WHERE @field2 = @val1".to_string(),
            ..Default::default()
        };
        let candidates = vec!["orders.id".to_string(), "orders.region_name".to_string()];
        let scores = vec![0.11, 0.15];
        let biased =
            apply_slot_role_bias("how many orders are in Lagos", &slot, &candidates, &scores);
        assert!(biased[0] > biased[1], "{biased:?}");
        assert!(slot_is_count_projection(&slot));

        let code_candidates = vec![
            "orders.reference_number".to_string(),
            "orders.id".to_string(),
        ];
        let code_biased = apply_slot_role_bias(
            "how many orders have total over 500",
            &slot,
            &code_candidates,
            &[0.58, 0.04],
        );
        assert!(code_biased[1] > code_biased[0], "{code_biased:?}");
    }

    #[test]
    fn slot_role_bias_lifts_numeric_comparison_predicate_field() {
        let slot = SlotInput {
            slot_name: "@field4".to_string(),
            slot_role: "predicate_field".to_string(),
            predicate_operator: Some("BETWEEN".to_string()),
            ..Default::default()
        };
        let candidates = vec![
            "orders.total_amount".to_string(),
            "orders.region_name".to_string(),
        ];
        let scores = vec![0.08, 0.23];
        let biased = apply_slot_role_bias(
            "orders between 2,000 and 3,000 total amount in Lagos region",
            &slot,
            &candidates,
            &scores,
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn slot_role_bias_maps_person_name_predicate_fields() {
        let skeleton = "SELECT @field1 FROM @entity1 WHERE @field2 = @val1 AND @field3 = @val2";
        let make_slot = |slot_name: &str| SlotInput {
            slot_name: slot_name.to_string(),
            slot_role: "predicate_field".to_string(),
            predicate_operator: Some("=".to_string()),
            context_window: skeleton.to_string(),
            ..Default::default()
        };

        let first_name = apply_slot_role_bias(
            "What's Angela Sanders's major?",
            &make_slot("@field2"),
            &[
                "major.major_name".to_string(),
                "member.first_name".to_string(),
            ],
            &[0.80, 0.05],
        );
        assert!(first_name[1] > first_name[0], "{first_name:?}");

        let last_name = apply_slot_role_bias(
            "What's Angela Sanders's major?",
            &make_slot("@field3"),
            &["zip_code.state".to_string(), "member.last_name".to_string()],
            &[0.80, 0.05],
        );
        assert!(last_name[1] > last_name[0], "{last_name:?}");

        let hometown_first = apply_slot_role_bias(
            "Where is Amy Firth's hometown?",
            &make_slot("@field2"),
            &[
                "zip_code.state".to_string(),
                "member.first_name".to_string(),
            ],
            &[0.80, 0.05],
        );
        assert!(hometown_first[1] > hometown_first[0], "{hometown_first:?}");

        let hometown_last = apply_slot_role_bias(
            "Where is Amy Firth's hometown?",
            &make_slot("@field3"),
            &[
                "zip_code.county".to_string(),
                "member.last_name".to_string(),
            ],
            &[0.80, 0.05],
        );
        assert!(hometown_last[1] > hometown_last[0], "{hometown_last:?}");
    }

    #[test]
    fn slot_role_bias_skips_join_keys() {
        let slot = SlotInput {
            slot_name: "@field1".to_string(),
            slot_role: "join_key".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "customer_metrics.cdscode".to_string(),
            "customer_metrics.county_name".to_string(),
        ];
        let scores = vec![0.2, 0.2];
        assert_eq!(
            apply_slot_role_bias("county name", &slot, &candidates, &scores),
            scores
        );
    }

    #[test]
    fn join_endpoint_content_filter_requires_identifier_language() {
        assert!(!slot_allows_join_endpoint_content(
            "customers with more than 500 test takers"
        ));
        assert!(!slot_allows_join_endpoint_content(
            "list the zip code for priority customers"
        ));
        assert!(slot_allows_join_endpoint_content(
            "list the CDS codes for those customers"
        ));
        assert!(slot_allows_join_endpoint_content(
            "give their external identification number"
        ));
    }

    #[test]
    fn value_role_bias_prefers_positive_boolean_literal() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_field_slot: Some("@field1".to_string()),
            ..Default::default()
        };
        let candidates = vec!["1".to_string(), "0".to_string()];
        let scores = vec![0.49, 0.50];
        let biased = apply_value_role_bias(
            "priority customers",
            &slot,
            &candidates,
            &scores,
            Some("customer_metrics.priority_customer_y_n"),
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn value_role_bias_prefers_negative_boolean_literal() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_field_slot: Some("@field1".to_string()),
            ..Default::default()
        };
        let candidates = vec!["1".to_string(), "0".to_string()];
        let scores = vec![0.50, 0.49];
        let biased = apply_value_role_bias(
            "not priority customers",
            &slot,
            &candidates,
            &scores,
            Some("customer_metrics.priority_customer_y_n"),
        );
        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn value_role_bias_prefers_explicit_false_boolean_literal() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_field_slot: Some("@field1".to_string()),
            ..Default::default()
        };
        let candidates = vec!["1".to_string(), "0".to_string()];
        let scores = vec![0.50, 0.49];
        let biased = apply_value_role_bias(
            "false priority customers",
            &slot,
            &candidates,
            &scores,
            Some("customer_metrics.priority_customer_y_n"),
        );
        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn value_role_bias_overrides_strong_false_default_for_positive_boolean_literal() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_field_slot: Some("@field1".to_string()),
            ..Default::default()
        };
        let candidates = vec!["1".to_string(), "0".to_string()];
        let scores = vec![0.09, 0.92];
        let biased = apply_value_role_bias(
            "direct priority-funded customers",
            &slot,
            &candidates,
            &scores,
            Some("customer_metrics.priority_customer_y_n"),
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn value_lexical_bias_prefers_mentioned_categorical_values() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "'Directly funded'".to_string(),
            "'Locally funded'".to_string(),
        ];
        let scores = vec![0.65, 0.78];
        let biased = apply_value_lexical_bias(
            "direct priority-funded customers",
            &slot,
            &candidates,
            &scores,
            None,
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn value_lexical_bias_keeps_role_title_positions() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let candidates = vec!["'Vice President'".to_string(), "'President'".to_string()];

        let president = apply_value_lexical_bias(
            "Which department was the President of the club in?",
            &slot,
            &candidates,
            &[0.904, 0.859],
            Some("member.position"),
        );
        assert!(president[1] > president[0], "{president:?}");

        let vice_president = apply_value_lexical_bias(
            "event attended by the vice president",
            &slot,
            &candidates,
            &[0.243, 0.282],
            Some("member.position"),
        );
        assert!(vice_president[0] > vice_president[1], "{vice_president:?}");
    }

    #[test]
    fn value_lexical_bias_prefers_longer_exact_phrase() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            slot_role: "predicate_value".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "'Breakfast Provision 2'".to_string(),
            "'Provision 2'".to_string(),
        ];
        let scores = vec![0.20, 0.28];
        let biased = apply_value_lexical_bias(
            "customers with Breakfast Provision 2",
            &slot,
            &candidates,
            &scores,
            Some("customer_metrics.nslp_provision_status"),
        );

        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn selected_value_source_bias_prefers_mentioned_field_value() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            slot_role: "predicate_value".to_string(),
            candidate_source_fields: vec![Vec::new(), vec!["customers.county".to_string()]],
            ..Default::default()
        };
        let candidates = vec!["'Lunch'".to_string(), "'Merced'".to_string()];
        let scores = vec![0.70, 0.20];
        let indexes = vec![0, 1];

        let biased = apply_selected_value_source_bias(
            "Lunch Provision 2 in the county of Merced",
            &slot,
            &candidates,
            &scores,
            &indexes,
            Some("customers.county"),
        );

        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn synthetic_selected_field_literals_surface_selected_source_field() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            candidate_source_fields: vec![vec!["customers.zip".to_string()]],
            ..Default::default()
        };
        assert_eq!(
            scored_candidate_source_fields(&slot, usize::MAX, Some("orders.order_status")),
            vec!["orders.order_status".to_string()]
        );
    }

    #[test]
    fn typed_value_candidates_preserve_generic_structured_literals() {
        let nl = "Supplier record with mailing street address of PO Box 1040";
        let mut indexes = Vec::new();
        let mut candidates = Vec::new();

        ensure_typed_value_candidates(
            nl,
            "suppliers.mailing_street",
            &mut indexes,
            &mut candidates,
        );
        assert!(candidates.contains(&"'PO Box 1040'".to_string()));
        assert_eq!(indexes.len(), candidates.len());
    }

    #[test]
    fn value_lexical_bias_requires_distinctive_overlap() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let candidates = vec!["'Enterprise'".to_string(), "'Consumer'".to_string()];
        let scores = vec![0.65, 0.78];
        let biased = apply_value_lexical_bias(
            "active subscription customers",
            &slot,
            &candidates,
            &scores,
            None,
        );
        assert_eq!(biased, scores);
    }

    #[test]
    fn value_lexical_bias_skips_boolean_predicates() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let candidates = vec!["'Office'".to_string(), "1".to_string()];
        let scores = vec![0.80, 0.10];
        let biased = apply_value_lexical_bias(
            "active customers in the office region",
            &slot,
            &candidates,
            &scores,
            Some("customers.is_active"),
        );
        assert_eq!(biased, scores);
    }

    #[test]
    fn value_lexical_bias_skips_typed_domain_predicates() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let candidates = vec!["'Owner'".to_string(), "'Weekly'".to_string()];
        let scores = vec![0.68, 0.95];
        let biased = apply_value_lexical_bias(
            "customers who choose weekly statement issuance are Owner",
            &slot,
            &candidates,
            &scores,
            Some("billing.frequency"),
        );
        assert_eq!(biased, scores);
    }

    #[test]
    fn value_field_bias_splits_owner_names() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "'Avetik Atoian'".to_string(),
            "'Avetik'".to_string(),
            "'Atoian'".to_string(),
        ];
        let scores = vec![0.86, 0.50, 0.50];
        let first = apply_value_field_bias(
            "employees managed by Avetik Atoian",
            &slot,
            &candidates,
            &scores,
            Some("employees.first_name"),
        );
        assert!(first[1] > first[0], "{first:?}");
        let last = apply_value_field_bias(
            "employees managed by Avetik Atoian",
            &slot,
            &candidates,
            &scores,
            Some("employees.last_name"),
        );
        assert!(last[2] > last[0], "{last:?}");

        let question_candidates = vec![
            "'What'".to_string(),
            "'Ricci'".to_string(),
            "'Ulrich'".to_string(),
        ];
        let question_first = apply_value_field_bias(
            "What is the average deal size for accounts managed by Ricci Ulrich? List the accounts.",
            &slot,
            &question_candidates,
            &[0.90, 0.20, 0.70],
            Some("employees.first_name"),
        );
        assert!(question_first[1] > question_first[0], "{question_first:?}");
        let question_last = apply_value_field_bias(
            "What is the average deal size for accounts managed by Ricci Ulrich? List the accounts.",
            &slot,
            &question_candidates,
            &[0.05, 0.20, 0.10],
            Some("employees.last_name"),
        );
        assert!(question_last[2] > question_last[1], "{question_last:?}");

        let quoted_candidates = vec![
            "'Sacha Harrison'".to_string(),
            "'Sacha'".to_string(),
            "'Harrison'".to_string(),
        ];
        let quoted_first = apply_value_field_bias(
            "Where is the hometown state for \"Sacha Harrison\"?",
            &slot,
            &quoted_candidates,
            &[0.87, 0.02, 0.77],
            Some("member.first_name"),
        );
        assert!(quoted_first[1] > quoted_first[0], "{quoted_first:?}");
    }

    #[test]
    fn person_name_mentions_skip_question_word_possessives() {
        let names = person_name_literal_mentions("What's Christof Nielson's zip code type?");
        assert!(names.contains(&("Christof".to_string(), "Nielson".to_string())));
        assert!(!names.iter().any(|(first, _)| first == "What"), "{names:?}");

        let names = person_name_literal_mentions("Tell the phone number of \"Jordan Lee\".");
        assert!(names.contains(&("Jordan".to_string(), "Lee".to_string())));
        assert!(!names.iter().any(|(first, _)| first == "Tell"), "{names:?}");
    }

    #[test]
    fn value_field_bias_prefers_county_name_without_county_suffix() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let candidates = vec![
            "'Los Angeles County'".to_string(),
            "'Los Angeles'".to_string(),
        ];
        let biased = apply_value_field_bias(
            "customers in Los Angeles County",
            &slot,
            &candidates,
            &[0.86, 0.83],
            Some("customers.county"),
        );
        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn between_boundary_value_bias_prefers_lower_then_upper_mentions() {
        let candidates = vec!["6000".to_string(), "10000".to_string(), "8110".to_string()];
        let lower_slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_operator: Some("BETWEEN".to_string()),
            context_window: "WHERE @field1 BETWEEN @val1 AND @val2".to_string(),
            ..Default::default()
        };
        let upper_slot = SlotInput {
            slot_name: "@val2".to_string(),
            predicate_operator: Some("BETWEEN".to_string()),
            context_window: "WHERE @field1 BETWEEN @val1 AND @val2".to_string(),
            ..Default::default()
        };

        let lower = apply_between_boundary_value_bias(
            "salary more than 6000 but less than 10000",
            &lower_slot,
            &candidates,
            &[0.30, 0.36, 0.80],
        );
        assert!(lower[0] > lower[1], "{lower:?}");
        assert!((lower[2] - 0.80).abs() < f32::EPSILON, "{lower:?}");

        let upper = apply_between_boundary_value_bias(
            "salary more than 6000 but less than 10000",
            &upper_slot,
            &candidates,
            &[0.30, 0.36, 0.80],
        );
        assert!(upper[1] > upper[0], "{upper:?}");
        assert!((upper[2] - 0.80).abs() < f32::EPSILON, "{upper:?}");

        let comma_range_candidates = vec!["1900".to_string(), "2000".to_string(), "1".to_string()];
        let comma_lower = apply_between_boundary_value_bias(
            "free meal count of 1,900-2,000",
            &lower_slot,
            &comma_range_candidates,
            &[0.02, 0.01, 0.70],
        );
        assert!(comma_lower[0] > comma_lower[2], "{comma_lower:?}");
    }

    #[test]
    fn comparison_value_bias_prefers_mentioned_threshold() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_operator: Some("<=".to_string()),
            ..Default::default()
        };
        let candidates = vec!["250".to_string(), "1".to_string()];
        let biased = apply_comparison_mentioned_value_bias(
            "test takers not more than 250",
            &slot,
            &candidates,
            &[0.37, 0.88],
            Some("order_scores.numtsttakr"),
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn comparison_value_bias_ignores_age_range_for_numeric_threshold() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_operator: Some(">".to_string()),
            ..Default::default()
        };
        let candidates = vec!["'15-17'".to_string(), "800".to_string()];
        let biased = apply_comparison_mentioned_value_bias(
            "more than 800 free meals for ages 15-17",
            &slot,
            &candidates,
            &[0.70, 0.40],
            Some("customer_metrics.free_meal_count_ages_5_17"),
        );
        assert_eq!(biased[0], 0.70);
        assert!(biased[1] > biased[0], "{biased:?}");
    }

    #[test]
    fn comparison_value_bias_still_accepts_date_thresholds_for_date_fields() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            predicate_operator: Some(">".to_string()),
            ..Default::default()
        };
        let candidates = vec!["'2000-01-01'".to_string(), "'15-17'".to_string()];
        let biased = apply_comparison_mentioned_value_bias(
            "opened after 2000/1/1",
            &slot,
            &candidates,
            &[0.20, 0.40],
            Some("customers.opened_on"),
        );
        assert!(biased[0] > biased[1], "{biased:?}");
    }

    #[test]
    fn typed_value_candidates_extract_generic_field_literals() {
        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "last edited the post \"Examples for teaching: Correlation does not mean causation\"",
            "posts.title",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(
            candidates,
            vec!["'Examples for teaching: Correlation does not mean causation'".to_string()]
        );
        assert_eq!(indexes, vec![usize::MAX]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "How many badges has the user csgillespie obtained?",
            "users.displayname",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["'csgillespie'".to_string()]);
        assert_eq!(indexes, vec![usize::MAX]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "the user with a username of Harvey Motulsky",
            "users.displayname",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["'Harvey Motulsky'".to_string()]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "User No.3025 gave a comment",
            "comments.userid",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["3025".to_string()]);
        assert_eq!(indexes, vec![usize::MAX]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "product named Sample Widget numbered 29",
            "products.name",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["'Sample Widget'".to_string()]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "product named Sample Widget numbered 29",
            "products.number",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["29".to_string()]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "orders with more than 100 upvotes and more than 1 downvotes",
            "users.upvotes",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["100".to_string()]);

        let mut indexes = Vec::new();
        let mut candidates = Vec::new();
        ensure_typed_value_candidates(
            "orders with more than 100 upvotes and more than 1 downvotes",
            "users.downvotes",
            &mut indexes,
            &mut candidates,
        );
        assert_eq!(candidates, vec!["1".to_string()]);
    }

    #[test]
    fn source_empty_numeric_literal_requires_numeric_slot_context() {
        let mut slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let sources: Vec<String> = Vec::new();
        assert!(!keep_source_empty_value_candidate(
            "math score greater than 400",
            &slot,
            "customers.digital",
            &sources,
            "400"
        ));

        slot.preserve_source_empty_literals = true;
        assert!(keep_source_empty_value_candidate(
            "math score greater than 400",
            &slot,
            "order_scores.avgscrmath",
            &sources,
            "400"
        ));
        assert!(!keep_source_empty_value_candidate(
            "statement of issuance after transaction are Disponent",
            &slot,
            "account.frequency",
            &sources,
            "'Disponent'"
        ));
    }

    #[test]
    fn source_empty_structured_literals_require_compatible_fields() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let sources: Vec<String> = Vec::new();
        assert!(keep_source_empty_value_candidate(
            "customers with zip code 95203-3704",
            &slot,
            "customers.zip",
            &sources,
            "'95203-3704'"
        ));
        assert!(keep_source_empty_value_candidate(
            "priority number 00D4",
            &slot,
            "customers.prioritynum",
            &sources,
            "'00D4'"
        ));
        assert!(keep_source_empty_value_candidate(
            "patients with an examination on 1993/9/27",
            &slot,
            "examination.examination_date",
            &sources,
            "'1993-09-27'"
        ));
        assert!(keep_source_empty_value_candidate(
            "badges obtained on 7/19/2010 7:39:08 PM",
            &slot,
            "badges.date",
            &sources,
            "'2010-07-19 19:39:08.0'"
        ));
        assert!(!keep_source_empty_value_candidate(
            "badges obtained on 7/19/2010 7:39:08 PM",
            &slot,
            "badges.date",
            &sources,
            "'2010-07-19'"
        ));
        assert!(!keep_source_empty_value_candidate(
            "number of test takers less than 100",
            &slot,
            "customers.county",
            &sources,
            "100"
        ));
    }

    #[test]
    fn source_empty_named_literals_survive_only_for_location_fields() {
        let slot = SlotInput {
            slot_name: "@val1".to_string(),
            ..Default::default()
        };
        let sources: Vec<String> = Vec::new();
        assert!(keep_source_empty_value_candidate(
            "customers with mailing city address in Central",
            &slot,
            "customers.mailing_city",
            &sources,
            "'Central'"
        ));
        assert!(!keep_source_empty_value_candidate(
            "priority customers in Central County Office of Education",
            &slot,
            "customer_metrics.priority_customer_y_n",
            &sources,
            "'Office'"
        ));
    }

    #[test]
    fn value_candidate_mention_detects_numeric_literals() {
        assert!(value_candidate_mentioned_in_nl(
            "math score greater than 400",
            "400"
        ));
        assert!(value_candidate_mentioned_in_nl(
            "opened after 2000-01-01",
            "'2000-01-01'"
        ));
        assert!(value_candidate_mentioned_in_nl(
            "opened after 2000/1/1",
            "'2000-01-01'"
        ));
        assert!(value_candidate_mentioned_in_nl(
            "obtained on 7/19/2010 7:39:08 PM",
            "'2010-07-19 19:39:08.0'"
        ));
        assert!(!value_candidate_mentioned_in_nl(
            "obtained on 7/19/2010 7:39:08 PM",
            "'2010-07-19'"
        ));
        assert!(value_candidate_mentioned_in_nl(
            "between 1,900-2,000 test takers",
            "1900"
        ));
        assert!(value_candidate_mentioned_in_nl(
            "between 1,900-2,000 test takers",
            "2000"
        ));
        assert!(!value_candidate_mentioned_in_nl(
            "between 1,900-2,000 test takers",
            "000"
        ));
        assert!(!value_candidate_mentioned_in_nl(
            "math score greater than 400",
            "40"
        ));
        assert!(!value_candidate_mentioned_in_nl(
            "statement of issuance after transaction are Disponent",
            "'Disponent'"
        ));
    }

    #[test]
    fn substitute_slots_handles_double_digit_indices_first() {
        // Without length-DESC ordering, replacing `@val1` first would
        // turn `@val10` into `<val1>0`. The implementation sorts by
        // slot-name length DESC.
        let skeleton = "SELECT @val1, @val10 FROM t";
        let picks = vec![("@val1", "X"), ("@val10", "Y")];
        let out = substitute_slots(skeleton, &picks);
        assert_eq!(out, "SELECT X, Y FROM t");
    }

    #[test]
    fn substitute_slots_repeats_same_slot_consistently() {
        let skeleton = "SELECT @entity1.id FROM @entity1";
        let picks = vec![("@entity1", "users")];
        let out = substitute_slots(skeleton, &picks);
        assert_eq!(out, "SELECT users.id FROM users");
    }

    #[test]
    fn pick_best_index_ignores_nan() {
        let scores = vec![f32::NAN, 0.4, 0.7, f32::NAN];
        assert_eq!(pick_best_index(&scores), Some(2));
    }
}
