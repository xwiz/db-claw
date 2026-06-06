# QueryFrame Canary Suite

- status: `FAIL`
- variants: `commerce, alias, random_alias`
- seeds: `20260601, 20260602, 20260603`
- runs: `0/9`
- routed exec accuracy: `135/144`
- reject fail-closed: `18/18`

## Runs

| variant | seed | routed | rejects | status |
|---|---:|---:|---:|---|
| `commerce` | `20260601` | `15/16` | `2/2` | `FAIL` |
| `commerce` | `20260602` | `15/16` | `2/2` | `FAIL` |
| `commerce` | `20260603` | `15/16` | `2/2` | `FAIL` |
| `alias` | `20260601` | `15/16` | `2/2` | `FAIL` |
| `alias` | `20260602` | `15/16` | `2/2` | `FAIL` |
| `alias` | `20260603` | `15/16` | `2/2` | `FAIL` |
| `random_alias` | `20260601` | `15/16` | `2/2` | `FAIL` |
| `random_alias` | `20260602` | `15/16` | `2/2` | `FAIL` |
| `random_alias` | `20260603` | `15/16` | `2/2` | `FAIL` |

## Failures

### `commerce` seed `20260601`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `commerce` seed `20260602`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `commerce` seed `20260603`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `alias` seed `20260601`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `alias` seed `20260602`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `alias` seed `20260603`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `random_alias` seed `20260601`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `random_alias` seed `20260602`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
### `random_alias` seed `20260603`
- `exec_mismatch`: who has external code 00D4 (stage `stage_0a`)
