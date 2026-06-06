# v0.2 Public Package Smoke, Alpha 5

Date: 2026-06-06. Retained release packaging proof; status lives in
[v02-current-status.md](v02-current-status.md).

## Result

Local and GitHub Actions public-registry smoke against
`@semsql/*@0.1.0-alpha.5` passed after switching the smoke command from
`pnpm dlx --package ...` to the portable `pnpm --package ... dlx ...` form.

Checks:

- package versions: `9/9`
- `@semsql/cli` version command: pass
- Laravel fixture extract via published packages: pass
- query against generated graph: pass
- `@semsql/extractor-cli --help`: pass
- `@semsql/extractor-cli --version`: pass

Artifact:

- `target/package-public-smoke-local-alpha5-portable/report.json`
- GitHub Actions run `27074347744`, artifact `semsql-package-public-smoke`

## CI Root Cause

GitHub release run `27073242093` reached successful GitHub asset upload and
npm publish, then failed only in `Public package smoke`. The smoke report showed
that the runner treated `--package` as a package name:

`GET https://registry.npmjs.org/--package: Not Found - 404`

That means the failure was command-shape incompatibility in the smoke harness
and docs, not package publication, npm auth, release assets, or binary download.

Final proof: GitHub release run `27074347744` passed with public package smoke
status `PASS` and all six checks green.
