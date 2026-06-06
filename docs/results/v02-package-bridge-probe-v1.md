# Package Bridge Probe v1

Date: 2026-06-05

## Result

PASS. Native `semsql extract --framework ...` found `semsql-extract` through
PATH with workspace fallback disabled.

## Evidence

- command: `uv run python -m semsql_eval package-bridge-probe --out target\v02\package-bridge-probe-v1 --semsql-bin target\debug\semsql.exe`
- workspace fallback: `SEMSQL_EXTRACTOR_DISABLE_WORKSPACE=1`
- installed-style bin: `target\v02\package-bridge-probe-v1\installed-bin\semsql-extract.cmd`
- framework fixtures: `5/5`
- source vocab mappings: `9/9`
- query checks: `3/3`
- artifacts: `target\v02\package-bridge-probe-v1\report.json`, `target\v02\package-bridge-probe-v1\report.md`

## Read

This proves the native binary can execute an npm-style `semsql-extract.cmd`
from PATH instead of relying on `packages/extractor-cli/dist/cli.js` in the
repo. It does not replace a tagged published-package `pnpm dlx` smoke.
