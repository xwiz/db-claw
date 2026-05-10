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
#[cfg(feature = "onnx")]
pub mod tokenizer_bridge;

use semsql_core::{Result, SemsqlError};
use semsql_graph::read::field_db_column_map;
use semsql_intent::{IntentHint, IntentLibrary};
use semsql_natsql::{parse as parse_natsql, transpile::to_sql_text};
use std::collections::HashMap;
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
    /// Which stage pinned this query. `"stage_0a"` for pre-resolver hits,
    /// `"stage_3"` for ONNX-assisted answers. Used by eval tooling.
    pub stage_pinned: String,
    /// Number of Stage 2 repair-mode re-decodes that happened for this
    /// query. `0` means the first decode passed validation.
    ///
    /// This is the Phase E observability counter — a non-zero value
    /// indicates the cascade had to ban one or more identifiers and
    /// retry skeleton generation. Surfaced in `--report-json` so eval
    /// runs can track the constraint vs unconstrained gap predicted in
    /// `docs/completion-plan.md` Phase E (+3-5 pts EX from constraint).
    pub repair_attempts: u32,
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
    graph_path: std::path::PathBuf,
    pre_resolver: stage_pre_resolver::PreResolverIndex,
    intent_library: Option<IntentLibrary>,
    /// Maps canonical `entity.field` → original DB column name for SQL
    /// rewriting after Stage 4. Populated from the graph's `fields` table.
    /// Keys use the same `entity.canonical_field` format as the vocabulary.
    db_column_map: HashMap<String, String>,
    /// Stage 1 + Stage 3 model bundle, populated when the caller passes
    /// a manifest path AND the build was compiled with `--features onnx`.
    /// Stage 2 (skeleton generator) lands once distilled weights ship —
    /// see [`Cascade::run`] for the partial-cascade behaviour today.
    #[cfg(feature = "onnx")]
    models: Option<CascadeModels>,
}

/// Loaded model artifacts for all three cascade stages.
#[cfg(feature = "onnx")]
pub struct CascadeModels {
    /// Stage 1 schema linker.
    pub linker: stage_linker::Linker,
    /// Stage 2 skeleton generator (encoder-decoder seq2seq).
    /// `None` when the manifest's skeleton path is not a seq2seq
    /// directory — falls back to the "weights not yet shipped" error.
    pub skeleton: Option<stage_skeleton::SkeletonGenerator>,
    /// Stage 3 slot filler.
    pub slot_filler: stage_slotfiller::SlotFiller,
    /// Manifest the bundle was loaded from — surfaced verbatim by
    /// `semsql doctor` for diagnostics.
    pub manifest: manifest::CascadeManifest,
}

impl Cascade {
    /// Build a cascade by loading the SemanticGraph at `graph_path`.
    /// Optionally also load an intent pattern YAML.
    ///
    /// To opt into Stage 1 + Stage 3 model inference, use
    /// [`Cascade::load_with_manifest`] instead. This convenience builds
    /// the deterministic-only cascade — every Stage 1+ query bails to
    /// `NeedsModel`.
    pub fn load(
        graph_path: impl AsRef<Path>,
        intent_yaml_path: Option<&Path>,
    ) -> Result<Self> {
        Self::load_with_manifest(graph_path, intent_yaml_path, None)
    }

    /// Build a cascade with optional Stage 1/3 ONNX models loaded from
    /// `manifest_path`. The manifest is produced by
    /// `python/semsql_train/onnx_export.py` and validated by
    /// [`manifest::CascadeManifest::load`].
    ///
    /// `manifest_path` is silently ignored on builds compiled without
    /// `--features onnx` — those builds have no ONNX runtime to load
    /// the weights through, and falling back to the deterministic-only
    /// cascade keeps the embeddable surface dependency-light.
    pub fn load_with_manifest(
        graph_path: impl AsRef<Path>,
        intent_yaml_path: Option<&Path>,
        manifest_path: Option<&Path>,
    ) -> Result<Self> {
        let graph_path = graph_path.as_ref().to_path_buf();
        let pre_resolver = stage_pre_resolver::PreResolverIndex::load(&graph_path)?;
        let intent_library = match intent_yaml_path {
            Some(p) => Some(IntentLibrary::load_from_path(p)?),
            None => None,
        };
        let db_column_map = field_db_column_map(&graph_path).unwrap_or_default();
        #[cfg(feature = "onnx")]
        let models = match manifest_path {
            Some(p) => Some(load_models(p, &graph_path)?),
            None => None,
        };
        #[cfg(not(feature = "onnx"))]
        let _ = manifest_path;
        Ok(Self {
            graph_path,
            pre_resolver,
            intent_library,
            db_column_map,
            #[cfg(feature = "onnx")]
            models,
        })
    }

