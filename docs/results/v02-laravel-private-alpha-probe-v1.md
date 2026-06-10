# Laravel Private-Alpha Probe v1
Date: 2026-06-10.

## Result
PASS on a generated Laravel/Filament app and SQLite schema.

- Eloquent `belongsTo`/`hasMany` supplied a `clients.plan_ref -> packages.code`
  join absent from the physical database.
- Reverse application declarations normalized to one graph edge.
- `show clients in segment enterprise` returned `ask_user` because two
  categorical fields had equally strong field/value evidence.
- Explicit approval selected `packages.plan_level=enterprise`.
- The rerun returned `execute` and rendered a validated join plus the approved
  predicate.
- Accepted wrong SQL: `0`.

Replay:

```bash
uv run python -m semsql_eval laravel-alpha-probe \
  --out target/v02/laravel-alpha-probe-v1 \
  --semsql-bin target/debug/semsql.exe \
  --out-json target/v02/laravel-alpha-probe-v1/report.json \
  --out-md target/v02/laravel-alpha-probe-v1/report.md
```

This closes the generated-fixture Laravel extract/query/resolve/save/rerun
gate. Real-app correction-loop and broader Laravel evidence surfaces remain.
