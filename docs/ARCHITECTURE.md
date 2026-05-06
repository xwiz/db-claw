# SemanticSQL — Architecture

This document is the canonical record of the system's design. The full plan, including risk analysis and roadmap, was approved on 2026-05-05 and lives at `~/.claude/plans/i-want-to-build-wiggly-puzzle.md`.

This doc is the *operating* architecture — what's true now, and where each piece of the codebase fits.

## Five guiding principles

1. **The cascade is the engine, not the LLM.** The runtime is a four-stage pipeline of specialised tiny models (~35 M parameters total), not a single generative LLM. A large LLM is optional, used at most once during graph construction or as an escalation path for queries the cascade can't handle.
2. **The SemanticGraph is the product.** Everything else serves it. Query accuracy is bounded by graph quality.
3. **Frontend vocabulary is canonical.** UI labels (i18n, Filament forms, table headers) outrank ORM and DB names. What the user reads on screen is what the user types.
4. **Security is non-negotiable and defense-in-depth.** Two independent SQL parsers must agree on every output. Postgres RLS (or vendor equivalent) is mandatory in production.
5. **Auditability everywhere.** Every vocabulary fragment carries a `(file, line)` locator. `semsql doctor` surfaces conflicts and deployment-readiness warnings.

## The cascade

```text
NL query
  → Stage 0a — Vocabulary Pre-resolver           (<1 ms, deterministic)
       └ HIGH confidence → emit NatSQL → jump to Stage 4
  → Stage 0b — Intent Pattern Library            (<1 ms, deterministic)
       └ produces intent_hints (or empty)
  → Stage 1 — Schema Linker                       (<5 ms, ~10M params)
       └ ranks entities + fields, intent_hints add bias
  → Stage 2 — Skeleton Generator                  (<15 ms, ~20M params)
       └ NatSQL skeleton with @slots; llguidance CFG enforced
  → Stage 3 — Slot Filler + IntentResolver        (<3 ms, ~5M params)
       └ resolves @entity / @field / @val + units + enums
  → Stage 4 — NatSQL → SQL transpiler             (<1 ms, deterministic)
  → Validator (Python sqlglot, always)
  → Mandatory-Filter Injector (Python sqlglot, always)
  → Second-Pass Re-Validator (Rust sqlparser-rs, always)
  → SQL Dialect Renderer (always)
  → Execute + cache
  → Feedback log (zero-row queries, low-confidence stages)
```

Latency budget: ≤ 25 ms on CPU end-to-end for Stages 0a → 4. Validator + injector + dialect render add another ~5 ms on top.

Research provenance:

