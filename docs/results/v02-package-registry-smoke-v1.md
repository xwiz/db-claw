# Package Registry Smoke

Date: 2026-06-06. Retained local-registry release smoke; status lives in
[v02-current-status.md](v02-current-status.md).

## Result

PASS. Verdaccio local registry published `9/9` internal `@semsql/*`
packages. `pnpm dlx` then installed `@semsql/cli` plus
`@semsql/extractor-cli`, ran native `semsql extract`, ran
`semsql-extract --help`, and queried the generated graph.

## Checks

| Check | Result |
|---|---:|
| `pack_ok` | pass |
| `registry_started` | pass |
| `publish_ok` | pass |
| `dlx_extract_ok` | pass |
| `dlx_query_ok` | pass |
| `dlx_extractor_help_ok` | pass |

## Limits

This uses a throwaway local registry and `SEMSQL_BIN`, so it proves npm package
resolution and full-stack `pnpm dlx` wiring, not public npm publishing or
GitHub Release binary download.
