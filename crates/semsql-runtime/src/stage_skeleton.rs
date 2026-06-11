//! Stage 2 — Skeleton Generator (~20M params, llguidance constrained).
//!
//! Decodes NatSQL skeletons from `(NL, ranked schema slice)` pairs using a
//! distilled T5-mini-class encoder-decoder exported to ONNX by
//! `python/semsql_train/onnx_export.py`.
//!
//! Architecture (from the Stage 2 training contract §2.2`):
//!
//!  - Encoder: 4 layers, d_model=384, 6 heads — processes the NL + schema
//!    sentinel string (format defined in §2.3).
//!  - Decoder: 4 layers, same dimensions — generates NatSQL tokens one by
//!    one under a per-query Lark grammar enforced by llguidance. The grammar
//!    constrains placeholder NatSQL syntax and slot cardinality. Semantic
//!    table/field/value binding happens downstream.
//!
//! Token decoding strategy: **greedy** (argmax after llguidance mask).
//! Beam-search (size 4) is wired in the Stage 2 training contract §2.4` but disabled by
//! default; the latency trade-off (2× cost, ~2% skeleton-match lift) is not
//! worth the default budget.
//!
//! llguidance integration:
//!
//!  The compiled grammar is bound to the model tokenizer once per
//!  [`SkeletonGenerator`] load. Per query, a new `llguidance::Constraint` is
//!  constructed (cheap — it clones the compiled automaton state) and
//!  drives the per-step `compute_mask()` → `consume_token()` loop.
//!  Grammar-compile cost (entity×field enumeration) is paid at
//!  `compile_grammar()` call time, not per decoded token.

#[cfg(feature = "onnx")]
use crate::grammar::build_natsql_grammar;
use crate::grammar::GrammarSchema;
#[cfg(feature = "onnx")]
use crate::onnx::{OnnxDecoder, OnnxEncoder};
#[cfg(feature = "onnx")]
use crate::tokenizer_bridge::OnnxTokEnv;
#[cfg(feature = "onnx")]
use llguidance::{Constraint, ParserFactory};
use semsql_core::{Result, SemsqlError};
#[cfg(feature = "onnx")]
use std::path::Path;
#[cfg(feature = "onnx")]
use tokenizers::Tokenizer;

/// T5 special token: decoder start / pad. T5-small and T5-efficient-base
/// both use token id 0 as the padding / decoder-start token.
#[cfg(feature = "onnx")]
const T5_PAD_ID: i64 = 0;

/// End-of-sequence token id for T5 vocab (shared across T5 family).
#[cfg(feature = "onnx")]
const T5_EOS_ID: i64 = 1;

/// Hard cap on generated skeleton length.
///
/// BIRD dev contains long arithmetic/ranking skeletons with several joins and
/// predicates. The old 96-token cap truncated otherwise-valid constrained
/// decodes mid-placeholder or mid-keyword (`@`, `ORDER B`), which then looked
/// like a semantic Stage 2 failure. Keep the cap bounded for latency, but high
/// enough for those state-machine shapes to finish.
#[cfg(feature = "onnx")]
const MAX_DECODE_STEPS: usize = 160;

/// Result of one Stage 2 run.
#[derive(Clone, Debug, Default)]
pub struct SkeletonOutput {
    /// NatSQL skeleton, e.g.
    /// `SELECT @field1 FROM @entity1 WHERE @field2 = @val1`.
    pub skeleton: String,
    /// Mean log-prob across decoded tokens — used for confidence routing.
    /// Range `(-∞, 0]`; close to 0 means high confidence.
    pub mean_logprob: f32,
    /// Tokens that were banned during decoding (repair-mode reruns).
    /// Empty on the first successful pass; populated when the validator
    /// rejected a previous attempt and decode was retried with the
    /// offending tokens masked out.
    pub repair_attempts: u32,
}

// ---------------------------------------------------------------------------
// SkeletonGenerator — the loaded model pair
// ---------------------------------------------------------------------------