- [RESDSQL (AAAI 2023)](https://arxiv.org/pdf/2302.05965) — schema-linker + skeleton-decoder cascade.
- [NatSQL (Findings of EMNLP 2021)](https://aclanthology.org/2021.findings-emnlp.174/) — intermediate representation.
- [llguidance](https://github.com/guidance-ai/llguidance) — constrained decoding at 50 µs/token.

## Where each piece lives

### Rust

| Crate                    | Purpose                                                                                                     |
| ------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `semsql-core`            | Generated protobuf bindings + canonical-name newtypes + the one error type re-exported by every other crate. |
| `semsql-graph`           | SQLite-backed SemanticGraph store. Migrations + indexed lookups + provenance.                                |
| `semsql-extract-db`      | DB-side schema introspection (Postgres, MySQL, SQLite, MSSQL).                                               |
| `semsql-intent`          | Stage 0b — Intent Pattern Library matcher (regex + fuzzy).                                                   |
| `semsql-natsql`          | NatSQL grammar + deterministic NatSQL → SQL transpiler.                                                       |
| `semsql-runtime`         | Cascade orchestrator: Stages 0a → 4, ONNX inference, llguidance, per-stage timers and confidence routing.    |
| `semsql-renderer`        | Last-mile dialect string emit (Postgres v0.1; MySQL/SQLite v0.2; MSSQL/BigQuery/Snowflake/DuckDB v1.0).      |
| `semsql-second-pass`     | Independent re-validation via `sqlparser-rs`. Two parsers must agree.                                         |
| `semsql-cli`             | `semsql` binary — `extract`, `query`, `doctor`, `eval`.                                                      |
| `semsql-ffi`             | C ABI / WASM / Node-API / PyO3 bindings for downstream language SDKs.                                       |

### Python

| Package           | Purpose                                                                                              |
| ----------------- | ---------------------------------------------------------------------------------------------------- |
| `semsql_rewriter` | **Security-critical.** sqlglot-based validator + mandatory-filter injector + vocabulary sanitiser.    |
| `semsql_train`    | Per-stage data generators, distillation, ONNX export.                                                |
| `semsql_eval`     | Spider 1.0 / 2.0 + BIRD + per-stage + adversarial + bypass test suites.                              |
| `semsql`          | Python SDK — thin wrapper over `semsql-ffi` (lands in v0.2).                                         |

### TypeScript

| Package                    | Purpose                                                                |
| -------------------------- | ---------------------------------------------------------------------- |
| `@semsql/extractor-sdk`    | Core Extractor protocol + merge engine + provenance plumbing.          |
| `@semsql/extractor-i18n`   | i18n parsers (ICU / gettext / Rails YAML / Laravel lang).              |
| `@semsql/extractor-laravel`| Laravel + Filament adapter — v0.1 priority.                            |
| `@semsql/extractor-cli`    | `semsql-extract` Node binary — orchestrator over the adapters.         |

### Other

| Path                  | Purpose                                                                                       |
| --------------------- | --------------------------------------------------------------------------------------------- |
| `schemas/`            | The canonical contract — `semantic_graph.proto`, `training_pair.proto`, `natsql.bnf`.         |
| `intent-library/`     | Stage 0b's PCRE pattern registry — community-extensible idiom mappings.                       |
| `models/cascade/`     | Three ONNX files (linker / skeleton / slot_filler) + manifest. Shipped with the package.      |
| `models/fine-tunes/`  | Optional per-app Stage 1/3 distilled deltas.                                                  |
| `examples/`           | `laravel-filament-demo/`, `framework-agnostic-demo/`.                                         |

## Security model

### Layered defense

1. **Vocabulary sanitisation at extraction time.** Every term entering the SemanticGraph is checked: canonical names match `[A-Za-z_][A-Za-z0-9_]{0,63}`, labels are NFC-normalised and length-capped. Failures go to `conflict_log`. Implemented in [`packages/extractor-sdk/src/sanitise.ts`](../packages/extractor-sdk/src/sanitise.ts), [`python/semsql_rewriter/src/semsql_rewriter/sanitiser.py`](../python/semsql_rewriter/src/semsql_rewriter/sanitiser.py), and [`crates/semsql-core/src/ids.rs`](../crates/semsql-core/src/ids.rs). All three implementations enforce the same rule and have parallel test suites.
2. **Statement-type allowlist.** The validator rejects DML / DDL / multi-statement input at the AST level. Banned-function deny-list covers `pg_read_server_files`, `lo_import`, `dblink`, `xp_cmdshell`, `load_file`, etc.
3. **Mandatory-filter injection.** sqlglot's expression visitor walks every `Table` node — including in CTEs, subqueries, derived tables, set operations, lateral joins, and recursive CTEs — and injects scope predicates with proper alias rewriting. Idempotent. Audit-logged.
4. **Independent second pass.** The Rust `semsql-second-pass` crate re-parses the rewritten SQL with `sqlparser-rs` (a different implementation from sqlglot) and re-verifies the same invariants. Disagreement fails closed with `ParserDisagreement`.
5. **Database-level RLS.** Production deployments are required to enable Postgres RLS (or vendor equivalent: BigQuery row access policies, Spanner fine-grained access control). `semsql doctor` blocks deployment if RLS is missing on tenanted tables.

### Reference incidents

| CVE / report           | What it teaches us                                                                                                  |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------- |
| [CVE-2025-48912](https://www.miggo.io/vulnerability-database/cve/CVE-2025-48912) (Apache Superset) | RLS bypass via SQL parser weakness; the fix replaced the validator with sqlglot. We adopt sqlglot from day one and back it with an independent Rust parser. |
| [CVE-2025-1094](https://www.rapid7.com/blog/post/2025/02/13/cve-2025-1094-postgresql-psql-sql-injection-fixed/) (PostgreSQL psql) | Even native escaping has bypass paths via invalid UTF-8. We NFC-normalise inputs and never construct SQL via string interpolation. |

## Build commands

```bash
# Rust
cargo build --workspace
cargo test  --workspace
cargo clippy --workspace --all-targets -- -D warnings

# Python
uv sync --all-extras
uv run pytest

# TypeScript
pnpm install
pnpm -r build
pnpm -r test

# Top-level orchestrator (everything above)
just build
just test
```

## Roadmap

| Milestone | Focus                                                                                                   |
| --------- | ------------------------------------------------------------------------------------------------------- |
| **v0.1**  | Rewriter foundation, Stage 0a/0b deterministic logic, Postgres dialect, no models. Pen-test suite seeded. |
| **v0.2**  | Stages 1–3 distilled + ONNX-served. Per-stage eval. Spider 1.0 dev ≥ 65 %. Next.js extractor.            |
| **v0.5**  | Optional Stage 1/3 fine-tune. MySQL + SQLite renderers. Rails + Vue + Django extractors. Browser ONNX. Spider ≥ 75 %. |
| **v1.0**  | Scaled-up Stage 2 option. All 7 dialects. Repair-mode decoding. `semsql doctor` RLS check. Spider ≥ 80 %. Public launch. |

The full per-version detail and risk register live in `~/.claude/plans/i-want-to-build-wiggly-puzzle.md`.

## Contributing

See the README of each layer:

- [`crates/semsql-core/`](../crates/semsql-core) for any change that touches the canonical types.
- [`python/semsql_rewriter/README.md`](../python/semsql_rewriter/README.md) for security-critical work.
- [`intent-library/README.md`](../intent-library/README.md) to add new idioms.

Every PR runs `just test`; security-touching PRs additionally run the bypass pen-test suite.
