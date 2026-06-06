# Package Launcher Smoke v1

Date: 2026-06-06

## Result

PASS. A clean local install of the packed `@semsql/cli` tarball exposed the
`semsql` bin, honored `SEMSQL_BIN`, resolved a native binary through a
release-style manifest, and failed closed when downloads were disabled.

## Evidence

- command: `uv run python -m semsql_eval package-launcher-smoke --out target\v02\package-launcher-smoke-v1 --semsql-bin target\debug\semsql.exe`
- package manager: `pnpm`
- target: `win32-x64`
- checks: `6/6`
- artifacts: `target\v02\package-launcher-smoke-v1\report.json`, `target\v02\package-launcher-smoke-v1\report.md`

## Read

This closes the local fresh-install launcher gap that unit tests and the PATH
bridge probe did not cover. It still does not replace a real tagged-release
run, signed or attested assets, published packages, or fresh
`pnpm dlx @semsql/cli@<version>` proof.
