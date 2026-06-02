# SemanticSQL

> Open-source NL→SQL with a tiny multi-stage cascade. The SemanticGraph is the product. Vocabulary is canonical. Security is non-negotiable.

[![License](https://img.shields.io/badge/license-Apache--2.0%20OR%20MIT-blue.svg)](#license)
[![Status](https://img.shields.io/badge/status-pre--alpha%20(v0.1)-orange.svg)](#roadmap)

SemanticSQL converts natural-language questions into safe, scoped SQL by:

1. Auto-extracting an auditable **SemanticGraph** from your codebase (frontend labels, i18n, API resources, ORM, DB) — frontend vocabulary is canonical.
2. Running queries through a **~35 M-parameter cascade** of specialised tiny models, not a single large LLM.
3. Validating + injecting mandatory tenant/RLS scoping through **sqlglot**, with an independent Rust second-pass parser.
4. Rendering for any of 7 SQL dialects via sqlglot's emitter.

The full design lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Why a cascade

| Property                         | Single LLM (e.g. Qwen-0.5B) | SemanticSQL cascade   |
| -------------------------------- | --------------------------- | --------------------- |
| Total parameters                 | ~500 M                      | ~35 M                 |
| Ship size (int8 ONNX)            | ~400 MB                     | ~40 MB                |
| Median CPU latency               | 80–150 ms                   | 20–25 ms              |
| Browser-deployable               | yes (heavy)                 | yes (under 50 MB)     |
| Hallucinated tables / columns    | possible                    | structurally rejected |
| Per-stage debuggability          | one black box               | five measurable steps |
| Idiomatic NL ("bleeding money")  | hit-or-miss                 | deterministic         |

Cascade research: [RESDSQL (AAAI 2023)](https://arxiv.org/pdf/2302.05965), [NatSQL (Findings EMNLP 2021)](https://aclanthology.org/2021.findings-emnlp.174/).

## Stack

| Layer                              | Language    | Crate / package                                   |
| ---------------------------------- | ----------- | ------------------------------------------------- |
| SemanticGraph + intent + cascade   | Rust        | `semsql-graph`, `semsql-intent`, `semsql-runtime` |
| NatSQL grammar + transpiler        | Rust        | `semsql-natsql`                                   |
| SQL validator + filter injector    | Python      | `semsql_rewriter` (sqlglot)                       |
| Independent second-pass validator  | Rust        | `semsql-second-pass` (sqlparser-rs)               |
| SQL dialect renderer               | Rust        | `semsql-renderer`                                 |
| Per-stage trainers + ONNX export   | Python      | `semsql_train`                                    |
| Spider/BIRD + adversarial eval     | Python      | `semsql_eval`                                     |
| Framework extractors               | TypeScript  | `packages/extractor-*`                            |
| Intent pattern library             | YAML        | `intent-library/patterns.yaml`                    |
| Canonical contract                 | Protobuf    | `schemas/semantic_graph.proto`                    |

## Status

Pre-alpha. v0.1 (rewriter foundation, no model) is in active development. See the [roadmap](docs/ARCHITECTURE.md#roadmap) for milestones.

## Quick start (planned API for v0.2)

```bash
# Extract a SemanticGraph from a Laravel/Filament app
semsql extract --framework laravel ./my-app -o my-app.semsql

# Or DB-only fallback
semsql extract --framework none --db-url postgres://localhost/myapp -o my-app.semsql

# Query in natural language
semsql query --graph my-app.semsql "active students who joined last month"

# Inspect provenance + flag conflicts
semsql doctor --graph my-app.semsql
```

## Browser spreadsheet demo

A browser-only CSV and public Google Sheets demo lives in `packages/sheets-demo`. It imports `@semsql/sheets` and supports packaged use cases, public CSV/Google Sheets URLs, and local CSV uploads.

```bash
pnpm --filter @semsql/sheets-demo build
python -m http.server 4173 --bind 127.0.0.1
# http://127.0.0.1:4173/packages/sheets-demo/index.html
```

For GitHub Pages:

```bash
pnpm --filter @semsql/sheets-demo build:pages
pnpm --filter @semsql/sheets-demo smoke:pages
python -m http.server 4173 --bind 127.0.0.1 -d target/sheets-demo-pages
# http://127.0.0.1:4173/
```

The `Sheets demo Pages` workflow deploys the artifact to `https://xwiz.github.io/db-claw/`.

## Security

SemanticSQL is designed with multi-tenant isolation as a hard requirement. Every generated query is:

1. **Validated** by sqlglot (the same parser Apache Superset adopted to fix [CVE-2025-48912](https://www.miggo.io/vulnerability-database/cve/CVE-2025-48912)) against an allow-list of statement types.
2. **Scoped** by the mandatory-filter injector, recursively across CTEs / UNION / lateral joins.
3. **Re-validated** by an independent Rust parser (sqlparser-rs) — two parsers must agree.
4. **Required to run alongside Postgres RLS** (or vendor equivalent) in production. `semsql doctor` blocks deployment if RLS is missing on tenanted tables.

Every vocabulary fragment entering the SemanticGraph is sanitised at extraction time so untrusted lang-file or label content can never reach SQL.

## License

Dual-licensed under **Apache-2.0** and **MIT**, at your option.

## Contributing

See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md). Each layer has its own README — pick the language you know.