/// Loaded Stage 2 model pair: encoder ONNX + decoder ONNX + tokenizer.
///
/// [`SkeletonGenerator::load`] reads the ONNX files produced by
/// `python/semsql_train/onnx_export.py::export_stage("skeleton", ...)`.
/// Optimum exports seq2seq models into a directory with:
///
///   - `encoder_model.onnx`
///   - `decoder_model.onnx`
///   - `tokenizer.json`
///
/// The manifest's `skeleton.path` must point at that directory.
///
/// `SkeletonGenerator` is `Send + Sync` — both underlying `ort::Session`s
/// use `Mutex` internally. It can be shared across query threads.
#[cfg(feature = "onnx")]
pub struct SkeletonGenerator {
    encoder: OnnxEncoder,
    decoder: OnnxDecoder,
    tokenizer: Tokenizer,
    /// llguidance parser factory bound to the model tokenizer. Built once
    /// at load — cloning a `ParserFactory` is cheap (it shares the
    /// `SlicedBiasComputer` via `Arc`) so per-query parser creation is
    /// amortised against the one-time vocab → trie construction.
    parser_factory: ParserFactory,
}

#[cfg(feature = "onnx")]
impl SkeletonGenerator {
    /// Tokenize `text` and return its token ids — used by repair-mode
    /// to convert validator-rejected identifiers back into the decoder
    /// vocabulary so the next attempt can mask them out.
    pub fn encode_text(&self, text: &str) -> Result<Vec<u32>> {
        let enc = self
            .tokenizer
            .encode(text, false)
            .map_err(|e| SemsqlError::Other(format!("encode_text: {e}")))?;
        Ok(enc.get_ids().to_vec())
    }

    /// Load the encoder+decoder ONNX pair from `model_dir` (the directory
    /// that `optimum` wrote) and the tokenizer from `tokenizer_path`.
    ///
    /// Expected directory layout:
    ///
    /// ```text
    /// model_dir/
    ///   encoder_model.onnx
    ///   decoder_model.onnx
    /// ```
    pub fn load(model_dir: &Path, tokenizer_path: &Path) -> Result<Self> {
        let encoder_path = model_dir.join("encoder_model.onnx");
        let decoder_path = model_dir.join("decoder_model.onnx");

        if !encoder_path.exists() {
            return Err(SemsqlError::Other(format!(
                "encoder_model.onnx not found in `{}`",
                model_dir.display()
            )));
        }
        if !decoder_path.exists() {
            return Err(SemsqlError::Other(format!(
                "decoder_model.onnx not found in `{}`",
                model_dir.display()
            )));
        }

        let encoder = OnnxEncoder::load(&encoder_path)?;
        let decoder = OnnxDecoder::load(&decoder_path)?;
        let tokenizer = Tokenizer::from_file(tokenizer_path).map_err(|e| {
            SemsqlError::Other(format!(
                "tokenizer load `{}`: {e}",
                tokenizer_path.display()
            ))
        })?;

        // Build the llguidance tokenizer bridge once. The tokenizer is
        // cloned (cheap — it's a single Arc<TokenizerImpl> internally)
        // because both `SkeletonGenerator` (for source encoding) and
        // `OnnxTokEnv` (for grammar-driven re-tokenisation) need it.
        let tok_env = OnnxTokEnv::new(tokenizer.clone())?.into_tok_env();
        let mut parser_factory = ParserFactory::new_simple(&tok_env)
            .map_err(|e| SemsqlError::Other(format!("ParserFactory::new_simple: {e}")))?;
        // Quiet the per-mask logging — we have our own observability via
        // SkeletonOutput.repair_attempts and parser_stats.
        parser_factory.quiet();

        Ok(Self {
            encoder,
            decoder,
            tokenizer,
            parser_factory,
        })
    }

    /// Generate a NatSQL skeleton for `source` (the formatted encoder input:
    /// `"question: <NL>  ¦  schema: ..."`) under the constraints defined by
    /// `compiled_grammar`.
    ///
    /// Returns the decoded skeleton string and the mean log-prob across all
    /// generated tokens. Uses greedy decoding (argmax after llguidance mask).
    ///
    /// `compiled_grammar.lark` is bound to the model tokenizer once per
    /// generate call; the automaton is then driven step by step as tokens
    /// are committed.
    pub fn generate(
        &self,
        source: &str,
        compiled_grammar: &CompiledGrammar,
    ) -> Result<SkeletonOutput> {
        self.generate_with_bans(source, compiled_grammar, &[], 0)
    }

