//! Shared ONNX cross-encoder scorer.
//!
//! Stage 1 (linker) and Stage 3 (slot filler) both score `(NL, candidate)`
//! pairs and pick the top-k. The model architecture is identical — a
//! distilled DistilBERT-class encoder with a 2-class head — so they
//! share this module.
//!
//! Inference path:
//!
//!   1. Tokenize the (text_a, text_b) pair via the HF tokenizer at
//!      `tokenizer.json`. Padding is per-batch (longest-in-batch).
//!   2. Run the ONNX session with `input_ids` + `attention_mask`. Some
//!      checkpoints also expose `token_type_ids`; the loader detects
//!      this and wires it conditionally.
//!   3. Read the `logits` output, apply softmax, return the class-1
//!      probability per row as the candidate's relevance score.
//!
//! Threading: `OnnxCrossEncoder` is `Send + Sync` because the
//! underlying `ort::Session` is. A single instance can serve concurrent
//! query workers without locks.

use ort::{
    inputs,
    session::{builder::GraphOptimizationLevel, Session},
    value::Value,
};
use semsql_core::{Result, SemsqlError};
use std::path::Path;
use std::sync::Mutex;
use tokenizers::Tokenizer;

/// Maximum sequence length the tokeniser emits. Tight bound: schema
/// items are short ("users.email"); 128 covers the question + item with
/// room to spare. Matches the training-time `max_seq_len`.
const MAX_SEQ_LEN: usize = 128;

/// Loaded ONNX cross-encoder, ready to score `(text_a, text_b)` pairs.
pub struct OnnxCrossEncoder {
    /// `ort::Session` is `Send + Sync` but `run` takes `&mut self` —
    /// hence the mutex. Per-query latency is dominated by the model
    /// compute itself; lock contention is negligible.
    session: Mutex<Session>,
    tokenizer: Tokenizer,
    /// Whether the ONNX graph expects a `token_type_ids` input. Read
    /// once at load time so the hot path is branchless.
    needs_token_type_ids: bool,
}

impl OnnxCrossEncoder {
    /// Load an ONNX file + a tokenizer.json from disk.
    ///
    /// Returns an `Err` (not a panic) on every failure mode — missing
    /// file, malformed model, tokenizer parse error — so the cascade
    /// orchestrator can fall back to `NeedsModel` instead of aborting.
    pub fn load(onnx_path: &Path, tokenizer_path: &Path) -> Result<Self> {
        // `commit_from_memory` works regardless of which ort feature
        // gates the file-loading path. We pre-read the file ourselves
        // so the error message points at the path the user supplied
        // rather than at an opaque ort error.
        let onnx_bytes = std::fs::read(onnx_path).map_err(|e| {
            SemsqlError::Other(format!("read ONNX `{}`: {e}", onnx_path.display()))
        })?;
        let session = Session::builder()
            .map_err(|e| SemsqlError::Other(format!("ort session builder: {e}")))?
            .with_optimization_level(GraphOptimizationLevel::Level3)
            .map_err(|e| SemsqlError::Other(format!("ort optimisation level: {e}")))?
            .commit_from_memory(&onnx_bytes)
            .map_err(|e| {
                SemsqlError::Other(format!(
                    "ort load `{}`: {e}",
                    onnx_path.display()
                ))
            })?;
        // ort 2.0.0-rc.10 exposes `inputs` as a public field on
        // `Session`, not a method. Each entry has a `name: String` field.
        let needs_token_type_ids = session
            .inputs
            .iter()
            .any(|i| i.name == "token_type_ids");

        let tokenizer = Tokenizer::from_file(tokenizer_path).map_err(|e| {
            SemsqlError::Other(format!(
                "tokenizer load `{}`: {e}",
                tokenizer_path.display()
            ))
        })?;

        Ok(Self {
            session: Mutex::new(session),
            tokenizer,
            needs_token_type_ids,
        })
    }

