//! Stage 2 latency benchmarks (M7 from the Stage 2 training contract §5.4).
//!
//! Measures the cost of the deterministic pieces of Stage 2 — grammar
//! generation and grammar compilation through llguidance — at varying
//! schema-slice sizes. The model-forward pass itself is not benchmarked
//! here because that's gated on actual ONNX weights; per the docs, the
//! 15 ms latency budget breaks down as ~10 ms model forward × 8 tokens
//! plus ~3 ms grammar bind plus ~2 ms post-processing. This bench gates
//! the ~3 ms grammar-bind line against regressions.
//!
//! CI gate (M7 acceptance): regression > 10 % on the p50 of any reported
//! benchmark fails the build. Run locally with:
//!
//!     cargo bench -p semsql-runtime --features onnx
//!
//! The bench is `--features onnx`-gated because the `compile_grammar`
//! function (which calls llguidance) is itself behind the `onnx`
//! feature — `cargo bench` without the feature still compiles but only
//! exercises the pure grammar-generation path, which is useful as a
//! cross-platform sanity benchmark on CI runners that don't have the
//! ONNX runtime installed.

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion};
use semsql_runtime::grammar::{build_natsql_grammar, GrammarSchema};

/// Build a representative schema slice with `n_entities` and roughly
/// `fields_per_entity` qualified fields per entity. Mirrors what Stage 1
/// emits as its top-k after intent biasing.
fn make_schema(n_entities: usize, fields_per_entity: usize) -> GrammarSchema {
    let entities: Vec<String> = (0..n_entities).map(|i| format!("entity_{i}")).collect();
    let fields: Vec<String> = entities
        .iter()
        .flat_map(|e| (0..fields_per_entity).map(move |j| format!("{e}.field_{j}")))
        .collect();
    GrammarSchema {
        entities,
        fields,
        value_slots: vec!["@val1".into(), "@val2".into(), "@val3".into()],
    }
}

/// Pure grammar-string generation — no llguidance, no ort. Deterministic
/// and fully cross-platform; sets the floor on Stage 2 grammar overhead.
fn bench_grammar_generation(c: &mut Criterion) {
    let mut group = c.benchmark_group("stage2_grammar_generation");
    for &(entities, fields_per) in &[(1usize, 5), (5, 10), (10, 25), (25, 50), (100, 50)] {
        let schema = make_schema(entities, fields_per);
        let total_fields = schema.fields.len();
        group.bench_with_input(
            BenchmarkId::new(
                "build_natsql_grammar",
                format!("e{entities}_f{total_fields}"),
            ),
            &schema,
            |b, schema| {
                b.iter(|| {
                    let g = build_natsql_grammar(black_box(schema));
                    black_box(g);
                });
            },
        );
    }
    group.finish();
}

/// llguidance grammar compilation — the dominant per-query cost when
/// the cascade has weights loaded. The reference budget from the docs is
/// p50 ≤ 1.8 ms / p95 ≤ 3.2 ms for a 5-entity × 50-fields slice.
#[cfg(feature = "onnx")]
fn bench_grammar_compile(c: &mut Criterion) {
    use semsql_runtime::stage_skeleton::compile_grammar;

    let mut group = c.benchmark_group("stage2_grammar_compile");
    for &(entities, fields_per) in &[(1usize, 5), (5, 10), (5, 50), (10, 25), (25, 50)] {
        let schema = make_schema(entities, fields_per);
        let total_fields = schema.fields.len();
        group.bench_with_input(
            BenchmarkId::new("compile_grammar", format!("e{entities}_f{total_fields}")),
            &schema,
            |b, schema| {
                b.iter(|| {
                    let compiled = compile_grammar(black_box(schema))
                        .expect("compile must succeed for benchmark schemas");
                    black_box(compiled);
                });
            },
        );
    }
    group.finish();
}

#[cfg(not(feature = "onnx"))]
fn bench_grammar_compile(_c: &mut Criterion) {
    // No-op when onnx feature is disabled — `compile_grammar` returns an
    // error in that build configuration, so there's nothing meaningful
    // to time. The pure-grammar bench above still runs.
}

/// Per-step llguidance mask compute benchmark. The target budget is
/// <= 100 us per `compute_mask()` call for a representative
/// schema slice. Builds a `TokenParser` from the bridge once, then times
/// repeated `compute_mask` calls on the prompt-empty parser (worst case
/// for first-token mask construction).
///
/// Skipped if no `tokenizer.json` fixture exists under `target/` - the
/// bench requires a real vocab to produce meaningful numbers.
#[cfg(feature = "onnx")]
fn bench_per_step_mask(c: &mut Criterion) {
    use llguidance::api::TopLevelGrammar;
    use llguidance::ParserFactory;
    use semsql_runtime::stage_skeleton::compile_grammar;
    use semsql_runtime::tokenizer_bridge::OnnxTokEnv;
    use std::path::Path;
    use tokenizers::Tokenizer;

    let fixtures = [
        "target/cascade-v2/skeleton/tokenizer.json",
        "target/cascade-v2/_skeleton_export/tokenizer.json",
        "target/cascade-v1/_skeleton_export/tokenizer.json",
    ];
    let tokenizer_path = fixtures.iter().find(|p| Path::new(p).exists());
    let Some(tokenizer_path) = tokenizer_path else {
        eprintln!("[per-step bench] no tokenizer fixture found — skipping");
        return;
    };

    let tokenizer = match Tokenizer::from_file(tokenizer_path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("[per-step bench] tokenizer load failed: {e}");
            return;
        }
    };
    let env = match OnnxTokEnv::new(tokenizer) {
        Ok(e) => e.into_tok_env(),
        Err(e) => {
            eprintln!("[per-step bench] OnnxTokEnv build failed: {e}");
            return;
        }
    };
    let mut factory = ParserFactory::new_simple(&env).expect("ParserFactory::new_simple");
    factory.quiet();

    let mut group = c.benchmark_group("stage2_per_step_mask");
    // Acceptance schema slice — a typical Stage 1 top-k output.
    let schema = make_schema(5, 10);
    let compiled = compile_grammar(&schema).expect("compile_grammar");

    group.bench_function("compute_mask_first_token", |b| {
        b.iter_batched(
            || {
                let g = TopLevelGrammar::from_lark(compiled.lark.clone());
                let mut p = factory.create_parser(g).expect("create_parser");
                p.start_without_prompt();
                p
            },
            |mut parser| {
                let m = parser.compute_mask().expect("compute_mask");
                black_box(m);
            },
            criterion::BatchSize::SmallInput,
        );
    });
    group.finish();
}

#[cfg(not(feature = "onnx"))]
fn bench_per_step_mask(_c: &mut Criterion) {}

criterion_group!(
    benches,
    bench_grammar_generation,
    bench_grammar_compile,
    bench_per_step_mask
);
criterion_main!(benches);
