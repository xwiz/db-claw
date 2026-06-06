# v0.2 Framework Extract Bridge Smoke v1
Date: 2026-06-05

Purpose: verify the native `semsql extract --framework <name>` command can run
the TypeScript framework extractor and merge source aliases into a DB-grounded
graph without a manual JSONL handoff.

Superseded for release evidence by
[v02-framework-extract-bridge-probe-v2.md](v02-framework-extract-bridge-probe-v2.md),
which covers five framework fixtures.

## Result

Pass on a Laravel/Filament fixture plus SQLite schema.

Replay path: `target/v02/framework-extract-smoke`

Command:

```bash
target/debug/semsql extract target/v02/framework-extract-smoke \
  --framework laravel \
  --db-url sqlite:target/v02/framework-extract-smoke/app.sqlite \
  --output target/v02/framework-extract-smoke/app.semsql \
  --no-sample-values
```

Observed:

- DB graph: `1` entity, `2` fields;
- framework source vocab merged: `3` fragments;
- graph vocab total: `7`;
- provider calls: `0`;
- sample rows retained: `0`.

Behavior check:

```bash
target/debug/semsql query --graph target/v02/framework-extract-smoke/app.semsql \
  --dialect sqlite "how many students"
```

Output:

```sql
SELECT COUNT(*) FROM "users"
```

## Limit

This is a command bridge smoke, not broad framework-readiness evidence. The
next proof needs real Laravel/Rails/Django/Next/Vue fixtures and package-install
verification that `semsql-extract` is available beside the native binary.