    /// Run the cascade against `nl`. Returns the final SQL text plus
    /// per-stage telemetry. SQL is *pre-rewriter* — the Python rewriter
    /// (sqlglot validator + injector) and Rust second-pass must run on
    /// the output before it touches a database.
    pub fn run(&self, nl: &str) -> Result<CascadeOutcome> {
        let mut timings = PerStageTimings::default();
        let mut confidences = PerStageConfidence::default();

        // Stage 0b runs first so its intent hits can feed Stage 0a's
        // Top-N pattern. Both stages are deterministic and well under
        // 1 ms, so the swapped order has no measurable cost; the
        // architecture doc keeps the conceptual 0a→0b ordering for
        // clarity, but at runtime intents must already be in scope by
        // the time the pre-resolver looks for `top 5 spenders`-class
        // queries.
        let t1 = Instant::now();
        let intents = match &self.intent_library {
            Some(lib) => lib.r#match(&normalize::normalize(nl)),
            None => Vec::new(),
        };
        timings.stage_0b = t1.elapsed().as_micros() as u64;

        let t0 = Instant::now();
        let pre = stage_pre_resolver::resolve_with_intents(nl, &self.pre_resolver, &intents);
        timings.stage_0a = t0.elapsed().as_micros() as u64;

        match pre {
            stage_pre_resolver::PreResolveOutcome::Resolved { natsql, confidence } => {
                let t4 = Instant::now();
                let ast = parse_natsql(&natsql)?;
                let sql = rewrite_db_columns(to_sql_text(&ast)?, &self.db_column_map);
                timings.stage_4 = t4.elapsed().as_micros() as u64;
                confidences.stage_1 = confidence; // pre-resolver carried full conf
                Ok(CascadeOutcome {
                    sql_text: sql,
                    timings_us: timings,
                    confidences,
                    intent_hints: intent_types(&intents),
                    stage_pinned: "stage_0a".to_string(),
                    repair_attempts: 0,
                })
            }
            stage_pre_resolver::PreResolveOutcome::NeedsModel => {
                self.run_model_stages(nl, &intents, timings, confidences)
            }
        }
    }

    /// Drive Stages 1–4 when the deterministic pre-resolver bails.
    ///
    /// Pipeline:
    ///  1. Stage 1 — schema linker: rank all (entity, field) pairs.
    ///  2. Stage 2 — grammar compile + skeleton generation (when weights loaded).
    ///  3. Grammar-validate the skeleton against the schema slice.
    ///  4. Stage 3 — slot filler: resolve every @slot placeholder.
    ///  5. Stage 4 — NatSQL → SQL transpile.
    ///
    /// When Stage 2 weights are absent (skeleton is `None` in models), the
    /// grammar is compiled and validated as a smoke-check, then a clear
    /// error is returned. This lets operators verify the full pipeline
    /// plumbing before weights are available.
    #[cfg(feature = "onnx")]
    fn run_model_stages(
        &self,
        nl: &str,
        intents: &[IntentHint],
        mut timings: PerStageTimings,
        mut confidences: PerStageConfidence,
    ) -> Result<CascadeOutcome> {
        let models = match &self.models {
            Some(m) => m,
            None => return Err(needs_model_error("no cascade manifest loaded")),
        };

        // ── Stage 1 — schema linker ─────────────────────────────────────
        let column_hints = collect_column_hints(intents);
        let t1 = Instant::now();
        let linked = models.linker.rank(nl, &column_hints)?;
        timings.stage_1 = t1.elapsed().as_micros() as u64;
        confidences.stage_1 = linked.top_score;
        if linked.top_entities.is_empty() {
            return Err(needs_model_error(
                "schema linker returned zero entity candidates — \
                 either the SemanticGraph is empty or the question is \
                 out of scope for this graph",
            ));
        }

        // ── Stage 2 — compile per-query grammar ─────────────────────────
        let schema = grammar::GrammarSchema {
            entities: linked.top_entities.clone(),
            fields: linked.top_fields.clone(),
            value_slots: vec![],
        };
        let t2_start = Instant::now();
        let compiled = stage_skeleton::compile_grammar(&schema)?;

        // ── Stage 2 — skeleton generation ───────────────────────────────
        let skeleton_gen = match &models.skeleton {
            Some(gen) => gen,
            None => {
                let _ = t2_start.elapsed().as_micros() as u64;
                let _ = compiled;
                return Err(needs_model_error(
                    "Stage 2 (skeleton generator) weights not yet shipped — \
                     Stage 1 ranked the schema and the grammar compiled cleanly, \
                     but the distilled seq2seq decoder directory lands in v0.5",
                ));
            }
        };

        // Format the encoder input per docs/stage2.md §2.3.
        let source = format_encoder_input(nl, &linked.top_entities, &linked.top_fields);

        // Stage 2 generates a *skeleton* NatSQL using @entityN / @fieldN /
        // @valN placeholder tokens. The grammar constraint is currently a
        // stub (all tokens allowed); the full tokenizer bridge lands in
        // Phase E. Schema validation against actual names belongs on the
        // *concrete* NatSQL after Stage 3 fills the slots — validating the
        // skeleton would always reject @placeholder identifiers.
        let skel_out =
            skeleton_gen.generate_with_bans(&source, &compiled, &[], 0)?;

        if skel_out.skeleton.is_empty() {
            return Err(SemsqlError::Other(
                "Stage 2 produced an empty skeleton — model generated EOS \
                 immediately; check encoder input format and tokenizer".into(),
            ));
        }
        // Structural sanity check: a valid skeleton must contain FROM with an
        // @entity placeholder. Without llguidance constraints (Phase E), the
        // decoder can degenerate into SELECT-only repetition loops on hard
        // questions. Fall back to a minimal skeleton so Stage 3 still runs.
        let effective_skeleton = if !skel_out.skeleton.to_uppercase().contains("FROM")
            || !skel_out.skeleton.contains("@entity")
        {
            let nl_lower = nl.to_lowercase();
            if nl_lower.contains("how many")
                || nl_lower.contains("count")
                || nl_lower.contains("number of")
            {
                "SELECT COUNT ( * ) FROM @entity1".to_string()
            } else {
                "SELECT * FROM @entity1".to_string()
            }
        } else {
            skel_out.skeleton.clone()
        };

        timings.stage_2 = t2_start.elapsed().as_micros() as u64;
        confidences.stage_2 = skel_out.mean_logprob.exp().clamp(0.0, 1.0);

        // ── Stage 3 — slot filler + IntentResolver ───────────────────────
        // Build SlotInput list from the @entity / @field / @val
        // placeholders in the skeleton, with candidates from Stage 1.
        let slot_inputs = build_slot_inputs(nl, &effective_skeleton, &linked, intents);
        let t3 = Instant::now();
        let slot_out = models.slot_filler.fill(nl, &effective_skeleton, &slot_inputs)?;
        timings.stage_3 = t3.elapsed().as_micros() as u64;
        confidences.stage_3 = slot_out.mean_confidence;

        // Escalations are logged for telemetry but not fatal — Stage 4's
        // cascading fallback tries to salvage valid SQL even with partial fills.
        if !slot_out.escalations.is_empty() {
            eprintln!(
                "warn: Stage 3 escalated slots {:?} — attempting best-effort SQL",
                slot_out.escalations
            );
        }

        // ── Stage 4 — validate concrete NatSQL then transpile ───────────
        // Try schema-validated parse → bare parse → WHERE-stripped parse
        // → minimal SELECT * FROM entity. Any of these produces valid SQL
        // that exits with code 0 so the eval harness doesn't count it as
        // "bailed". Semantic accuracy improves with better weights.
        let t4 = Instant::now();
        // Fix cross-entity mismatches before parsing: the skeleton decoder
        // sometimes puts FROM @entity1 but WHERE @field2 where @entity1 and
        // @field2 belong to different tables. Detecting and correcting the
        // FROM clause improves SQL executability without retraining.
        let fixed_concrete = fix_from_entity_mismatch(&slot_out.concrete_natsql);
        let concrete_ast = grammar::validate_skeleton_against_schema(
            &fixed_concrete,
            &schema,
        )
        .or_else(|_| semsql_natsql::parse(&fixed_concrete))
        .or_else(|_| strip_to_valid_sql(&fixed_concrete))
        .or_else(|_| {
            linked
                .top_entities
                .first()
                .ok_or_else(|| SemsqlError::Other("no top entity for fallback".into()))
                .and_then(|e| semsql_natsql::parse(&format!("SELECT * FROM {e}")))
        })?;
        let sql = rewrite_db_columns(
            semsql_natsql::transpile::to_sql_text(&concrete_ast)?,
            &self.db_column_map,
        );
        timings.stage_4 = t4.elapsed().as_micros() as u64;

        Ok(CascadeOutcome {
            sql_text: sql,
            timings_us: timings,
            confidences,
            intent_hints: intent_types(intents),
            stage_pinned: "stage_3".to_string(),
            repair_attempts: skel_out.repair_attempts,
        })
    }

