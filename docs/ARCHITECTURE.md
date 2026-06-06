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
  -> candidate fields, values, joins, filters, measures
  -> typed QueryFrame
  -> deterministic SQL rendering when the frame is grounded
  -> optional rejected-query packet for harder LLM-assisted cases
  -> sqlglot validation + tenant filter injection
  -> Rust second-pass validation
  -> dialect SQL
```

The runtime should prefer a narrow, inspectable answer over a clever guess. When
the graph cannot support a question, the correct behavior is to reject, clarify,
or prepare a constrained handoff packet.

## Main Pieces

| Area | Path | Notes |
| --- | --- | --- |
| CLI | `crates/semsql-cli` | `extract`, `query`, `doctor`, diagnostics |
| Runtime | `crates/semsql-runtime` | intent, cascade, QueryFrame routing |
| Graph | `crates/semsql-graph` | `.semsql` graph read/write |
| DB extraction | `crates/semsql-extract-db` | schema extraction for supported databases |
| NatSQL | `crates/semsql-natsql` | placeholder grammar and SQL transpilation |
| SQL rendering | `crates/semsql-renderer` | dialect rendering |
| Second pass | `crates/semsql-second-pass` | independent Rust SQL validation |
| SQL rewrite | `python/semsql_rewriter` | sqlglot validation and tenant filter injection |
| Eval harness | `python/semsql_eval` | BIRD/Spider, canaries, real-schema probes |
| Training/export | `python/semsql_train` | stage training and ONNX export |
| Framework extractors | `packages/extractor-*` | Laravel, Django, Rails, Next.js, Vue, i18n |

## QueryFrame

`QueryFrame` is the bridge between "what the user asked" and "SQL we are willing
to run." It carries:

- selected entities and fields;
- filters and literal values;
- measures, dimensions, grouping, sorting, and limits;
- join paths;
- candidate rejection reasons;
- renderability and safety status;
- result-shape hints for metric, table, and chart UI plumbing.

For contributors, this is usually the right place to add product behavior. A new
question shape should become a reusable frame transition, not a one-off string
rewrite for a benchmark example.

## Safety Path

Every generated query is expected to pass through:

1. allow-listed statement validation with sqlglot;
2. mandatory scope injection across nested query shapes;
3. second-pass parsing with `sqlparser-rs`;
4. deployment checks through `semsql doctor`.

Changes that touch validation, injection, sanitization, tenant scoping, or
generated SQL should include adversarial tests. A query that cannot be scoped is
not a partial success.

## LLM Handoff

The larger LLM path is for unresolved reasoning, not direct SQL execution. The
runtime can package a rejected question with compact schema context and candidate
reasoning. Any accepted proposal must still come back through deterministic
rendering, scoping, and validation.

## Evaluation

Use the docs in `docs/results/` for the current scorecard and accepted gates.
The short version:

- practical QueryFrame/BoundQueryPlan gates are the product-routing signal;
- real-schema probes check extraction, read-only execution, and fail-closed
  behavior;
- semantic-alias pressure tests show whether the atlas is rich enough;
- BIRD is a research/benchmark diagnostic until the cleaned runtime is broader;
- sampled smokes are useful for triage, not release claims.

Raw JSON reports are generated artifacts and should stay out of Git. Keep
human-readable summaries in Markdown.
