# Real DB MySQL/MariaDB Schema-Only Probe Suite

- status: `PASS`
- seeds: `20260604, 20260605, 20260606, 20260607, 20260608`
- databases: `fraud_radar, hostshell, maildb, mailer_web`
- safety mode: `schema-only extraction; required count-only execution; optional governed analytics execution; no result values retained`

## Summary

- runs passed: `5/5`
- runs skipped: `0`
- runs failed/error: `0`
- questions: `85`
- required contract: `45/45`
- count-only routes: `41`
- executed count-only queries: `41`
- analytics probes: `40`
- analytics ok: `40/40`
- analytics gaps: `0`
- executed governed analytics queries: `40`
- execution errors: `0`
- safe not-executed routes/rejects: `4`
- semantic ok or safe not-executed: `85/85`
- needs review: `0`
- sample-value rows: `0`

## Runs

| Seed | Status | Database | Questions | Count-only | Executed | Safe rejects | Review | Sample rows |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| `20260604` | `PASS` | `mailer_web` | `23` | `10` | `10` | `4` | `0` | `0` |
| `20260605` | `PASS` | `fraud_radar` | `19` | `10` | `10` | `0` | `0` | `0` |
| `20260606` | `PASS` | `fraud_radar` | `19` | `10` | `10` | `0` | `0` | `0` |
| `20260607` | `PASS` | `maildb` | `5` | `1` | `1` | `0` | `0` | `0` |
| `20260608` | `PASS` | `hostshell` | `19` | `10` | `10` | `0` | `0` | `0` |