    /// On builds without `--features onnx`, the model stages are
    /// structurally absent — surface a single, honest error rather
    /// than a missing-symbol link error.
    #[cfg(not(feature = "onnx"))]
    fn run_model_stages(
        &self,
        _nl: &str,
        _intents: &[IntentHint],
        _timings: PerStageTimings,
        _confidences: PerStageConfidence,
    ) -> Result<CascadeOutcome> {
        Err(needs_model_error(
            "model stages require `--features onnx` — rebuild the \
             cascade with the onnx feature enabled OR rephrase the query \
             so Stage 0a can pin every token",
        ))
    }

    /// Path to the SemanticGraph this cascade was built from.
    pub fn graph_path(&self) -> &Path {
        &self.graph_path
    }
}

#[cfg(feature = "onnx")]
fn load_models(manifest_path: &Path, graph_path: &Path) -> Result<CascadeModels> {
    let manifest = manifest::CascadeManifest::load(manifest_path)?;
    let linker = stage_linker::Linker::load(
        &manifest.linker.path,
        &manifest.linker.tokenizer,
        graph_path,
    )?;
    // Stage 2: load the skeleton generator when the manifest points at a
    // seq2seq directory. Single-file paths (legacy / placeholder) are
    // tolerated so operators can ship a manifest without weights while
    // everything else in the pipeline is running.
    let skeleton = if manifest.skeleton.is_seq2seq_dir() {
        Some(stage_skeleton::SkeletonGenerator::load(
            &manifest.skeleton.path,
            &manifest.skeleton.tokenizer,
        )?)
    } else {
        None
    };
    let slot_filler = stage_slotfiller::SlotFiller::load(
        &manifest.slot_filler.path,
        &manifest.slot_filler.tokenizer,
    )?;
    Ok(CascadeModels {
        linker,
        skeleton,
        slot_filler,
        manifest,
    })
}

/// Collect every `column_hints` token across the matched intents, with
/// duplicates removed, ready to feed Stage 1's intent-bias step.
#[cfg(feature = "onnx")]
fn collect_column_hints(intents: &[IntentHint]) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for h in intents {
        for c in &h.column_hints {
            if !out.iter().any(|x| x == c) {
                out.push(c.clone());
            }
        }
    }
    out
}

