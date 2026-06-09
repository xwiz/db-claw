# Architecture

SemanticSQL turns a natural-language question into scoped SQL in small,
reviewable steps. The important design choice is simple: models can help rank or
resolve ambiguity, but deterministic code owns the final SQL shape, scoping, and
validation.

## Runtime Flow

```text
app + database sources
  -> extract vocabulary/schema into a .semsql graph

question
  -> vocabulary and intent matching
  -> SemanticAtlas profile and per-query strength report
  -> candidate fields, values, joins, filters, measures
  -> typed IntentFrame
  -> candidate plan enumeration
  -> BoundQueryPlan validation
  -> ResolutionDecision: execute | ask_user | ask_llm | reject
  -> deterministic SQL rendering only when the plan is grounded
  -> typed handoff packet for user or LLM-assisted cases
  -> sqlglot validation + tenant filter injection
  -> Rust second-pass validation
  -> dialect SQL
```

The runtime should prefer a narrow, inspectable answer over a clever guess. When
the graph cannot support a question, the correct behavior is to ask the user,
ask an LLM for a typed proposal over bounded evidence, or reject.

## Main Pieces

`crates/semsql-cli` owns commands and diagnostics; `crates/semsql-runtime`
owns atlas/planning/decisions; `crates/semsql-graph` stores `.semsql` data.
Extraction lives in `crates/semsql-extract-db` and `packages/extractor-*`;
rendering and validation live in `semsql-natsql`, `semsql-renderer`,
`semsql-second-pass`, and `python/semsql_rewriter`. Evaluation/training live
under `python/semsql_eval` and `python/semsql_train`.

## SemanticAtlas And QueryFrame

`SemanticAtlas` is the query-time truth layer: app context, DB facts, values,
relationships, metrics, table-family hints, and approved memory. `IntentFrame`
and `QueryFrame` carry selected fields, filters, measures, dimensions, joins,
result shape, rejection reasons, and slot-level uncertainty.

For contributors, product behavior belongs in reusable atlas evidence, typed
candidate generation, or the decision gate. It must not be a one-off string
rewrite for a benchmark or app example.

## Safety Path

Every generated query is expected to pass through:

1. allow-listed statement validation with sqlglot;
2. mandatory scope injection across nested query shapes;
3. second-pass parsing with `sqlparser-rs`;
4. deployment checks through `semsql doctor`.

A query that cannot be scoped is not a partial success. Validation, injection,
tenant scoping, and SQL changes require adversarial tests.

## LLM Handoff

The larger LLM path is for unresolved reasoning, not direct SQL execution. The
runtime can package a rejected question with compact schema context, atlas
strength, unresolved slots, and candidate reasoning. Any accepted proposal must
still come back through deterministic rendering, scoping, and validation.

## Contract And Memory

Authored contracts and local resolution memory are loaded beside the generated
graph. Authored aliases have higher precedence than generated evidence;
confirmed memory can resolve a previously bounded ambiguity, but stale entries
are ignored when their schema drift key no longer matches.

Only `confirmed` and `governed` memory participates in planning. Successful SQL
execution alone never creates memory. `semsql resolve` records an explicit user
approval with provenance, then the normal planner and decision gate rerun.

Current contract consumption covers aliases and governed metrics. Canonical join
paths, date roles, active table families, virtual fields, and reusable typed
plan templates remain release work.

## Evaluation

Use `docs/results/` for the current scorecard. Product signals are
QueryFrame/BoundQueryPlan gates, real-schema read-only probes, and semantic
alias pressure tests. BIRD is a DB-only diagnostic; sampled smokes are triage,
not release claims.

Raw JSON reports are generated artifacts and should stay out of Git. Keep
human-readable summaries in Markdown.
