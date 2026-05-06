//! Stage 2 — Skeleton Generator (~20M params, llguidance constrained).
//!
//! v0.2 wires the llguidance grammar binding alongside the deterministic
//! grammar generator (`crate::grammar`). The decoder loop arrives with
//! the distilled student weights; until then `generate` returns
//! `NeedsModel` and the cascade falls through to clarification.
//!
//! The grammar passed to llguidance is dynamically built per query: only
//! the schema items Stage 1 returned are valid productions, so hallucinated
//! tables/columns are *structurally impossible*. We compile the grammar
//! eagerly so a malformed-grammar bug surfaces *before* token decoding
//! starts — see [`compile_grammar`].

use crate::grammar::GrammarSchema;
#[cfg(feature = "onnx")]
use crate::grammar::build_natsql_grammar;
use semsql_core::{Result, SemsqlError};

/// Result of one Stage 2 run.
#[derive(Clone, Debug, Default)]
pub struct SkeletonOutput {
    /// NatSQL skeleton, e.g. `SELECT @field1 FROM @entity1 WHERE @field2 = @val1`.
    pub skeleton: String,
    /// Mean log-prob across decoded tokens — used for confidence routing.
    pub mean_logprob: f32,
}

/// Run Stage 2.
pub fn generate(
    _nl: &str,
    _ranked_entities: &[String],
    _ranked_fields: &[String],
) -> Result<SkeletonOutput> {
    Err(semsql_core::SemsqlError::Other(
        "stage_skeleton::generate not yet implemented (v0.2 milestone)".into(),
    ))
}

/// Compile the per-query NatSQL grammar through llguidance so the
/// decoder can apply token-level constraints. The function builds the
/// Lark grammar deterministically (via [`build_natsql_grammar`]) and
/// hands it to llguidance's lark→internal compiler.
///
/// Why we run this *before* invoking the model:
///
///   - It validates the grammar is well-formed against the live schema
///     slice. A malformed-grammar bug at decode time costs a model call
///     and yields an opaque error; catching it here keeps the failure
///     near the cause (the schema slice).
///   - It warms llguidance's regex cache so the first decoded token
///     pays the same constraint cost as the rest.
///
/// Available only with `--features onnx` because the Stage 2 decoding
/// loop itself depends on the ONNX runtime — there's no scenario where
/// you want llguidance without it.
#[cfg(feature = "onnx")]
pub fn compile_grammar(schema: &GrammarSchema) -> Result<CompiledGrammar> {
    use llguidance::api::{GrammarInit, ParserLimits, TopLevelGrammar, ValidationResult};

    let lark = build_natsql_grammar(schema);
    let grammar = TopLevelGrammar::from_lark(lark.clone());
    // `validate(None, ...)` runs the full lark→internal compile path
    // *without* needing a tokenizer. The actual decoder loop binds a
    // tokenizer at Stage 2 invocation time; this call exists so a
    // grammar bug surfaces near the schema slice that produced it
    // (and warms llguidance's regex cache as a side effect).
    let init = GrammarInit::Serialized(grammar);
    let warnings = match init.validate(None, ParserLimits::default()) {
        ValidationResult::Valid => Vec::new(),
        ValidationResult::Warnings(ws) => ws,
        ValidationResult::Error(e) => {
            return Err(SemsqlError::Other(format!(
                "llguidance compile (entities={}, fields={}): {e}",
                schema.entities.len(),
                schema.fields.len()
            )));
        }
    };
    Ok(CompiledGrammar { lark, warnings })
}

/// Default-build shim. With `--features onnx` off, callers shouldn't
/// reach Stage 2; the cascade orchestrator returns `NeedsModel` long
/// before. This stub exists so callers compiling without the feature
/// see a single, honest error rather than a missing-symbol link error.
#[cfg(not(feature = "onnx"))]
pub fn compile_grammar(_schema: &GrammarSchema) -> Result<CompiledGrammar> {
    Err(SemsqlError::Other(
        "stage_skeleton::compile_grammar requires `--features onnx`".into(),
    ))
}

/// Output of [`compile_grammar`]. The `lark` source is retained so a
/// Stage 2 decoder loop can re-bind the grammar against a tokenizer
/// (the bind step is per-tokenizer, not per-grammar). `warnings` are
/// llguidance-emitted lints — non-blocking but worth surfacing in
/// `semsql doctor` so subtle grammar regressions don't go unnoticed.
#[derive(Clone, Debug)]
pub struct CompiledGrammar {
    /// The Lark source the grammar was compiled from. Stable across
    /// invocations for the same schema slice.
    pub lark: String,
    /// Non-fatal warnings from llguidance's lexer/grammar compiler.
    pub warnings: Vec<String>,
}

#[cfg(all(test, feature = "onnx"))]
mod tests {
    use super::*;

    #[test]
    fn compiles_a_realistic_schema_slice() {
        let schema = GrammarSchema {
            entities: vec!["users".into(), "orders".into()],
            fields: vec![
                "users.id".into(),
                "users.email".into(),
                "orders.total".into(),
            ],
            value_slots: vec![],
        };
        let compiled = compile_grammar(&schema).expect("grammar must compile");
        assert!(compiled.lark.contains("\"users\""));
        assert!(compiled.lark.contains("\"orders\""));
    }

    #[test]
    fn empty_schema_emits_an_unsatisfiable_grammar_but_still_compiles() {
        // The sentinel `__no_entities__` / `__no_fields__` productions
        // mean the grammar accepts no input — but it must still parse
        // through llguidance so callers see the schema-coverage gap as
        // a Stage 0 problem rather than a grammar-bug crash.
        let compiled = compile_grammar(&GrammarSchema::default())
            .expect("sentinel grammar must still parse");
        assert!(compiled.lark.contains("__no_entities__"));
    }

    #[test]
    fn weird_canonical_names_compile_after_lark_escaping() {
        // Defensive — sanitiser should keep these out, but the grammar
        // generator escapes them anyway. Make sure the escape survives
        // round-trip through llguidance.
        let schema = GrammarSchema {
            entities: vec!["weird\"name".into()],
            fields: vec!["weird\"name.col".into()],
            value_slots: vec![],
        };
        compile_grammar(&schema).expect("escaped quotes must compile");
    }
}