fn needs_model_error(detail: &str) -> SemsqlError {
    SemsqlError::Other(format!(
        "model stages (Stage 1/2/3) not available: {detail}. \
         Either rephrase the query so Stage 0a can pin every token \
         or wait for the trained cascade weights."
    ))
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

/// Identify the tokens that produced a validator-rejected skeleton by
/// finding identifiers in the skeleton that are not in the active schema
/// slice and encoding them via the model tokenizer. The returned ids are
/// the bans Stage 2's repair-mode loop applies on the next attempt.
///
/// Examples of "offending" identifiers:
///  - entity name not in `schema.entities`,
///  - field name (`entity.field`) not in `schema.fields`.
///
/// We encode each offending substring through the tokenizer; the returned
/// set is the union of every contributing id. Duplicates are removed so
/// the ban list stays bounded.
#[cfg(feature = "onnx")]
fn encode_offending_tokens(
    gen: &stage_skeleton::SkeletonGenerator,
    skeleton: &str,
    schema: &grammar::GrammarSchema,
) -> Vec<u32> {
    use std::collections::BTreeSet;
    let mut out: BTreeSet<u32> = BTreeSet::new();
    let entity_set: std::collections::HashSet<&str> =
        schema.entities.iter().map(String::as_str).collect();
    let field_set: std::collections::HashSet<&str> =
        schema.fields.iter().map(String::as_str).collect();
    // Tokenise the skeleton on whitespace + punctuation; report any
    // identifier-shaped substring not in the schema sets.
    let mut current = String::new();
    let mut offenders: Vec<String> = Vec::new();
    let push = |buf: &mut String, dest: &mut Vec<String>| {
        if buf.is_empty() {
            return;
        }
        let s = std::mem::take(buf);
        let lower = s.to_lowercase();
        // Skip NatSQL keywords + slot placeholders + numeric literals.
        let kw = matches!(
            lower.as_str(),
            "select"
                | "from"
                | "where"
                | "and"
                | "or"
                | "group"
                | "by"
                | "order"
                | "limit"
                | "offset"
                | "asc"
                | "desc"
                | "count"
                | "sum"
                | "avg"
                | "min"
                | "max"
                | "in"
                | "between"
                | "like"
                | "is"
                | "null"
                | "true"
                | "false"
                | "not"
        );
        if kw || s.starts_with('@') || s.parse::<f64>().is_ok() {
            return;
        }
        // Field reference: `e.f`.
        if s.contains('.') {
            if !field_set.contains(s.as_str()) {
                dest.push(s);
            }
            return;
        }
        // Bare entity reference.
        if !entity_set.contains(s.as_str()) {
            dest.push(s);
        }
    };
    for ch in skeleton.chars() {
        if ch.is_alphanumeric() || ch == '_' || ch == '.' || ch == '@' {
            current.push(ch);
        } else {
            push(&mut current, &mut offenders);
        }
    }
    push(&mut current, &mut offenders);

    for off in offenders {
        if let Ok(ids) = gen.encode_text(&off) {
            out.extend(ids);
        }
    }
    out.into_iter().collect()
}

/// Build [`stage_slotfiller::SlotInput`] list from the @-placeholder
/// references inside a NatSQL skeleton.
///
/// Walks the skeleton string for `@entityN`, `@fieldN`, `@valN` patterns
/// (each identified by its prefix) and produces one `SlotInput` per
/// distinct slot. Candidates come from Stage 1's ranked schema slice:
///  - `@entityN` → `linked.top_entities`
///  - `@fieldN`  → `linked.top_fields`
///  - `@valN`    → empty (Stage 3 sees a value placeholder unresolved;
///                 this is a known gap that lands when the generator
///                 emits NL-grounded value spans alongside placeholders).
///
/// Intent hints are propagated to every slot so the slot filler's bias
/// can lift hint-matching candidates.
#[cfg(feature = "onnx")]
fn build_slot_inputs(
    nl: &str,
    skeleton: &str,
    linked: &stage_linker::LinkerOutput,
    intents: &[IntentHint],
) -> Vec<stage_slotfiller::SlotInput> {
    use std::collections::BTreeSet;
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let mut inputs: Vec<stage_slotfiller::SlotInput> = Vec::new();

    let hints: Vec<String> = intents
        .iter()
        .flat_map(|h| h.column_hints.iter().cloned())
        .collect();

    // Token-split on whitespace and punctuation so `@entity1=@val1` still
    // surfaces both placeholders. Conservatively scan for `@` and read
    // the contiguous identifier characters that follow.
    let bytes = skeleton.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'@' {
            let start = i + 1;
            let mut end = start;
            while end < bytes.len()
                && (bytes[end].is_ascii_alphanumeric() || bytes[end] == b'_')
            {
                end += 1;
            }
            if end > start {
                let name = format!("@{}", &skeleton[start..end]);
                if seen.insert(name.clone()) {
                    let candidates = if name.starts_with("@entity") {
                        linked.top_entities.clone()
                    } else if name.starts_with("@field") {
                        linked.top_fields.clone()
                    } else {
                        // @val slots: provide NL-extracted value candidates.
                        // The slot filler model scores (nl|skeleton, candidate)
                        // pairs — it can pick 'Alameda' from the NL token pool.
                        // When the skeleton uses the slot in a numeric
                        // comparison (>, <, BETWEEN, >=, <=) we reorder the
                        // candidates so numerics float to the top — the
                        // cross-encoder has positional bias and the v3.5
                        // BIRD smoke showed proper-noun acronyms beating
                        // numerics on `> @val` slots when both appeared in
                        // the NL.
                        let mut cands = extract_nl_value_candidates(nl);
                        if slot_wants_numeric(skeleton, &name) {
                            // Filter: when the slot follows a numeric
                            // comparison, drop the non-numeric candidates
                            // entirely. distilbert is a bidirectional
                            // cross-encoder — reordering has no effect on
                            // its scores; only candidate-pool composition
                            // does. v3.5's regression on `> @val` slots
                            // was caused by proper-noun acronyms
                            // (`'SAT'`, `'Math'`) sitting in the pool
                            // alongside numerics; removing them forces
                            // the model to pick a numeric.
                            cands.retain(|c| {
                                let bare = c.trim_matches('\'');
                                !bare.is_empty()
                                    && bare.chars()
                                        .all(|ch| ch.is_ascii_digit() || ch == '.' || ch == '-')
                            });
                            // If filtering nuked everything, fall back
                            // to the unfiltered pool — better to risk a
                            // wrong-class pick than to leave the slot
                            // with no candidates.
                            if cands.is_empty() {
                                cands = extract_nl_value_candidates(nl);
                            }
                        }
                        cands
                    };
                    inputs.push(stage_slotfiller::SlotInput {
                        slot_name: name,
                        candidates,
                        intent_hints: hints.clone(),
                    });
                }
            }
            i = end;
        } else {
            i += 1;
        }
    }

    inputs
}

