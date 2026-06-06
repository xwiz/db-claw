# npm Release Package Check v1

Date: 2026-06-06

## Result

PASS. The reusable release-package checker validated all nine npm release
packages at `0.1.0-dev`, ran package lint/test/typecheck, packed tarballs, and
found no unresolved `workspace:` dependencies in packed metadata.

## Evidence

- command: `python scripts/check_npm_release_packages.py --expected-version 0.1.0-dev --run-checks --pack-destination target\release-smoke\npm-script-v1 --clean-pack-destination`
- packages: `9/9`
- tarballs: `9/9`
- workspace dependency violations: `0`
- command failures: `0`
- artifact: `target\release-smoke\npm-script-v1\release-package-check.json`

## Read

This replaces the brittle inline release-workflow package checks with a local
and CI-reusable verifier. It still does not prove package publishing or a fresh
`pnpm dlx @semsql/cli@<version>` install from the public registry.