    /// Generate with an explicit list of banned token ids and a repair-attempt
    /// counter (surfaced in the output for telemetry).
    ///
    /// The banned list is OR-ed into the llguidance mask at every step:
    /// any token id in `banned_token_ids` is force-masked to `-∞` regardless
    /// of what the grammar says. This is the repair-mode path: when the
    /// validator rejects a skeleton (e.g. it picked an out-of-scope
    /// identifier), the cascade orchestrator captures the offending token
    /// id(s), increments the attempt counter, and re-decodes with those
    /// tokens forbidden.
    pub fn generate_with_bans(
        &self,
        source: &str,
        compiled_grammar: &CompiledGrammar,
        banned_token_ids: &[u32],
        repair_attempts: u32,
    ) -> Result<SkeletonOutput> {
        // 1. Tokenise the encoder source.
        let enc = self
            .tokenizer
            .encode(source, true)
            .map_err(|e| SemsqlError::Other(format!("tokenise source: {e}")))?;
        let src_ids: Vec<i64> = enc.get_ids().iter().map(|&id| id as i64).collect();
        let src_mask: Vec<i64> = enc.get_attention_mask().iter().map(|&m| m as i64).collect();
        let src_len = src_ids.len();

        if src_len == 0 {
            return Err(SemsqlError::Other("empty encoder input".into()));
        }

        // 2. Encoder forward pass.
        let enc_hidden = self.encoder.encode(&src_ids, &src_mask, src_len)?;
        let hidden_size = self.encoder.hidden_size;

        // 3. Set up llguidance constraint for this query.
        //    The constraint is constructed per-query from the compiled grammar
        //    and the model vocabulary. It drives the token-level mask.
        let vocab_size = self.decoder.vocab_size;
        let mut constraint =
            build_llguidance_constraint(&compiled_grammar.lark, &self.parser_factory, vocab_size)?;

        // 4. Greedy decode loop.
        let mut decode_ids: Vec<i64> = vec![T5_PAD_ID]; // T5 decoder starts with pad
        let mut log_probs: Vec<f32> = Vec::new();

        loop {
            if decode_ids.len() > MAX_DECODE_STEPS {
                break;
            }

            // 4a. Compute llguidance token mask for this position.
            //     Returns a bit-vector of length vocab_size; 1 = allowed.
            let mut mask = match query_llguidance_mask(&mut constraint, vocab_size)? {
                LlgStep::Stop => break,
                LlgStep::Sample(mask) => mask,
            };
            // Apply repair-mode bans: tokens that triggered a previous
            // validation failure are forbidden regardless of grammar.
            for &banned in banned_token_ids {
                let idx = banned as usize;
                if idx < mask.len() {
                    mask[idx] = false;
                }
            }

            // 4b. Decoder one step — pass full prefix so the non-KV-cache
            //     decoder ONNX can attend over all tokens generated so far.
            let logits =
                self.decoder
                    .step(&decode_ids, &enc_hidden, &src_mask, src_len, hidden_size)?;

            // 4c. Apply mask: set forbidden logits to −∞.
            let masked_logits = apply_token_mask(&logits, &mask);

            // 4d. Greedy argmax + log-prob tracking.
            let (next_id, logprob) = greedy_argmax_logprob(&masked_logits)?;
            log_probs.push(logprob);

            // 4e. Commit the sampled token through llguidance. The high-level
            // constraint owns forced-token and backtrack bookkeeping, which
            // keeps the parser state aligned with the decoder prefix.
            let commit = commit_llguidance_token(&mut constraint, next_id as u32)?;
            if commit.backtrack > 0 {
                let remove = commit.backtrack as usize;
                if remove > decode_ids.len().saturating_sub(1) {
                    return Err(SemsqlError::Other(
                        "stage2_constraint_error: llguidance requested invalid backtrack".into(),
                    ));
                }
                let keep = decode_ids.len() - remove;
                decode_ids.truncate(keep);
                for _ in 0..remove.min(log_probs.len()) {
                    let _ = log_probs.pop();
                }
            }
            for token in commit.tokens {
                decode_ids.push(token as i64);
            }
            if commit.stop || next_id == T5_EOS_ID as usize {
                break;
            }
        }

        // 5. Decode the generated token ids back to text (strip the BOS pad).
        let generated_ids: Vec<u32> = decode_ids[1..].iter().map(|&id| id as u32).collect();
        let skeleton = self
            .tokenizer
            .decode(&generated_ids, true)
            .map_err(|e| SemsqlError::Other(format!("decode skeleton tokens: {e}")))?;

        let mean_logprob = if log_probs.is_empty() {
            0.0
        } else {
            log_probs.iter().sum::<f32>() / log_probs.len() as f32
        };

        Ok(SkeletonOutput {
            skeleton: skeleton.trim().to_string(),
            mean_logprob,
            repair_attempts,
        })
    }
}