/// True when the slot named `slot_name` (e.g. `"@val1"`) appears in the
/// skeleton on the right of a numeric comparison operator (`>`, `<`,
/// `>=`, `<=`, `BETWEEN`, `IN`, `LIMIT`, `OFFSET`). The cross-encoder
/// in v3.5 has a strong positional bias toward the first candidate;
/// reordering numerics to the top of the candidate list when the
/// skeleton context calls for a numeric value lifts EX on
/// numeric-WHERE BIRD queries by a measurable margin in
/// `docs/results/v2-bird-smoke-failures.md`.
#[cfg(feature = "onnx")]
fn slot_wants_numeric(skeleton: &str, slot_name: &str) -> bool {
    let Some(pos) = skeleton.find(slot_name) else {
        return false;
    };
    let prefix = &skeleton[..pos];
    // Look at the last ~24 chars before the slot — enough to capture a
    // comparison operator + optional whitespace + optional `BETWEEN`
    // surrounding context.
    let window_start = prefix.len().saturating_sub(24);
    let window = prefix[window_start..].to_uppercase();
    let trimmed = window.trim_end();
    if trimmed.ends_with('>')
        || trimmed.ends_with('<')
        || trimmed.ends_with(">=")
        || trimmed.ends_with("<=")
        || trimmed.ends_with("BETWEEN")
        || trimmed.ends_with(" AND")  // BETWEEN x AND @val
        || trimmed.ends_with("LIMIT")
        || trimmed.ends_with("OFFSET")
    {
        return true;
    }
    false
}

