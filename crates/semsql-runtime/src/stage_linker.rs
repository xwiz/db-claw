//! Stage 1 — Schema Linker (cross-encoder, ~10M params).
//!
//! Given the natural-language query and the set of all `(entity, field)`
//! pairs in the SemanticGraph, the linker scores each pair for
//! relevance and returns the top-k. The cascade then hands the slice
//! to Stage 2 (skeleton generator) so the constrained-decoding grammar
//! covers only the relevant schema items.
//!
//! The model is a distilled DistilBERT-class cross-encoder produced by
//! `python/semsql_train` and exported to ONNX. Loading is fallible —
//! callers (the `Cascade` orchestrator) treat a missing manifest as
//! "skip Stage 1 and emit `NeedsModel`" rather than aborting.
//!
//! Intent hints from Stage 0b add an additive bias to candidate
//! columns whose canonical tail matches the hint vocabulary — see
//! [`apply_intent_bias`].

#[cfg(feature = "onnx")]
use crate::onnx::OnnxCrossEncoder;
use semsql_core::Result;
#[cfg(feature = "onnx")]
use semsql_graph::read::{vocabulary, VocabularyEntry};
#[cfg(feature = "onnx")]
use std::path::Path;

/// Result of one Stage 1 run.
#[derive(Clone, Debug, Default)]
pub struct LinkerOutput {
    /// Top-k entity canonical names, ordered by score descending.
    pub top_entities: Vec<String>,
    /// Top-k field canonical names (`entity.field`), flattened across
    /// every entity, ordered by score descending.
    pub top_fields: Vec<String>,
    /// Top-1 score across all candidates — feeds the confidence router.
    pub top_score: f32,
}

/// Linker model + the schema slice it scores against. Built once per
/// graph load; per-query work is bounded by [`Self::rank`].
///
/// Available only with `--features onnx`. The default build skips the
/// model wiring entirely so embeddable consumers (WASM, server side)
/// stay dep-light; the cascade orchestrator falls back to `NeedsModel`
/// when no Linker has been loaded.
#[cfg(feature = "onnx")]
pub struct Linker {
    model: OnnxCrossEncoder,
    /// Pre-deduplicated set of canonical entity names.
    entities: Vec<String>,
    /// Pre-deduplicated set of canonical fields as `entity.field`.
    fields: Vec<String>,
    /// Top-k entities to keep. Default 5.
    pub top_k_entities: usize,
    /// Top-k fields to keep. Default 10.
    pub top_k_fields: usize,
    /// Bias added to scores for fields whose canonical tail (or whose
    /// owning entity's tail) matches an intent hint. Tuned to 0.3 per
    /// the architecture plan; configurable for ablations.
    pub intent_bias: f32,
}

#[cfg(feature = "onnx")]
impl Linker {
    /// Load a linker model from the manifest's stage artifact and
    /// snapshot the SemanticGraph's vocabulary into entity/field lists.
    pub fn load(onnx_path: &Path, tokenizer_path: &Path, graph_path: &Path) -> Result<Self> {
        let model = OnnxCrossEncoder::load(onnx_path, tokenizer_path)?;
        let vocab = vocabulary(graph_path)?;
        let (entities, fields) = collect_schema_items(&vocab);
        Ok(Self {
            model,
            entities,
            fields,
            // Match the Stage 2 training/eval schema-slice distribution:
            // BIRD p95 is 3 entities / 7 fields. Wider slices improve raw
            // linker recall but pollute the T5 encoder prompt and produce
            // extra tables/columns end-to-end.
            top_k_entities: 3,
            top_k_fields: 7,
            intent_bias: 0.3,
        })
    }

    /// Score every `(NL, entity)` and `(NL, field)` pair, return top-k.
    ///
    /// `intent_column_hints` are token strings (e.g. `["expense",
    /// "cost"]`) that bias matching-tail fields upward — see
    /// [`apply_intent_bias`] for the matching rule.
    pub fn rank(&self, nl: &str, intent_column_hints: &[String]) -> Result<LinkerOutput> {
        if self.entities.is_empty() && self.fields.is_empty() {
            return Ok(LinkerOutput::default());
        }

        let entity_pairs: Vec<(String, String)> = self
            .entities
            .iter()
            .map(|e| (nl.to_string(), e.clone()))
            .collect();
        let field_pairs: Vec<(String, String)> = self
            .fields
            .iter()
            .map(|f| (nl.to_string(), f.clone()))
            .collect();

        let entity_scores = self.model.score_batch(&entity_pairs)?;
        let field_scores_raw = self.model.score_batch(&field_pairs)?;
        let field_scores = apply_intent_bias(
            &self.fields,
            &field_scores_raw,
            intent_column_hints,
            self.intent_bias,
        );

        let top_entities = top_k(&self.entities, &entity_scores, self.top_k_entities);
        let top_fields = top_k(&self.fields, &field_scores, self.top_k_fields);

        let top_score = entity_scores
            .iter()
            .chain(field_scores.iter())
            .copied()
            .fold(0.0f32, f32::max);

        Ok(LinkerOutput {
            top_entities,
            top_fields,
            top_score,
        })
    }
}

