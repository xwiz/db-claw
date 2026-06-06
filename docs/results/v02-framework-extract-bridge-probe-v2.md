# v0.2 Framework Extract Bridge Probe v2
Date: 2026-06-05

Purpose: verify the native `semsql extract --framework <name>` bridge across
multiple framework source surfaces, with source canonicals resolved against a
DB-grounded graph.

## Result

Pass.

Replay path: `target/v02/framework-bridge-probe-v2`

Command:

```bash
uv run python -m semsql_eval framework-bridge-probe \
  --out target/v02/framework-bridge-probe-v2 \
  --semsql-bin target/debug/semsql.exe \
  --out-json target/v02/framework-bridge-probe-v2/report.json \
  --out-md target/v02/framework-bridge-probe-v2/report.md
```

| Framework | Source Surface | Expected Vocab | Query Check |
|---|---|---:|---:|
| Laravel | Filament resource labels | `2/2` | `1/1` |
| Django | model verbose names, choices | `3/3` | `1/1` |
| Rails | ActiveRecord locale labels | `2/2` | `1/1` |
| Next.js | Zod `.describe()` labels | `1/1` | n/a |
| Vue | SFC `label` + `v-model` | `1/1` | n/a |

Totals: `5/5` frameworks, `9/9` expected vocab mappings, `3/3` query checks.

## Fixes Proven

- Framework model-style canonicals such as `user.status` now resolve to
  DB-grounded plural graph fields such as `users.status` when unambiguous.
- Source-only fields that do not exist in the DB graph are skipped instead of
  becoming misleading vocabulary.
- Django and Laravel tree-sitter loaders use ESM-safe `createRequire`, so real
  extractor CLI runs no longer silently degrade to zero parser output.

## Limits

This proves fixture breadth for the framework bridge. It does not yet prove
fresh package-install availability, large real-app extraction coverage, or every
framework-specific UI/component idiom.