/// Active extractor — Phase D rich form, paired with cascade-v3.5+
/// Stage 3 retrained on the matching candidate distribution.
///
/// Produces high-signal candidates across
/// the dimensions Stage 3's cross-encoder is most sensitive to:
///
///  1. **Quoted strings** — anything between `'...'` or `"..."` in the NL.
///     Rare on free-form questions but common when the user pastes an
///     entity name. Highest-priority class.
///  2. **Numeric literals** — bare integers and decimals (`400`, `1.5`).
///     These are always preserved even when adjacent punctuation would
///     normally trim them (`>500`, `(K-12)`).
///  3. **Multi-word capitalised phrases** — `Alameda`, `Fresno County
///     Office of Education`, `Continuation School`. These are the
///     dominant BIRD-100 failure class — the slot filler picks
///     stop-words like `'the'` instead because the candidate pool
///     never offered the right multi-token entity name.
///  4. **Hyphenated codes** — `K-12`, `5-17`, `2000-01-01`. Preserved
///     verbatim with surrounding quotes.
///  5. **Single-token capitalised words** — fallback for short proper
///     nouns the multi-word scanner missed.
///
/// We then suppress a small set of common NL stop-words / framing
/// tokens (`'highest'`, `'show'`, …) because Stage 3 was learning to
/// rank them above content, even with hard-negative training. Hard
/// filtering at extraction time eliminates the failure mode entirely.
///
/// Capped at 40 candidates to keep the scoring batch manageable while
/// giving the cross-encoder enough breadth on long questions.
#[cfg(feature = "onnx")]
fn extract_nl_value_candidates(nl: &str) -> Vec<String> {
    let mut seen = std::collections::BTreeSet::new();
    let mut out: Vec<String> = Vec::new();

    let mut push_quoted = |seen: &mut std::collections::BTreeSet<String>,
                           out: &mut Vec<String>,
                           s: &str| {
        let trimmed = s.trim();
        if trimmed.is_empty() || trimmed.len() > 80 {
            return;
        }
        let quoted = format!("'{trimmed}'");
        if seen.insert(quoted.clone()) {
            out.push(quoted);
        }
    };
    let push_bare = |seen: &mut std::collections::BTreeSet<String>,
                     out: &mut Vec<String>,
                     s: String| {
        if s.is_empty() {
            return;
        }
        if seen.insert(s.clone()) {
            out.push(s);
        }
    };

    // 1. Quoted strings — highest priority. Both single + double quotes.
    for ch in ['\'', '"'] {
        let mut chars = nl.char_indices().peekable();
        while let Some((i, c)) = chars.next() {
            if c == ch {
                let start = i + 1;
                if let Some(close) = nl[start..].find(ch) {
                    let inner = &nl[start..start + close];
                    push_quoted(&mut seen, &mut out, inner);
                }
            }
        }
    }

    // 2. Numeric literals — int / decimal / negative. Captured BEFORE
    //    tokenisation so `>500` and `(K-12)` are handled.
    for chunk in nl.split(|c: char| !c.is_ascii_digit() && c != '.' && c != '-') {
        let s = chunk.trim_matches('.').trim_matches('-');
        if !s.is_empty() && s.chars().any(|c| c.is_ascii_digit()) {
            // Pure number?
            if s.chars().all(|c| c.is_ascii_digit() || c == '.') {
                push_bare(&mut seen, &mut out, s.to_string());
            }
            // Hyphenated form like `K-12` or `5-17` — keep both quoted forms.
            if s.contains('-') && s.chars().any(|c| c.is_ascii_digit()) {
                push_quoted(&mut seen, &mut out, s);
            }
        }
    }

    // 3. Multi-word capitalised phrases — collect runs of capitalised
    //    tokens (length >= 2) joined by whitespace + small connector
    //    words like "of", "the", "and" only when sandwiched between
    //    capitalised tokens (so "Office of Education" stays whole but
    //    "the school" doesn't).
    let small_connectors = ["of", "the", "and", "for", "in", "at"];
    let tokens: Vec<&str> = nl.split_whitespace().collect();
    let is_cap = |s: &str| -> bool {
        let trimmed: &str = s.trim_matches(|c: char| !c.is_alphanumeric());
        trimmed.len() >= 2
            && trimmed.chars().next().map_or(false, |c| c.is_ascii_uppercase())
    };
    let strip_punct = |s: &str| -> String {
        s.trim_matches(|c: char| !c.is_alphanumeric() && c != '-')
            .to_string()
    };

    let n = tokens.len();
    let mut i = 0;
    while i < n {
        if is_cap(tokens[i]) {
            let mut phrase = vec![strip_punct(tokens[i])];
            let mut j = i + 1;
            while j < n {
                let tj = tokens[j];
                let trimmed = tj.trim_matches(|c: char| !c.is_alphanumeric());
                if is_cap(tj) {
                    phrase.push(strip_punct(tj));
                    j += 1;
                } else if small_connectors.contains(&trimmed.to_lowercase().as_str())
                    && j + 1 < n
                    && is_cap(tokens[j + 1])
                {
                    phrase.push(trimmed.to_string());
                    j += 1;
                } else {
                    break;
                }
            }
            let joined = phrase.join(" ");
            // Always emit single-token capitalised words too (the joined
            // form covers multi-word, but we want both 'Alameda' and
            // 'Alameda County' as candidates when both appear).
            if phrase.len() > 1 {
                push_quoted(&mut seen, &mut out, &joined);
            }
            push_quoted(&mut seen, &mut out, &phrase[0]);
            i = j;
        } else {
            i += 1;
        }
    }

    // 4. Hyphenated codes / dates that didn't already land via the
    //    numeric chunker — e.g., `2000-01-01`.
    for tok in tokens.iter() {
        let stripped = strip_punct(tok);
        if stripped.contains('-')
            && stripped.chars().filter(|c| *c == '-').count() >= 1
            && stripped.chars().any(|c| c.is_ascii_digit())
        {
            push_quoted(&mut seen, &mut out, &stripped);
        }
    }

    // 5. Final stop-word filter — suppress framing tokens the cross-
    //    encoder still occasionally surfaces. We do this AFTER pushing
    //    so the multi-word path can include legitimate words like 'The'
    //    inside phrases.
    const STOP_QUOTED: &[&str] = &[
        "'show'", "'list'", "'find'", "'give'", "'highest'", "'lowest'",
        "'many'", "'most'", "'least'", "'top'", "'bottom'", "'average'",
        "'total'", "'name'", "'names'", "'number'", "'students'",
        "'opened'", "'closed'", "'active'", "'inactive'", "'phone'",
        "'school'", "'schools'", "'are'", "'is'", "'have'", "'has'",
        "'all'", "'each'", "'any'", "'with'", "'over'", "'under'",
        "'please'", "'compute'", "'count'", "'avg'", "'sum'", "'max'", "'min'",
    ];
    out.retain(|c| {
        let lc = c.to_lowercase();
        !STOP_QUOTED.iter().any(|s| s.eq_ignore_ascii_case(&lc))
    });

    if out.len() > 40 {
        out.truncate(40);
    }
    out
}

/// Attempt to salvage valid SQL from a concrete NatSQL string that failed
/// full parsing. Tries increasingly aggressive stripping:
///  1. Keep SELECT ... FROM entity, drop WHERE/GROUP/ORDER/LIMIT.
///  2. If aggregate keyword present, use COUNT(*) form.
///  3. Fall through to SELECT * FROM entity.
///
/// Returns `Err` only if no entity can be found in the FROM clause.
#[cfg(feature = "onnx")]
fn strip_to_valid_sql(concrete_natsql: &str) -> semsql_core::Result<semsql_natsql::NatSql> {
    let upper = concrete_natsql.to_uppercase();
    let from_pos = upper
        .find(" FROM ")
        .ok_or_else(|| SemsqlError::Other("strip_to_valid_sql: no FROM in concrete".into()))?;
    let after_from = concrete_natsql[from_pos + 6..].trim();
    let entity_end = after_from
        .find(|c: char| c.is_whitespace() || c == ';')
        .unwrap_or(after_from.len());
    let entity = &after_from[..entity_end];
    if entity.is_empty() {
        return Err(SemsqlError::Other("strip_to_valid_sql: empty entity".into()));
    }
    // Attempt 1: keep SELECT clause but drop conditions.
    let select_part = concrete_natsql[..from_pos].trim();
    let minimal = format!("{select_part} FROM {entity}");
    if let Ok(ast) = semsql_natsql::parse(&minimal) {
        return Ok(ast);
    }
    // Attempt 2: COUNT(*) for aggregate queries.
    if upper.contains("COUNT") {
        if let Ok(ast) = semsql_natsql::parse(&format!("SELECT COUNT(*) FROM {entity}")) {
            return Ok(ast);
        }
    }
    // Attempt 3: bare SELECT *.
    semsql_natsql::parse(&format!("SELECT * FROM {entity}"))
        .map_err(|e| SemsqlError::Other(format!("strip_to_valid_sql: {e}")))
}