/// Apply additive intent bias to a parallel `(item, score)` vector.
///
/// Match rule: a field `e.f` matches a hint `h` iff the lower-cased
/// hint is a substring of either `e` or `f`. This is intentionally
/// loose — Stage 1 candidates are already vetted by relevance, and
/// the bias is small enough (default 0.3) to swing only ties. Tighter
/// vocabulary alignment is the slot filler's job.
pub fn apply_intent_bias(
    fields: &[String],
    scores: &[f32],
    hints: &[String],
    bias: f32,
) -> Vec<f32> {
    let normalised_hints: Vec<String> = hints.iter().map(|h| h.to_lowercase()).collect();
    if normalised_hints.is_empty() || fields.len() != scores.len() {
        return scores.to_vec();
    }
    let mut out = Vec::with_capacity(scores.len());
    for (field, &score) in fields.iter().zip(scores.iter()) {
        let lower = field.to_lowercase();
        let matched = normalised_hints.iter().any(|h| lower.contains(h.as_str()));
        out.push(if matched { score + bias } else { score });
    }
    out
}

// Used by `Linker::rank` (gated on `onnx`) and by the unit tests
// (always on). The `cfg(any(...))` keeps the default-build warning-free.
#[cfg(any(feature = "onnx", test))]
fn top_k(items: &[String], scores: &[f32], k: usize) -> Vec<String> {
    let mut indexed: Vec<(usize, f32)> = scores.iter().copied().enumerate().collect();
    // Stable sort with NaN-safe comparator: NaNs sink to the bottom.
    indexed.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    indexed
        .into_iter()
        .take(k)
        .map(|(idx, _)| items[idx].clone())
        .collect()
}

#[cfg(feature = "onnx")]
fn collect_schema_items(vocab: &[VocabularyEntry]) -> (Vec<String>, Vec<String>) {
    use std::collections::BTreeSet;
    let mut entities: BTreeSet<String> = BTreeSet::new();
    let mut fields: BTreeSet<String> = BTreeSet::new();
    for v in vocab {
        match v.canonical_kind.as_str() {
            "entity" => {
                entities.insert(v.canonical_value.clone());
            }
            "field" if v.canonical_value.contains('.') => {
                fields.insert(v.canonical_value.clone());
            }
            _ => {}
        }
    }
    (entities.into_iter().collect(), fields.into_iter().collect())
}

/// Backwards-compatible free-function entry point — returns NeedsModel
/// when no Linker has been loaded. Kept so the cascade orchestrator
/// can keep the same call shape during the model rollout.
pub fn rank(_nl: &str, _intent_column_hints: &[String]) -> Result<LinkerOutput> {
    Err(semsql_core::SemsqlError::Other(
        "stage_linker::rank free function is unused once a Linker is loaded — \
         call Linker::rank instead"
            .into(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn intent_bias_lifts_matching_fields_only() {
        let fields = vec![
            "users.cost".to_string(),
            "users.email".to_string(),
            "orders.expense".to_string(),
        ];
        let scores = vec![0.40, 0.50, 0.30];
        let hints = vec!["cost".to_string(), "expense".to_string()];
        let biased = apply_intent_bias(&fields, &scores, &hints, 0.3);
        assert!(biased[0] > scores[0], "users.cost should be lifted");
        assert_eq!(biased[1], scores[1], "users.email untouched");
        assert!(biased[2] > scores[2], "orders.expense should be lifted");
    }

    #[test]
    fn intent_bias_is_no_op_with_empty_hints() {
        let fields = vec!["a.b".to_string()];
        let scores = vec![0.7];
        let biased = apply_intent_bias(&fields, &scores, &[], 0.3);
        assert_eq!(biased, scores);
    }

    #[test]
    fn intent_bias_returns_input_on_length_mismatch() {
        // Defensive: caller bug shouldn't corrupt scores.
        let biased = apply_intent_bias(&["a.b".to_string()], &[0.1, 0.2], &["x".to_string()], 0.3);
        assert_eq!(biased, vec![0.1, 0.2]);
    }

    #[test]
    fn top_k_picks_highest_scores_in_order() {
        let items = vec!["a".into(), "b".into(), "c".into(), "d".into()];
        let scores = vec![0.1, 0.9, 0.4, 0.7];
        let out = top_k(&items, &scores, 2);
        assert_eq!(out, vec!["b".to_string(), "d".to_string()]);
    }

    #[test]
    fn top_k_handles_nan_without_panicking() {
        let items = vec!["a".into(), "b".into(), "c".into()];
        let scores = vec![f32::NAN, 0.5, 0.3];
        let out = top_k(&items, &scores, 2);
        // NaN sinks; a survivor of {b, c} fills the top.
        assert!(out.contains(&"b".to_string()));
    }
}
