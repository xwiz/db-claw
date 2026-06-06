# v0.2 State-Machine Pivot Assessment

Date: 2026-06-05

Historical design memo. Current status and evidence live in
`v02-current-status.md` and `v02-evidence-ledger.md`.

## Decision

Keep the benchmark pipeline as an oracle/regression harness, but move product
query construction toward a deterministic, evidence-gated state machine:

```text
NL query
  -> typed mention extraction
  -> schema/value/metric retrieval
  -> IntentFrame
  -> BoundQueryPlan
  -> SQL renderer
  -> validator
```

Models may score choices inside legal transitions. They should not decide
whether a SQL state is legal.

## Preserved Lessons

- The grammar constrains placeholder syntax and slot cardinality, not full
  schema semantics.
- The useful fixes were deterministic: typed literals, slot roles,
  field-compatible values, join repairs, range parsing, and guarded frame
  transitions.
- Sampled smokes can saturate and mislead; broad BIRD becomes useful again only
  after cleaned-runtime coverage improves.
- Historical v10-v17 BIRD recovery is diagnostic background, not release proof.

## Required Transitions

- `Mention -> EntityCandidate`
- `Mention -> FieldCandidate`
- `Mention -> TypedLiteral`
- `(FieldCandidate, TypedLiteral) -> Predicate`
- `(EntityCandidate, Relationship) -> Join`
- `(Projection | Aggregate | Group | Order | Limit) -> SelectShape`
- `BoundQueryPlan -> SQL`

Each transition owns compatibility checks, source spans, diagnostics, and
fail-closed behavior.

## Implementation Focus

1. Rebuild worthwhile lost behavior as source-grounded transitions, not
   DB-family shortcuts.
2. Improve one residual capability slice at a time and require `0` wrong
   accepted SQL.
3. Capture route traces and rejection packets for every promoted probe.
4. Retrain only when legal candidates exist and are merely misranked.

## Stop Conditions

- Do not promote BIRD20/BIRD100 smokes as readiness evidence.
- Do not encode dataset contradictions as product behavior without a flag.
- Do not render partial plans with missing field, value, metric, date, join, or
  safety evidence.
