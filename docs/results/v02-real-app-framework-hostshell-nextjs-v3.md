# Real App Framework Probe: hostshell Next.js/Drizzle v3

Date: 2026-06-06. Retained proof for monorepo Drizzle framework extraction.

## Result

PASS. A real TypeScript monorepo with Drizzle schemas under
`packages/db/src/schema*` grounded source vocabulary against a populated local
MariaDB schema and routed source-entity count checks.

## Evidence

- app: `C:\dev\hostshell`
- framework/database: `nextjs` / `hostshell`
- graph: `target\v02\real-app-framework-hostshell-nextjs-v3\app.framework.semsql`
- raw source fragments: `174`
- source vocab grounded: `174/174`
- entities/fields/relationships: `15/159/21`
- source-entity query checks: `5/5`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-hostshell-nextjs-v3\report.json`,
  `target\v02\real-app-framework-hostshell-nextjs-v3\report.md`

## Change Proven

- Drizzle extraction now scans monorepo package schema folders such as
  `packages/db/src/schema` and `packages/db/src/schema-mysql`.
- Drizzle table declarations now emit schema-derived entity vocabulary as well
  as field vocabulary, enabling source-entity route checks.

## Limits

This is source/schema grounding evidence. It does not add authored metrics or
prove arbitrary app semantics.
