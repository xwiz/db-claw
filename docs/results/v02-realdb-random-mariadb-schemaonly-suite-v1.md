# Real DB MySQL/MariaDB Schema-Only Probe Suite

- status: `PASS`
- seeds: `20260601, 20260602, 20260603`
- databases: `fraudv_go, hostshell, maildb`
- safety mode: `schema-only extraction; count-only execution; no result values retained`

## Summary

- runs passed: `3/3`
- runs skipped: `0`
- runs failed/error: `0`
- questions: `21`
- count-only routes: `21`
- executed count-only queries: `21`
- execution errors: `0`
- safe not-executed routes/rejects: `0`
- semantic ok or safe not-executed: `21/21`
- needs review: `0`
- sample-value rows: `0`

## Runs

| Seed | Status | Database | Questions | Count-only | Executed | Safe rejects | Review | Sample rows |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| `20260601` | `PASS` | `fraudv_go` | `10` | `10` | `10` | `0` | `0` | `0` |
| `20260602` | `PASS` | `hostshell` | `10` | `10` | `10` | `0` | `0` | `0` |
| `20260603` | `PASS` | `maildb` | `1` | `1` | `1` | `0` | `0` | `0` |
