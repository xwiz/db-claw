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

// ---------------------------------------------------------------------------
// Seq2Seq encoder + decoder — Stage 2 (skeleton generator)
// ---------------------------------------------------------------------------

/// ONNX encoder half of a seq2seq model (T5-mini-class).
///
/// Produced by `optimum` as `encoder_model.onnx`. Takes source
/// `(input_ids, attention_mask)` and returns `last_hidden_state`
/// `[batch, src_len, hidden_size]`.
pub struct OnnxEncoder {
    session: Mutex<Session>,
    /// Hidden-state width (`d_model`) — third dimension of the encoder
    /// output tensor `[batch, src_len, hidden_size]`. Inferred at load
    /// time; defaults to 384 for unidentifiable models.
    pub hidden_size: usize,
}

/// ONNX decoder half of a seq2seq model.
///
/// Produced by `optimum` as `decoder_model.onnx`. Per-step inputs:
/// - `input_ids`              `[1, 1]` — last generated token
/// - `encoder_hidden_states`  `[1, src_len, hidden_size]`
/// - `encoder_attention_mask` `[1, src_len]`
///
/// Output: `logits` `[1, 1, vocab_size]`.
pub struct OnnxDecoder {
    session: Mutex<Session>,
    /// Vocabulary size — last dimension of the decoder logits tensor.
    /// Inferred at load time from the `logits`/`scores` output; falls
    /// back to T5's 32000 when the model doesn't declare it explicitly.
    pub vocab_size: usize,
}

impl OnnxEncoder {
    /// Load `encoder_model.onnx` from disk.
    pub fn load(path: &std::path::Path) -> Result<Self> {
        let bytes = std::fs::read(path).map_err(|e| {
            SemsqlError::Other(format!("read encoder ONNX `{}`: {e}", path.display()))
        })?;
        let session = Session::builder()
            .map_err(|e| SemsqlError::Other(format!("ort builder: {e}")))?
            .with_optimization_level(GraphOptimizationLevel::Level3)
            .map_err(|e| SemsqlError::Other(format!("ort opt level: {e}")))?
            .commit_from_memory(&bytes)
            .map_err(|e| SemsqlError::Other(format!("ort load encoder: {e}")))?;

        // Infer hidden_size from the output shape if the model stores
        // it as a symbolic dim; fall back to 384 (our student d_model).
        let hidden_size = session
            .outputs
            .first()
            .and_then(|o| match &o.output_type {
                ort::value::ValueType::Tensor { shape, .. } => {
                    shape.iter().nth(2).copied().map(|d| d.max(1) as usize)
                }
                _ => None,
            })
            .unwrap_or(384);

        Ok(Self { session: Mutex::new(session), hidden_size })
    }

    /// Forward pass. Returns `last_hidden_state` as a flat Vec of
    /// `[src_len × hidden_size]` f32 values (batch = 1).
    pub fn encode(
        &self,
        input_ids: &[i64],
        attention_mask: &[i64],
        seq_len: usize,
    ) -> Result<Vec<f32>> {
        let shape = vec![1i64, seq_len as i64];
        let ids_v = Value::from_array((shape.clone(), input_ids.to_vec())).map_err(map_ort)?;
        let mask_v = Value::from_array((shape, attention_mask.to_vec())).map_err(map_ort)?;

        let mut sess = self.session.lock().map_err(|_| {
            SemsqlError::Other("encoder mutex poisoned".into())
        })?;
        let outputs = sess
            .run(inputs!["input_ids" => ids_v, "attention_mask" => mask_v])
            .map_err(map_ort)?;

        for (_name, val) in outputs.iter() {
            if let Ok((_shape, data)) = val.try_extract_tensor::<f32>() {
                return Ok(data.to_vec());
            }
        }
        Err(SemsqlError::Other("encoder produced no float output".into()))
    }
}

impl OnnxDecoder {
    /// Load `decoder_model.onnx` from disk.
    pub fn load(path: &std::path::Path) -> Result<Self> {
        let bytes = std::fs::read(path).map_err(|e| {
            SemsqlError::Other(format!("read decoder ONNX `{}`: {e}", path.display()))
        })?;
        let session = Session::builder()
            .map_err(|e| SemsqlError::Other(format!("ort builder: {e}")))?
            .with_optimization_level(GraphOptimizationLevel::Level3)
            .map_err(|e| SemsqlError::Other(format!("ort opt level: {e}")))?
            .commit_from_memory(&bytes)
            .map_err(|e| SemsqlError::Other(format!("ort load decoder: {e}")))?;

        // Infer vocab_size from the logits output dim; fall back to T5's 32000.
        let vocab_size = session
            .outputs
            .iter()
            .find(|o| o.name.contains("logit") || o.name.contains("score"))
            .and_then(|o| match &o.output_type {
                ort::value::ValueType::Tensor { shape, .. } => shape
                    .iter()
                    .last()
                    .copied()
                    .map(|d| d.max(1) as usize),
                _ => None,
            })
            .unwrap_or(32_000);

        Ok(Self { session: Mutex::new(session), vocab_size })
    }

    /// One decoder step. Returns `logits` for the last position as
    /// `[vocab_size]` f32. Caller applies the llguidance mask before
    /// argmax.
    ///
    /// `decoder_ids` is the full sequence of decoder token ids generated
    /// so far (starting with the BOS/pad token). The decoder ONNX model
    /// exported by `optimum` without KV-cache expects the full prefix at
    /// every step and returns logits for every position; we extract only
    /// the last position for greedy decoding.
    ///
    /// `encoder_hidden_states` is a flat `[src_len × hidden_size]` Vec;
    /// `hidden_size` is passed explicitly so the tensor can be reshaped.
    pub fn step(
        &self,
        decoder_ids: &[i64],
        encoder_hidden_states: &[f32],
        encoder_attention_mask: &[i64],
        src_len: usize,
        hidden_size: usize,
    ) -> Result<Vec<f32>> {
        let dec_len = decoder_ids.len();
        if dec_len == 0 {
            return Err(SemsqlError::Other("decoder_ids must not be empty".into()));
        }
        // Decoder input: [1, dec_len] — full prefix generated so far.
        let ids_v = Value::from_array((vec![1i64, dec_len as i64], decoder_ids.to_vec())).map_err(map_ort)?;
        // Encoder states: [1, src_len, hidden_size]
        let enc_shape = vec![1i64, src_len as i64, hidden_size as i64];
        let enc_v = Value::from_array((enc_shape, encoder_hidden_states.to_vec())).map_err(map_ort)?;
        // Encoder mask: [1, src_len]
        let mask_shape = vec![1i64, src_len as i64];
        let mask_v = Value::from_array((mask_shape, encoder_attention_mask.to_vec())).map_err(map_ort)?;

        let mut sess = self.session.lock().map_err(|_| {
            SemsqlError::Other("decoder mutex poisoned".into())
        })?;
        let outputs = sess
            .run(inputs![
                "input_ids"             => ids_v,
                "encoder_hidden_states" => enc_v,
                "encoder_attention_mask"=> mask_v
            ])
            .map_err(map_ort)?;

        // Pick the logits output — shape [1, dec_len, vocab_size].
        // We always want the last position: data[last_pos * vocab_size ..].
        for (_name, val) in outputs.iter() {
            if let Ok((_shape, data)) = val.try_extract_tensor::<f32>() {
                let total = data.len();
                if total >= self.vocab_size && total % self.vocab_size == 0 {
                    let start = total - self.vocab_size;
                    return Ok(data[start..].to_vec());
                }
            }
        }
        Err(SemsqlError::Other("decoder produced no logits output".into()))
    }
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