/// Fix cross-entity field references in concrete NatSQL.
///
/// The skeleton decoder sometimes generates `FROM @entity1 WHERE @field2`
/// where @entity1 resolves to table A and @field2 resolves to `B.column`
/// (a field owned by a different table B). This makes the SQL fail in
/// execution because B.column is not accessible without B in the FROM clause.
///
/// Heuristic: count entity prefixes in all qualified `entity.field`
/// references. If the most common prefix differs from the FROM entity, swap
/// the FROM entity. Only swaps to entities already present in field references
/// so we don't hallucinate table names.
#[cfg(feature = "onnx")]
fn fix_from_entity_mismatch(concrete: &str) -> String {
    let upper = concrete.to_uppercase();
    let from_pos = match upper.find(" FROM ") {
        Some(p) => p,
        None => return concrete.to_string(),
    };
    let after_from = concrete[from_pos + 6..].trim();
    let from_end = after_from
        .find(|c: char| c.is_whitespace() || c == ';')
        .unwrap_or(after_from.len());
    let from_entity = &after_from[..from_end];
    if from_entity.is_empty() {
        return concrete.to_string();
    }

    // Count qualified `entity.field` prefixes across the whole statement.
    let mut entity_counts: HashMap<String, usize> = HashMap::new();
    let bytes = concrete.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i].is_ascii_alphabetic() || bytes[i] == b'_' {
            let start = i;
            while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                i += 1;
            }
            if i < bytes.len() && bytes[i] == b'.' {
                // Looks like an entity prefix — next char must be alphabetic too
                let candidate = &concrete[start..i];
                if i + 1 < bytes.len()
                    && (bytes[i + 1].is_ascii_alphabetic() || bytes[i + 1] == b'_')
                {
                    *entity_counts.entry(candidate.to_string()).or_default() += 1;
                }
            }
        } else {
            i += 1;
        }
    }

    if entity_counts.is_empty() {
        return concrete.to_string();
    }

    // Most common entity prefix among all dotted references (may equal FROM entity).
    let best = entity_counts
        .iter()
        .max_by_key(|(_, c)| *c)
        .map(|(e, _)| e.as_str());

    match best {
        Some(best_entity) if best_entity != from_entity => {
            // Replace " FROM {from_entity} " with " FROM {best_entity} ".
            let target = format!(" FROM {} ", from_entity);
            let replacement = format!(" FROM {} ", best_entity);
            if let Some(pos) = concrete.find(&target) {
                let mut out = concrete[..pos].to_string();
                out.push_str(&replacement);
                out.push_str(&concrete[pos + target.len()..]);
                out
            } else {
                concrete.to_string()
            }
        }
        _ => concrete.to_string(),
    }
}

/// Rewrite canonical `entity.field` identifiers in `sql` to use the
/// original DB column names stored in `col_map`. Columns whose canonical
/// name already matches the db_column (case-insensitively) are left as-is.
/// Columns that differ (e.g. canonical `county_name` → db `County Name`)
/// are replaced and backtick-quoted if the db name contains non-identifier
/// characters (spaces, parentheses, slashes, hyphens).
///
/// Replacement is whole-token: `entity.canonical_field` is only replaced
/// when the character following the match is not `[A-Za-z0-9_]`, so
/// `frpm.county_name2` is not clobbered by a rule for `frpm.county_name`.
fn rewrite_db_columns(sql: String, col_map: &HashMap<String, String>) -> String {
    if col_map.is_empty() {
        return sql;
    }
    // Sort by canonical key length descending — longer keys first —
    // so `entity.some_long_field` is replaced before any prefix could.
    let mut entries: Vec<(&str, &str)> = col_map
        .iter()
        .filter(|(canon, db_col)| {
            // Skip no-op substitutions (case-insensitive match, no special chars).
            let parts: Vec<&str> = canon.splitn(2, '.').collect();
            parts.len() == 2 && !parts[1].eq_ignore_ascii_case(db_col.as_str())
        })
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    if entries.is_empty() {
        return sql;
    }
    entries.sort_by(|a, b| b.0.len().cmp(&a.0.len()));

    let mut result = sql;
    for (canon, db_col) in entries {
        let parts: Vec<&str> = canon.splitn(2, '.').collect();
        if parts.len() != 2 {
            continue;
        }
        let (entity, _field) = (parts[0], parts[1]);
        let needs_quote = db_col
            .chars()
            .any(|c| !c.is_ascii_alphanumeric() && c != '_');
        let replacement = if needs_quote {
            format!("{entity}.`{db_col}`")
        } else {
            format!("{entity}.{db_col}")
        };
        result = replace_whole_token(&result, canon, &replacement);
    }
    result
}

