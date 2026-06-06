# Package dlx Smoke v1

Date: 2026-06-06

## Result

PASS. `pnpm dlx --package <local @semsql/cli tarball> semsql --version`
works through both `SEMSQL_BIN` and manifest download/cache, and fails closed
when downloads are disabled.

## Evidence

- command: `uv run python -m semsql_eval package-dlx-smoke --out target\v02\package-dlx-smoke-v1 --semsql-bin target\debug\semsql.exe`
- package manager: `pnpm`
- target: `win32-x64`
- checks: `4/4`
- artifacts: `target\v02\package-dlx-smoke-v1\report.json`, `target\v02\package-dlx-smoke-v1\report.md`

## Read

This is closer to the intended `pnpm dlx` user path than a plain local install.
It still uses a local tarball and local file manifest. Full extractor-stack
`dlx` remains unproven until all internal `@semsql/*` packages are published to
a registry, because `@semsql/extractor-cli` depends on those packages by
version.
