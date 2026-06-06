# Contributing

SemanticSQL has Rust, Python, and TypeScript pieces. You do not need to
understand all of them to contribute. Pick the layer you are changing, run the
checks for that layer, and keep generated artifacts out of Git.

## Start Here

- Runtime or CLI changes: `crates/semsql-runtime`, `crates/semsql-cli`.
- SQL safety changes: `python/semsql_rewriter`, `crates/semsql-second-pass`.
- Database extraction: `crates/semsql-extract-db`.
- Framework extraction: `packages/extractor-*`.
- Eval and canaries: `python/semsql_eval`.
- Training/export: `python/semsql_train`.
- Docs and release notes: `README.md`, `docs/`, `docs/results/*.md`.

The cross-language contract is the `.semsql` graph schema. When a change affects
that contract, update the readers/writers and tests together.

## Local Checks

Run the smallest useful check first, then broaden before publishing a branch.

```bash
# Rust
cargo test --workspace
cargo clippy --workspace --all-targets

# Python
uv run pytest
uv run ruff check .
uv run mypy .

# TypeScript extractors
pnpm test
pnpm lint
pnpm typecheck

# Artifact guard
python scripts/check_git_artifacts.py --all

# No static query/app shortcuts in production runtime paths
python scripts/audit_static_query_shortcuts.py

# Living-doc size guard
python scripts/check_docs_hygiene.py
```

Use release binaries for timing-sensitive benchmark probes. Debug builds are fine
for normal development, but model-path latency can look worse in debug mode.

## Safety Rules

If you touch any of these areas, add focused tests:

- SQL validation or rendering;
- tenant/RLS scoping;
- vocabulary sanitization;
- QueryFrame routing;
- LLM handoff packets;
- database or framework extraction.

Generated SQL must stay boring. If a question cannot be grounded, scoped, and
validated, the runtime should reject or ask for a narrower question.

## Docs Rules

Public docs should be contributor-facing:

- explain what someone can run or change;
- link to the compact living doc instead of copying histories;
- keep dashboards, ledgers, plans, and checklists compact;
- move run matrices into one retained report or ignored artifact;
- avoid raw JSON dumps, local paths, private database names, and screenshots
  unless they are intentional and small.

Use `docs/results/v02-current-status.md` for the decision read,
`docs/results/v02-evidence-ledger.md` for gate numbers, and
`docs/results/README.md` for retained-report pointers.

Run the docs hygiene check when changing living status, gate, or plan docs:

```bash
python scripts/check_docs_hygiene.py --fail-current-looking --fail-large-retained --top 12
```

Use the warning mode during broad cleanup passes:

```bash
python scripts/check_docs_hygiene.py --warn-current-looking --top 12
```

## Artifact Rules

Do not commit:

- `data/`, `.venv/`, `target/`, `node_modules/`, local caches;
- ONNX models, SQLite/DB files, `.semsql` graphs;
- raw eval JSON under `docs/results/`;
- binaries, archives, logs, or release builds.

Generated outputs should live under ignored directories such as `target/`,
`reports/`, or `artifacts/`. Release binaries belong in GitHub Releases, not the
repository.

Before committing, run:

```bash
python scripts/check_git_artifacts.py
```

Use `--all` when doing a hygiene pass:

```bash
python scripts/check_git_artifacts.py --all
```

## License

By contributing, you agree your work will ship under the project's dual license:
Apache-2.0 OR MIT.
