# v0.2 Sheets Demo Pages
Date: 2026-06-07.

## Status
The browser-only CSV/public Google Sheets demo path is implemented as `@semsql/sheets` and `@semsql/sheets-demo`.

## Fix
`packages/sheets-demo/scripts/build-pages.mjs` now overwrites the known Pages artifact files instead of deleting `target/sheets-demo-pages`. This avoids Windows `EBUSY` failures when a browser or previous smoke import holds the output directory open.

## Verification
- `pnpm --filter @semsql/sheets test` -> 29 passed
- `pnpm --filter @semsql/sheets lint`
- `pnpm --filter @semsql/sheets typecheck`
- `pnpm --filter @semsql/sheets-demo typecheck`
- `pnpm --filter @semsql/sheets-demo lint`
- `pnpm --filter @semsql/sheets-demo build:pages`
- `pnpm --filter @semsql/sheets-demo smoke:pages`

The Pages smoke validates the built artifact, Chart.js config output, and every built-in practical CSV use case.