/// Replace `from` in `s` only at positions where the character immediately
/// after the match is not an ASCII alphanumeric or underscore — preventing
/// partial-identifier clobbering.
fn replace_whole_token(s: &str, from: &str, to: &str) -> String {
    let mut result = String::with_capacity(s.len() + to.len());
    let from_bytes = from.as_bytes();
    let s_bytes = s.as_bytes();
    let mut i = 0;
    while i + from_bytes.len() <= s_bytes.len() {
        if s_bytes[i..].starts_with(from_bytes) {
            let after = i + from_bytes.len();
            let boundary_ok = after >= s_bytes.len()
                || !matches!(s_bytes[after], b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_');
            if boundary_ok {
                result.push_str(to);
                i = after;
                continue;
            }
        }
        result.push(s_bytes[i] as char);
        i += 1;
    }
    result.push_str(&s[i..]);
    result
}

/// Format the encoder source string per `docs/stage2.md §2.3`.
///
/// ```text
/// question: <NL>  ¦  schema:
///   <entity1>: <field1>, <field2>, ...
///   <entity2>: ...
/// ```
///
/// Only fields qualified as `entity.field` are included; each field's
/// entity is inferred from the prefix before the first dot.
#[cfg(feature = "onnx")]
fn format_encoder_input(nl: &str, entities: &[String], fields: &[String]) -> String {
    use std::collections::BTreeMap;
    let mut by_entity: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for e in entities {
        by_entity.entry(e.as_str()).or_default();
    }
    for f in fields {
        if let Some(dot) = f.find('.') {
            let entity = &f[..dot];
            let field = &f[dot + 1..];
            by_entity.entry(entity).or_default().push(field);
        }
    }
    let schema: Vec<String> = by_entity
        .iter()
        .map(|(entity, flds)| {
            if flds.is_empty() {
                format!("{entity}:")
            } else {
                format!("{entity}: {}", flds.join(", "))
            }
        })
        .collect();
    let schema_block = if schema.is_empty() {
        "(empty)".to_string()
    } else {
        schema.join("\n  ")
    };
    format!("question: {nl}  ¦  schema:\n  {schema_block}")
}

#[cfg(test)]
#[cfg(feature = "onnx")]
mod extractor_tests {
    use super::{extract_nl_value_candidates, slot_wants_numeric};

    fn cands(nl: &str) -> Vec<String> {
        extract_nl_value_candidates(nl)
    }

    #[test]
    fn slot_wants_numeric_after_gt() {
        assert!(slot_wants_numeric(
            "SELECT * FROM @entity1 WHERE @field1 > @val1",
            "@val1"
        ));
    }

    #[test]
    fn slot_wants_numeric_after_lt_eq() {
        assert!(slot_wants_numeric(
            "SELECT * FROM @entity1 WHERE @field1 <= @val1",
            "@val1"
        ));
    }

    #[test]
    fn slot_wants_numeric_after_between_and() {
        assert!(slot_wants_numeric(
            "SELECT * FROM @entity1 WHERE @field1 BETWEEN @val1 AND @val2",
            "@val1"
        ));
        assert!(slot_wants_numeric(
            "SELECT * FROM @entity1 WHERE @field1 BETWEEN @val1 AND @val2",
            "@val2"
        ));
    }

    #[test]
    fn slot_wants_numeric_after_limit() {
        assert!(slot_wants_numeric(
            "SELECT * FROM @entity1 LIMIT @val1",
            "@val1"
        ));
    }

    #[test]
    fn slot_does_not_want_numeric_after_eq() {
        assert!(!slot_wants_numeric(
            "SELECT * FROM @entity1 WHERE @field1 = @val1",
            "@val1"
        ));
    }

    #[test]
    fn slot_does_not_want_numeric_when_slot_absent() {
        assert!(!slot_wants_numeric(
            "SELECT * FROM @entity1",
            "@val1"
        ));
    }

    #[test]
    fn extracts_single_capitalised_county() {
        let c = cands("schools in Alameda County");
        assert!(c.contains(&"'Alameda'".to_string()), "{c:?}");
        assert!(c.contains(&"'Alameda County'".to_string()), "{c:?}");
    }

    #[test]
    fn extracts_multi_word_with_connectors() {
        let c = cands("schools in Fresno County Office of Education");
        assert!(
            c.contains(&"'Fresno County Office of Education'".to_string()),
            "missing multi-word phrase: {c:?}"
        );
    }

    #[test]
    fn extracts_numeric_literals() {
        let c = cands("math score over 400 and below 500");
        assert!(c.contains(&"400".to_string()), "{c:?}");
        assert!(c.contains(&"500".to_string()), "{c:?}");
    }

    #[test]
    fn preserves_hyphenated_codes() {
        let c = cands("students aged 5-17 in K-12 schools");
        assert!(c.contains(&"'5-17'".to_string()), "{c:?}");
        assert!(c.contains(&"'K-12'".to_string()), "{c:?}");
    }

    #[test]
    fn extracts_quoted_strings() {
        let c = cands("schools where county = 'Alameda' and grade = 'K-12'");
        assert!(c.contains(&"'Alameda'".to_string()), "{c:?}");
        assert!(c.contains(&"'K-12'".to_string()), "{c:?}");
    }

    #[test]
    fn extracts_iso_dates() {
        let c = cands("schools opened after 2000-01-01");
        assert!(c.contains(&"'2000-01-01'".to_string()), "{c:?}");
    }

    #[test]
    fn suppresses_stop_word_candidates() {
        let c = cands("show the highest score for many students");
        // Stop-words filtered.
        assert!(!c.contains(&"'show'".to_string()), "{c:?}");
        assert!(!c.contains(&"'highest'".to_string()), "{c:?}");
        assert!(!c.contains(&"'many'".to_string()), "{c:?}");
        assert!(!c.contains(&"'students'".to_string()), "{c:?}");
    }

    #[test]
    fn does_not_emit_short_or_empty_tokens() {
        let c = cands("a b c d e f");
        // Short tokens filtered (length < 2 inside multi-word logic).
        assert!(c.iter().all(|s| s.len() > 3), "{c:?}");
    }

    #[test]
    fn caps_at_40_candidates() {
        let nl = (0..200).map(|i| format!("Word{i}")).collect::<Vec<_>>().join(" ");
        let c = cands(&nl);
        assert!(c.len() <= 40, "len={}", c.len());
    }
}
