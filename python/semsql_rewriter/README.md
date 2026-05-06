# semsql-rewriter

Security-critical SQL rewriter — sqlglot-based validator + mandatory-filter injector + vocabulary sanitiser.

## Install

```bash
uv sync --all-extras
```

## Test

```bash
uv run pytest python/semsql_rewriter
```

## Modules

| Module        | Role                                                                  |
| ------------- | --------------------------------------------------------------------- |
| `sanitiser`   | Vocabulary input sanitisation — runs at extraction time.              |
| `validator`   | AST-level statement-type allowlist + banned-function deny-list.       |
| `injector`    | Mandatory-filter injection across CTEs, subqueries, UNION, lateral.   |

## Why sqlglot

[Apache Superset CVE-2025-48912](https://www.miggo.io/vulnerability-database/cve/CVE-2025-48912) was an RLS SQLi bypass that a custom in-app validator missed. The fix replaced that validator with sqlglot. We adopt sqlglot from day one and back it with an independent Rust `sqlparser-rs` second-pass — two parsers must agree.

For the cascade architecture and where this layer fits, see [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md).
