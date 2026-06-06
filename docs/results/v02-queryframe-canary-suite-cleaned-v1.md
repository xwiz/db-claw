# QueryFrame Canary Suite

- status: `FAIL`
- variants: `commerce, alias, random_alias`
- seeds: `20260601, 20260602, 20260603`
- runs: `0/9`
- routed exec accuracy: `126/144`
- reject fail-closed: `18/18`

## Runs

| variant | seed | routed | rejects | status |
|---|---:|---:|---:|---|
| `commerce` | `20260601` | `14/16` | `2/2` | `FAIL` |
| `commerce` | `20260602` | `14/16` | `2/2` | `FAIL` |
| `commerce` | `20260603` | `14/16` | `2/2` | `FAIL` |
| `alias` | `20260601` | `14/16` | `2/2` | `FAIL` |
| `alias` | `20260602` | `14/16` | `2/2` | `FAIL` |
| `alias` | `20260603` | `14/16` | `2/2` | `FAIL` |
| `random_alias` | `20260601` | `14/16` | `2/2` | `FAIL` |
| `random_alias` | `20260602` | `14/16` | `2/2` | `FAIL` |
| `random_alias` | `20260603` | `14/16` | `2/2` | `FAIL` |

## Failures

### `commerce` seed `20260601`
- `exec_mismatch`: top 2 products by order amount (stage `stage_0a`)
- `exec_mismatch`: which 2 products have the highest order amount (stage `stage_0a`)
### `commerce` seed `20260602`
- `exec_mismatch`: top 2 products by order amount (stage `stage_0a`)
- `exec_mismatch`: which 2 products have the highest order amount (stage `stage_0a`)
### `commerce` seed `20260603`
- `exec_mismatch`: top 2 products by order amount (stage `stage_0a`)
- `exec_mismatch`: which 2 products have the highest order amount (stage `stage_0a`)
### `alias` seed `20260601`
- `exec_mismatch`: top 2 items by transaction amount (stage `stage_0a`)
- `exec_mismatch`: which 2 items have the highest transaction amount (stage `stage_0a`)
### `alias` seed `20260602`
- `exec_mismatch`: top 2 items by transaction amount (stage `stage_0a`)
- `exec_mismatch`: which 2 items have the highest transaction amount (stage `stage_0a`)
### `alias` seed `20260603`
- `exec_mismatch`: top 2 items by transaction amount (stage `stage_0a`)
- `exec_mismatch`: which 2 items have the highest transaction amount (stage `stage_0a`)
### `random_alias` seed `20260601`
- `exec_mismatch`: top 2 items by sale amount (stage `stage_0a`)
- `exec_mismatch`: which 2 items have the highest sale amount (stage `stage_0a`)
### `random_alias` seed `20260602`
- `exec_mismatch`: top 2 services by purchase amount (stage `stage_0a`)
- `exec_mismatch`: which 2 services have the highest purchase amount (stage `stage_0a`)
### `random_alias` seed `20260603`
- `exec_mismatch`: top 2 services by purchase amount (stage `stage_0a`)
- `exec_mismatch`: which 2 services have the highest purchase amount (stage `stage_0a`)
