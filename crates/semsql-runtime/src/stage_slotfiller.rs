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
//!  2. If a slot has *no* matching candidate and an intent fired,
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

#[cfg(feature = "onnx")]
use crate::onnx::OnnxCrossEncoder;
use semsql_core::Result;
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
    /// Slots that escalated to clarification (no candidate matched
    /// the intent hint, or the model produced confidence below the
    /// abstain threshold).
    pub escalations: Vec<String>,
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

/// Threshold below which a slot escalates to clarification instead of
/// committing the model's top pick. 0.1 is the Phase A smoke-test value —
/// BIRD questions score 0.18-0.38 on models trained on Spider, which is
/// lower than the 0.4 threshold calibrated for Spider. Raise back to 0.4
/// after Phase D retraining on BIRD.
pub const ABSTAIN_THRESHOLD: f32 = 0.1;

/// Bias added to candidates whose canonical tail matches an intent
/// hint. Smaller than Stage 1's `intent_bias` (0.3) because Stage 3
/// scores are already tightly bounded — too large a bump would
/// over-rule the model on every hinted slot.
pub const INTENT_PREFERENCE_BIAS: f32 = 0.2;

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
    sorted.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
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
            });
        }

        let mut picks: Vec<(String, String)> = Vec::with_capacity(slots.len());
        let mut escalations: Vec<String> = Vec::new();
        let mut score_sum = 0.0f32;
        let mut score_count = 0usize;
        let context = format!("{nl} | {skeleton}");

        for slot in slots {
            if slot.candidates.is_empty() {
                escalations.push(slot.slot_name.clone());
                continue;
            }
            let pairs: Vec<(String, String)> = slot
                .candidates
                .iter()
                .map(|c| (context.clone(), c.clone()))
                .collect();
            let scores = self.model.score_batch(&pairs)?;
            let biased = apply_intent_preference(
                &slot.candidates,
                &scores,
                &slot.intent_hints,
                self.intent_bias,
            );
            let best = pick_best_index(&biased);
            match best {
                Some(idx) if scores[idx] >= self.abstain_threshold => {
                    picks.push((slot.slot_name.clone(), slot.candidates[idx].clone()));
                    score_sum += scores[idx];
                    score_count += 1;
                }
                _ => {
                    escalations.push(slot.slot_name.clone());
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
        })
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