// ---------------------------------------------------------------------------
// Grammar compilation — available regardless of `onnx` feature
// ---------------------------------------------------------------------------

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

/// Default-build shim.
#[cfg(not(feature = "onnx"))]
pub fn compile_grammar(_schema: &GrammarSchema) -> Result<CompiledGrammar> {
    Err(SemsqlError::Other(
        "stage_skeleton::compile_grammar requires `--features onnx`".into(),
    ))
}

/// Output of [`compile_grammar`]. The `lark` source is retained so the
/// Stage 2 decoder loop can bind the grammar against the model tokenizer
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

// ---------------------------------------------------------------------------
// llguidance per-step helpers — feature-gated
// ---------------------------------------------------------------------------

/// Per-query llguidance state.
#[cfg(feature = "onnx")]
pub(crate) struct LlgConstraint {
    /// High-level sampling constraint. It wraps the parser and handles
    /// forced tokens/backtracking between `compute_mask()` and `commit_token()`.
    constraint: Constraint,
}

/// Construct a fresh llguidance constraint for one generation run.
///
/// Compiles the Lark grammar against the bound tokenizer (one-shot at
/// `ParserFactory` creation; the per-call cost here is automaton
/// instantiation only) and returns a high-level constraint ready for
/// `compute_mask()` / `commit_token()` driving from the decode loop.
#[cfg(feature = "onnx")]
fn build_llguidance_constraint(
    lark: &str,
    factory: &ParserFactory,
    vocab_size: usize,
) -> Result<LlgConstraint> {
    use llguidance::api::TopLevelGrammar;
    let grammar = TopLevelGrammar::from_lark(lark.to_string());
    let parser = factory
        .create_parser(grammar)
        .map_err(|e| SemsqlError::Other(format!("llguidance parser create: {e}")))?;
    let _ = vocab_size; // sized at the call site for symmetry with the mask path
    Ok(LlgConstraint {
        constraint: Constraint::new(parser),
    })
}

/// One llguidance sampling step.
#[cfg(feature = "onnx")]
enum LlgStep {
    Stop,
    Sample(Vec<bool>),
}

/// Result of committing one sampled token.
#[cfg(feature = "onnx")]
struct LlgCommit {
    stop: bool,
    backtrack: u32,
    tokens: Vec<u32>,
}

/// Returns a bitmask of allowed tokens at the current decode position, or
/// `Stop` when the grammar is complete. llguidance errors fail closed so
/// gated benchmark runs bucket them as `stage2_constraint_error`.
#[cfg(feature = "onnx")]
fn query_llguidance_mask(constraint: &mut LlgConstraint, vocab_size: usize) -> Result<LlgStep> {
    match constraint.constraint.compute_mask() {
        Ok(step) if step.is_stop() => Ok(LlgStep::Stop),
        Ok(step) => {
            let vob = step.sample_mask.as_ref().ok_or_else(|| {
                SemsqlError::Other(
                    "stage2_constraint_error: llguidance returned no sample mask".into(),
                )
            })?;
            let mut out = vec![false; vocab_size];
            for tok in vob.iter() {
                let idx = tok as usize;
                if idx < vocab_size {
                    out[idx] = true;
                }
            }
            Ok(LlgStep::Sample(out))
        }
        Err(e) => Err(SemsqlError::Other(format!(
            "stage2_constraint_error: llguidance mask failed: {e}"
        ))),
    }
}

/// Commit a generated token to the constraint state.
#[cfg(feature = "onnx")]
fn commit_llguidance_token(constraint: &mut LlgConstraint, token_id: u32) -> Result<LlgCommit> {
    match constraint.constraint.commit_token(Some(token_id)) {
        Ok(commit) => Ok(LlgCommit {
            stop: commit.stop,
            backtrack: commit.backtrack,
            tokens: commit.ff_tokens,
        }),
        Err(e) => Err(SemsqlError::Other(format!(
            "stage2_constraint_error: llguidance commit failed for token {token_id}: {e}"
        ))),
    }
}

