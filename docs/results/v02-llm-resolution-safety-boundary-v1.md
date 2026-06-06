# v0.2 LLM Resolution Safety Boundary v1

Date: 2026-06-06. Retained boundary report; headline numbers live in
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Contract

Typed fallback is a bounded plan-proposal path. Direct provider SQL is rejected;
all selected plans pass local validation, rendering, and optional read-only
execution.

## Guardrails

- Sensitive entities/fields, ambiguous shard families, unsupported row dumps,
  and vague schema paths fail closed or ask for clarification.
- Row-list SQL is capped locally: default `LIMIT 100`, provider limits capped at
  `1000`.
- Provider proposals use a closed `result_shape` vocabulary:
  `scalar_metric`, `table`, `categorical_chart`, `time_series_chart`,
  `multi_series_chart`, or empty/unknown.
- Local rendering rejects shape mismatches, including multi-series proposals
  without both time and segment groups.
- Null predicates, lifecycle-existence checks, named month/quarter windows,
  date-role anchors, and tenant/account/org scope paths are normalized into
  typed evidence before promotion.
- Non-PII field-scoped sample values can surface as `sample_value_hits`, seed
  capped fields into packets, and validate only against their owning field.
  Compound samples such as code-like enum values can validate only when the
  matched component is unique; ambiguous component matches require clarification.
- Source vocabulary now surfaces field/entity aliases and `enum_value_hits`.
  Field/entity aliases can seed capped schema references; enum-value filters
  validate only when the matched source term is unambiguous.
- Durable graph metric definitions can surface as `metric_catalog_hits`, and
  unique packet-backed value formulas can surface as `metric_formula_hits` for
  conditional rates; ambiguous formulas fail closed instead of auto-routing.
  Metric-catalog definitions outrank generic packet formulas.
- Extractors and authored `semsql.metrics.json` files can populate durable
  metric definitions through the same JSONL ingest path as vocabulary.
- Batch fallback can execute validated SQL through the read-only executor,
  discard row values, use per-packet DB URL maps, and bucket execution failures
  by generic DB/runtime cause.
- Render-issue summaries are ASCII-normalized for Windows console safety.

## Verification

- LLM-resolution tests: `89/89`.
- SemSQL graph tests: `14/14`, including metric-definition read/write coverage.
- Extractor CLI authored-metric test: `6/6` package tests.
- CLI fallback/result-shape slice: `53/53`.
- Real-DB typed fallback/result-shape slice: `12/12`.
- `ruff check python`: pass.
- `mypy python`: pass, `24` source files.

## Interpretation

This is a generic safety boundary, not a query shortcut. The regression rule is
simple: provider help may improve rejected cases, but accepted SQL must still be
schema-backed, locally rendered, read-only, bounded, and row-value-discarding in
batch proofs.
