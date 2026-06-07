# SemanticSQL (DB-CLAW)

> Ask your app's database questions in English. Get scoped SQL, not a guess.

[![License](https://img.shields.io/badge/license-Apache--2.0%20OR%20MIT-blue.svg)](#license)
[![Status](https://img.shields.io/badge/status-v0.2%20working%20runtime-orange.svg)](#status)

SemanticSQL adds natural-language database questions to an existing app. It
reads app labels, translations, API resources, ORM models, and database schema
into a `.semsql` graph. SQL is emitted only when a question is
grounded in that graph, scoped, rendered locally, and validated.

If a question is vague, unsafe, or outside the graph, SemanticSQL rejects it or
writes a typed handoff packet for reviewed/LLM-assisted plan proposals. The
model does not get to execute its own SQL.

## Questions You Can Ask

```bash
semsql query --graph app.semsql "how many active accounts signed up in February 2024?"
semsql query --graph app.semsql "list active enterprise accounts in EMEA"
semsql query --graph app.semsql "top 2 products by order amount"
semsql query --graph app.semsql "find account ACME-001"
```

And when the question is too loose or unsafe:

```bash
semsql query --graph app.semsql "show open things"                  # narrow it down
semsql query --graph app.semsql "which customer is healthiest?"     # define the metric first
semsql query --graph app.semsql "email all accounts with invoices"  # not a SQL query
semsql query --graph app.semsql "list every ticket with all columns" # row dump
```

## Try It

```bash
semsql extract . --framework none --db-url postgres://localhost/myapp -o app.semsql
semsql extract . --framework laravel --db-url "$DATABASE_URL" -o app.semsql
semsql query --graph app.semsql "active students who joined last month"
semsql query --graph app.semsql --query-frame-json frame.json "active students"
semsql query --graph app.semsql \
  --rejection-packet-json rejected.packet.json \
  "which customer is healthiest?"
semsql doctor --graph app.semsql
```

Resolve a rejected packet from a reviewed typed proposal:

```bash
uv run --package semsql-eval python -m semsql_eval llm-resolution-resolve-packet \
  --packet-json rejected.packet.json \
  --proposal-json reviewed.proposal.json \
  --render-out render.json \
  --strict
```

Optional provider path. Supported providers: `openai`, `groq`, `deepseek`, and
`openai-compatible`. Missing config makes `0` provider calls and fails closed.

```bash
uv run --package semsql-eval python -m semsql_eval llm-resolution-fallback-query \
  --graph app.semsql \
  --question "which segment has the highest average revenue?" \
  --provider openai \
  --out target/fallback-smoke
```

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
   roles, sensitivity, and metric hints.
3. Turn a question into a typed `IntentFrame`.
4. Bind it into a `BoundQueryPlan`.
5. Render SQL only when the plan is grounded.
6. Validate and optionally execute read-only with bounded previews.

LLMs can help unresolved cases by proposing a typed plan over a bounded packet.
Direct LLM SQL is rejected.

## Status

v0.2 is a working runtime line, not a broad benchmark-complete release.

Current proof points:

- promoted governed routes have `0` wrong accepted SQL;
- MariaDB and Postgres probes pass read-only;
- framework/source aliases flow through native `extract` when `semsql-extract`
  is available;
- direct LLM SQL, static example routers, and ambiguous sharded routes fail
  closed;
- broad BIRD remains poor after shortcut removal and is a DB-only atlas signal,
  not a reason to add static examples.

Detailed status lives in [docs/results/v02-current-status.md](docs/results/v02-current-status.md).

## Docs

- [Contributor docs](docs/README.md)
- [Comparisons](docs/COMPARISONS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Contributing](docs/CONTRIBUTING.md)
- [Go-live packaging](docs/GO_LIVE.md)
- [Results index](docs/results/README.md)
License: dual **Apache-2.0** and **MIT**, at your option.
