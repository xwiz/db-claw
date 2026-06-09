# SemanticSQL (DB-CLAW)

> Ask your app's database questions in English. Get scoped SQL, not a guess.

[![License](https://img.shields.io/badge/license-Apache--2.0%20OR%20MIT-blue.svg)](#license)
[![Status](https://img.shields.io/badge/status-v0.2%20working%20runtime-orange.svg)](#status)

SemanticSQL adds natural-language database questions to an existing app. It
reads app labels, translations, API resources, ORM models, and database schema
into a `.semsql` graph. SQL is emitted only when a question is grounded in that
graph, scoped, rendered locally, and validated. Otherwise the runtime should
return a typed decision: ask the user, ask an LLM for a typed proposal, or reject.

If a question is vague, unsafe, or outside the graph, SemanticSQL rejects it or
writes a typed handoff packet for reviewed/LLM-assisted plan proposals. The
model does not get to execute its own SQL.

```bash
semsql query --graph app.semsql "how many active accounts signed up in February 2024?"
semsql query --graph app.semsql "list active enterprise accounts in EMEA"
semsql query --graph app.semsql "top 2 products by order amount"
semsql query --graph app.semsql "find account ACME-001"
```

## Try It

```bash
semsql extract . --framework none --db-url postgres://localhost/myapp -o app.semsql
semsql extract . --framework laravel --db-url "$DATABASE_URL" -o app.semsql
semsql query --graph app.semsql "active students who joined last month"
semsql query --graph app.semsql --format json "active students"
semsql query --graph app.semsql \
  --rejection-packet-json rejected.packet.json \
  "which customer is healthiest?"
semsql resolve rejected.packet.json
semsql resolve --mode cli rejected.packet.json
semsql resolve --mode json rejected.packet.json
semsql doctor --graph app.semsql
```

Authored semantics and approved corrections are optional sidecars:

```bash
semsql query --graph app.semsql \
  --semantic-contract semsql.contract.yaml \
  --resolution-memory semsql.memory.yaml \
  --format json \
  "show enterprise customers"
```

Resolve a rejected packet from a reviewed typed proposal:

```bash
uv run --package semsql-eval python -m semsql_eval llm-resolution-resolve-packet \
  --packet-json rejected.packet.json \
  --proposal-json reviewed.proposal.json \
  --render-out render.json \
  --strict
```

Optional typed-provider paths support `openai`, `groq`, `deepseek`, and
`openai-compatible`; missing config makes `0` calls and fails closed.

## Laravel Alpha Shape

```bash
pnpm --package @semsql/cli --package @semsql/extractor-cli dlx \
  semsql extract . \
  --framework laravel \
  --db-url "$DATABASE_URL" \
  --no-sample-values \
  -o storage/semsql/app.semsql
pnpm --package @semsql/cli dlx semsql doctor --graph storage/semsql/app.semsql
pnpm --package @semsql/cli dlx semsql query --graph storage/semsql/app.semsql "count active users"
```

## How It Works

1. Extract app and database vocabulary into a `.semsql` graph.
2. Build a `SemanticAtlas`: entities, fields, aliases, relationships, values,
   roles, sensitivity, metrics, table-family hints, and approved memory.
3. Turn a question into a typed `IntentFrame`.
4. Enumerate and bind candidate plans into a `BoundQueryPlan`.
5. Run a decision gate: `execute`, `ask_user`, `ask_llm`, or `reject`.
6. Render SQL only when the plan is grounded.
7. Validate and optionally execute read-only with bounded previews.

LLMs can help unresolved cases by proposing a typed plan over a bounded packet.
Direct LLM SQL is rejected.

## Status

v0.2 is a working private-alpha runtime line, not a broad benchmark-complete or
general production release. The resolution spine now emits slot-level
`execute`, `ask_user`, `ask_llm`, and `reject` decisions; approved corrections
are drift-keyed and reused on later queries.

Current proof points:

- promoted governed routes have `0` wrong accepted SQL;
- MariaDB and Postgres probes pass read-only;
- framework/source aliases flow through native `extract` when `semsql-extract`
  is available;
- direct LLM SQL, static example routers, and ambiguous sharded routes fail
  closed;
- a duplicate-value ambiguity can be approved through `semsql resolve` and the
  rerun binds the approved field without a query-specific shortcut;
- broad BIRD remains poor after shortcut removal and is a DB-only atlas signal,
  not a reason to add static examples.

Remaining alpha work is broader semantic-contract coverage, a full Laravel
extract/query/resolve/rerun proof, release rehearsal, and visual QA of the local
resolver.

Detailed status lives in [docs/results/v02-current-status.md](docs/results/v02-current-status.md).

[Contributor docs](docs/README.md) | [Architecture](docs/ARCHITECTURE.md) |
[Comparisons](docs/COMPARISONS.md) | [Go-live](docs/GO_LIVE.md) |
[Results](docs/results/README.md)
License: dual **Apache-2.0** and **MIT**, at your option.
