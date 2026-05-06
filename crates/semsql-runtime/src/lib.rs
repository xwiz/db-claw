//! Cascade orchestrator.
//!
//! Drives the runtime pipeline:
//!
//! ```text
//! NL query
//!   → Stage 0a — Vocabulary Pre-resolver           (<1 ms, deterministic)
//!   → Stage 0b — Intent Pattern Library            (<1 ms, deterministic)
//!   → Stage 1  — Schema Linker                      (<5 ms, ~10M params)
//!   → Stage 2  — Skeleton Generator + llguidance    (<15 ms, ~20M params)
//!   → Stage 3  — Slot Filler + IntentResolver       (<3 ms, ~5M params)
//!   → Stage 4  — NatSQL → SQL transpiler            (<1 ms, deterministic)
//!   → (Python rewriter validates + injects scope)
//!   → Second-pass re-validator
//!   → Dialect renderer
//! ```
//!
//! v0.1 wires Stages 0a + 0b + 4 with deterministic logic only — model
//! stages return [`SemsqlError::Other`] so the rewriter pipeline can be
//! exercised end-to-end on fully-mapped vocabulary before the cascade is
//! trained.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod grammar;
pub mod manifest;
pub mod normalize;
#[cfg(feature = "onnx")]
pub mod onnx;
pub mod stage_linker;
pub mod stage_pre_resolver;
pub mod stage_skeleton;
pub mod stage_slotfiller;

use semsql_core::{Result, SemsqlError};
use semsql_intent::{IntentHint, IntentLibrary};
use semsql_natsql::{parse as parse_natsql, transpile::to_sql_text};
use std::path::Path;
use std::time::Instant;

/// One end-to-end run through the cascade. Returned by [`run`].
#[derive(Clone, Debug)]
pub struct CascadeOutcome {
    /// Final SQL text emitted by Stage 4 (pre-rewriter).
    pub sql_text: String,
    /// Per-stage timings, in microseconds.
    pub timings_us: PerStageTimings,
    /// Confidence reported by each model stage (Stage 0a/0b are 1.0).
    pub confidences: PerStageConfidence,
    /// Intent hints that fired in Stage 0b — surfaced for telemetry.
    pub intent_hints: Vec<String>,
}

/// Per-stage wall-clock timings.
#[derive(Copy, Clone, Debug, Default)]
pub struct PerStageTimings {
    /// Vocabulary pre-resolver.
    pub stage_0a: u64,
    /// Intent pattern matcher.
    pub stage_0b: u64,
    /// Schema linker.
    pub stage_1: u64,
    /// Skeleton generator (incl. llguidance).
    pub stage_2: u64,
    /// Slot filler + IntentResolver.
    pub stage_3: u64,
    /// NatSQL → SQL transpile.
    pub stage_4: u64,
}

/// Per-stage confidence scores (`0.0..=1.0`).
#[derive(Copy, Clone, Debug, Default)]
pub struct PerStageConfidence {
    /// Stage 1 top-1 score.
    pub stage_1: f32,
    /// Stage 2 mean token log-prob.
    pub stage_2: f32,
    /// Stage 3 mean per-slot top-1.
    pub stage_3: f32,
}

/// Cached, reusable orchestrator. Builds the pre-resolver index once and
/// the intent library once; per-query work is bounded by the cascade
/// stages themselves.
pub struct Cascade {
    pre_resolver: stage_pre_resolver::PreResolverIndex,
    intent_library: Option<IntentLibrary>,
}

impl Cascade {
    /// Build a cascade by loading the SemanticGraph at `graph_path`.
    /// Optionally also load an intent pattern YAML.
    pub fn load(
        graph_path: impl AsRef<Path>,
        intent_yaml_path: Option<&Path>,
    ) -> Result<Self> {
        let pre_resolver = stage_pre_resolver::PreResolverIndex::load(graph_path)?;
        let intent_library = match intent_yaml_path {
            Some(p) => Some(IntentLibrary::load_from_path(p)?),
            None => None,
        };
        Ok(Self {
            pre_resolver,
            intent_library,
        })
    }

    /// Run the cascade against `nl`. Returns the final SQL text plus
    /// per-stage telemetry. SQL is *pre-rewriter* — the Python rewriter
    /// (sqlglot validator + injector) and Rust second-pass must run on
    /// the output before it touches a database.
    pub fn run(&self, nl: &str) -> Result<CascadeOutcome> {
        let mut timings = PerStageTimings::default();
        let mut confidences = PerStageConfidence::default();

        let t0 = Instant::now();
        let pre = stage_pre_resolver::resolve(nl, &self.pre_resolver);
        timings.stage_0a = t0.elapsed().as_micros() as u64;

        let t1 = Instant::now();
        let intents = match &self.intent_library {
            Some(lib) => lib.r#match(&normalize::normalize(nl)),
            None => Vec::new(),
        };
        timings.stage_0b = t1.elapsed().as_micros() as u64;

        match pre {
            stage_pre_resolver::PreResolveOutcome::Resolved { natsql, confidence } => {
                let t4 = Instant::now();
                let ast = parse_natsql(&natsql)?;
                let sql = to_sql_text(&ast)?;
                timings.stage_4 = t4.elapsed().as_micros() as u64;
                confidences.stage_1 = confidence; // pre-resolver carried full conf
                Ok(CascadeOutcome {
                    sql_text: sql,
                    timings_us: timings,
                    confidences,
                    intent_hints: intent_types(&intents),
                })
            }
            stage_pre_resolver::PreResolveOutcome::NeedsModel => Err(SemsqlError::Other(
                "model stages (Stage 1/2/3) not wired in v0.2 — pre-resolver \
                 could not pin every token. Either rephrase or wait for \
                 the trained cascade weights."
                    .into(),
            )),
        }
    }
}

/// Convenience: load + run in a single call. Re-loads the graph each
/// invocation; prefer [`Cascade::load`] when running many queries against
/// the same graph.
pub fn run(graph_path: impl AsRef<Path>, nl: &str) -> Result<CascadeOutcome> {
    let cascade = Cascade::load(graph_path, None)?;
    cascade.run(nl)
}

fn intent_types(hits: &[IntentHint]) -> Vec<String> {
    hits.iter().map(|h| h.intent_type.clone()).collect()
}
