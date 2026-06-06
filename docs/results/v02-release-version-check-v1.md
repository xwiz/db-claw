# Release Version Check v1

Date: 2026-06-06

## Result

PASS for the current dev version set. Release-version coherence is now checked
by reusable tooling instead of relying on scattered manual inspection.

## Evidence

- command: `python scripts/check_release_versions.py --expected-version 0.1.0-dev --surface rust --surface npm`
- Rust workspace version: `0.1.0-dev`
- npm release package versions: `10/10`, including root plus 9 release packages
- extended dev check: Rust/npm `0.1.0-dev` plus Python `0.1.0.dev0`
- artifacts: `target\release-smoke\release-version-check-dev.json`, `target\release-smoke\release-version-check-all-dev.json`

## Read

The release workflow now blocks native binary builds when
`Cargo.toml [workspace.package].version` does not match the release tag, and
blocks npm packaging when release package versions do not match the tag. This
prevents a tagged release from uploading a binary that still reports
`0.1.0-dev`.

This is not a completed alpha release. A real pre-release tag still requires
intentional non-dev version preparation, the real workflow run, attestation
execution, package publication, and fresh `pnpm dlx` proof.