// ---------------------------------------------------------------------------
// Decoding utilities
// ---------------------------------------------------------------------------

/// Apply a boolean token mask to raw logits: forbidden tokens → −∞.
#[cfg(feature = "onnx")]
fn apply_token_mask(logits: &[f32], mask: &[bool]) -> Vec<f32> {
    logits
        .iter()
        .zip(mask.iter().chain(std::iter::repeat(&true)))
        .map(|(&l, &allowed)| if allowed { l } else { f32::NEG_INFINITY })
        .collect()
}

/// Greedy argmax + log-prob extraction.
///
/// Returns `(best_token_id, log_prob)`. `log_prob = log(softmax[best_id])`.
/// Returns an error if `logits` is empty or every entry is `−∞` (can
/// happen if the grammar mask forbids all tokens — indicates a grammar
/// or model mismatch that must be surfaced).
#[cfg(feature = "onnx")]
fn greedy_argmax_logprob(logits: &[f32]) -> Result<(usize, f32)> {
    if logits.is_empty() {
        return Err(SemsqlError::Other("empty logits from decoder".into()));
    }

    let best_idx_max = logits
        .iter()
        .enumerate()
        .filter(|(_, v)| v.is_finite())
        .max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
        .map(|(i, v)| (i, *v));
    let Some((best_idx, max)) = best_idx_max else {
        return Err(SemsqlError::Other(
            "all tokens masked (grammar + model mismatch)".into(),
        ));
    };

    // Compute log(softmax[best_idx]) for confidence tracking.
    let exps: Vec<f32> = logits
        .iter()
        .map(|&v| if v.is_finite() { (v - max).exp() } else { 0.0 })
        .collect();
    let sum: f32 = exps.iter().sum();
    let log_prob = if sum > f32::EPSILON {
        (exps[best_idx] / sum).ln()
    } else {
        f32::NEG_INFINITY
    };

    Ok((best_idx, log_prob))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

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
        assert!(compiled.lark.contains("\"@entity1\""));
        assert!(compiled.lark.contains("\"@entity2\""));
        assert!(compiled.lark.contains("\"@field1\""));
    }

    #[test]
    fn empty_schema_emits_an_unsatisfiable_grammar_but_still_compiles() {
        let compiled =
            compile_grammar(&GrammarSchema::default()).expect("sentinel grammar must still parse");
        assert!(compiled.lark.contains("__no_entities__"));
    }

    #[test]
    fn weird_canonical_names_do_not_enter_placeholder_grammar() {
        let schema = GrammarSchema {
            entities: vec!["weird\"name".into()],
            fields: vec!["weird\"name.col".into()],
            value_slots: vec![],
        };
        let compiled = compile_grammar(&schema).expect("placeholder grammar must compile");
        assert!(compiled.lark.contains("\"@entity1\""));
        assert!(!compiled.lark.contains("weird"));
    }

    #[test]
    fn apply_token_mask_sets_forbidden_to_neg_inf() {
        let logits = vec![1.0, 2.0, 3.0, 4.0];
        let mask = vec![true, false, true, false];
        let out = apply_token_mask(&logits, &mask);
        assert!(out[0].is_finite());
        assert!(out[1].is_infinite() && out[1] < 0.0);
        assert!(out[2].is_finite());
        assert!(out[3].is_infinite() && out[3] < 0.0);
    }

    #[test]
    fn greedy_argmax_picks_highest_finite() {
        let logits = vec![1.0, f32::NEG_INFINITY, 5.0, 3.0];
        let (idx, lp) = greedy_argmax_logprob(&logits).unwrap();
        assert_eq!(idx, 2);
        assert!(lp < 0.0);
    }

    #[test]
    fn greedy_argmax_errors_on_all_masked() {
        let logits = vec![f32::NEG_INFINITY, f32::NEG_INFINITY];
        assert!(greedy_argmax_logprob(&logits).is_err());
    }

    #[test]
    fn greedy_argmax_errors_on_empty() {
        assert!(greedy_argmax_logprob(&[]).is_err());
    }
}
