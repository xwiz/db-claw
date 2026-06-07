# v0.2 BIRD100 ONNX Diagnostic v1
Date: 2026-06-07. Retained one-run benchmark evidence; live status remains in
[v02-current-status.md](v02-current-status.md).

## Scope

This rerun used the cleaned runtime and ONNX-enabled local CLI to answer the
first 100 BIRD dev examples. It is a stress diagnostic for arbitrary dynamic
schemas, not a production-readiness pass/fail for the private-alpha product
surface.

Artifacts:

- eval report: `target/v02/current-bird100-onnx-20260607b/report.json`
- diagnosis: `target/v02/current-bird100-onnx-20260607b/diagnosis.md`
- manifest: `target/v02/cascade-v3-runtime-covered500-adapt/manifest.json`
- CLI: `target/debug/semsql.exe`, `semsql 0.1.0-alpha.5`

## Result

- total: `100`
- correct: `3`
- wrong: `87`
- bails: `7`
- errors: `3`
- exec accuracy: `3.00%`
- DB mix: `california_schools=89`, `financial=11`
- stage breakdown: `stage_3=61`, `stage_0a=29`, `needs_model=7`,
  `stage2_structural_error=1`, `stage4_render_error=1`, `error=1`

## Product-Safety Read

The useful signal is not only low benchmark accuracy. The unsafe signal is that
the runtime still emits wrong SQL when evidence is incomplete:

- real final SQL emitted: `89`
- final SQL wrong: `86`
- deterministic route used for final SQL: `29`
- route-used wrong SQL: `26`
- model SQL after route rejection: `28`
- model SQL after route rejection wrong: `28`

Representative route-used wrong examples include grade-span, rank/projection,
range parsing, charter/enrollment, grouped aggregate, monthly average, and
district-ratio questions. These are generic semantic-binding failures, not a
reason to add BIRD-specific or California-schools shortcuts.

## Interpretation

BIRD100 remains useful as a stress benchmark, but the first-100 prefix is not a
balanced product proxy because it is dominated by one school dataset. The run
does prove that issue #1 cannot close yet: broad dynamic-schema benchmark
readiness is blocked by fail-closed promotion, metric/value binding, and typed
fallback routing.

Next benchmark work should:

- fail closed before route promotion unless field/value/metric evidence is
  complete;
- route rejected complex questions to typed LLM fallback or clarification before
  Stage 3 emits SQL;
- improve SemanticAtlas metric formulas, categorical/code-value binding,
  date/range normalization, and join-minimality;
- rerun BIRD100 after those generic fixes, then move to a stratified sample.