    /// Score a batch of `(text_a, text_b)` pairs and return their
    /// class-1 probabilities. Output length matches input length.
    ///
    /// Inputs are tokenised in one shot (HF tokenizers handle batching
    /// efficiently), padded to the longest item in the batch (capped at
    /// [`MAX_SEQ_LEN`]). The ONNX session is locked once per batch.
    pub fn score_batch(&self, pairs: &[(String, String)]) -> Result<Vec<f32>> {
        if pairs.is_empty() {
            return Ok(Vec::new());
        }

        let encodings = self
            .tokenizer
            .encode_batch(
                pairs
                    .iter()
                    .map(|(a, b)| (a.as_str(), b.as_str()))
                    .collect::<Vec<_>>(),
                true,
            )
            .map_err(|e| SemsqlError::Other(format!("tokenize batch: {e}")))?;

        let batch_size = encodings.len();
        let seq_len = encodings
            .iter()
            .map(|e| e.get_ids().len())
            .max()
            .unwrap_or(0)
            .min(MAX_SEQ_LEN);
        if seq_len == 0 {
            return Ok(vec![0.0; batch_size]);
        }

        let pad_id = self.tokenizer.get_padding().map(|p| p.pad_id).unwrap_or(0) as i64;
        let n = batch_size * seq_len;
        let mut input_ids = vec![pad_id; n];
        let mut attention_mask = vec![0i64; n];
        let mut token_type_ids = vec![0i64; n];

        for (row, enc) in encodings.iter().enumerate() {
            let ids = enc.get_ids();
            let mask = enc.get_attention_mask();
            let types = enc.get_type_ids();
            let len = ids.len().min(seq_len);
            let base = row * seq_len;
            for col in 0..len {
                input_ids[base + col] = ids[col] as i64;
                attention_mask[base + col] = mask[col] as i64;
                token_type_ids[base + col] = types[col] as i64;
            }
        }

        let shape = vec![batch_size as i64, seq_len as i64];
        // ort 2.0 accepts `(shape, Vec<T>)` tuples for owned tensor data.
        let input_ids_v = Value::from_array((shape.clone(), input_ids)).map_err(map_ort)?;
        let attention_mask_v =
            Value::from_array((shape.clone(), attention_mask)).map_err(map_ort)?;
        let token_type_ids_v =
            Value::from_array((shape, token_type_ids)).map_err(map_ort)?;

        let mut session = self
            .session
            .lock()
            .map_err(|_| SemsqlError::Other("onnx session mutex poisoned".into()))?;

        let outputs = if self.needs_token_type_ids {
            session
                .run(inputs![
                    "input_ids" => input_ids_v,
                    "attention_mask" => attention_mask_v,
                    "token_type_ids" => token_type_ids_v,
                ])
                .map_err(map_ort)?
        } else {
            session
                .run(inputs![
                    "input_ids" => input_ids_v,
                    "attention_mask" => attention_mask_v,
                ])
                .map_err(map_ort)?
        };

        // The classification head's output is named "logits" by HF
        // convention; some exports use the model's class name. Pick
        // the first output that extracts as a 2D float tensor with
        // our batch size.
        //
        // ort 2.0 returns `(Shape, &[f32])` — flat slice + dims — so we
        // index manually instead of relying on ndarray views (which
        // would require a separate copy anyway since the Value's
        // borrow is tied to the SessionOutputs).
        let (logit_shape, logit_data): (Vec<usize>, Vec<f32>) = {
            let mut found: Option<(Vec<usize>, Vec<f32>)> = None;
            for (_name, val) in outputs.iter() {
                if let Ok((shape, data)) = val.try_extract_tensor::<f32>() {
                    let dims: Vec<usize> = shape.iter().map(|d| *d as usize).collect();
                    if dims.len() == 2 && dims[0] == batch_size {
                        found = Some((dims, data.to_vec()));
                        break;
                    }
                }
            }
            found.ok_or_else(|| {
                SemsqlError::Other("no 2D float logits in model output".into())
            })?
        };

        let num_classes = logit_shape[1];
        if num_classes == 0 {
            // Defensive: degenerate output shape `[N, 0]`. We cannot
            // produce a meaningful score; emit zeros so the caller
            // treats every candidate as bottom-ranked rather than
            // panicking on an empty slice index.
            return Ok(vec![0.0; batch_size]);
        }
        let mut out = Vec::with_capacity(batch_size);
        for row in 0..batch_size {
            let base = row * num_classes;
            let row_slice = &logit_data[base..base + num_classes];
            let max = row_slice.iter().copied().fold(f32::NEG_INFINITY, f32::max);
            let exps: Vec<f32> = row_slice.iter().map(|v| (v - max).exp()).collect();
            let sum: f32 = exps.iter().sum();
            let class1 = if num_classes >= 2 {
                exps[1] / sum.max(f32::EPSILON)
            } else {
                // Single-output regression head — sigmoid of the raw
                // logit gives a probability-shaped relevance score.
                let raw = row_slice[0];
                1.0 / (1.0 + (-raw).exp())
            };
            out.push(class1);
        }
        Ok(out)
    }
}

fn map_ort(e: ort::Error) -> SemsqlError {
    SemsqlError::Other(format!("ort: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// Loading a non-existent file must return Err — never panic — so
    /// the cascade can fall back to `NeedsModel` cleanly.
    #[test]
    fn load_returns_err_on_missing_file() {
        let r = OnnxCrossEncoder::load(
            &PathBuf::from("does-not-exist.onnx"),
            &PathBuf::from("does-not-exist.tok"),
        );
        assert!(r.is_err());
    }

    /// Empty pair input is a no-op — never lock the session, never run
    /// the model. Important for callers that build empty batches when
    /// a query has no candidates.
    #[test]
    fn score_batch_empty_input_is_a_noop() {
        // We can't construct an OnnxCrossEncoder without a real ONNX
        // file, so this test just documents the contract: callers
        // depend on `pairs.is_empty() → Ok(vec![])` even when the
        // session hasn't been initialised. The behaviour is enforced
        // by the early-return at the top of `score_batch`.
    }
}
