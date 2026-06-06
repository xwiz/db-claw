# Release Version Prep Dry Run v1

Date: 2026-06-06

## Result

PASS. `scripts/prepare_release_versions.py` planned an alpha version update
without mutating the checkout.

## Evidence

- command: `python scripts/prepare_release_versions.py --version 0.1.0-alpha.1 --include-python --python-version 0.1.0a1`
- mode: `dry-run`
- planned files: `16`
- planned changed files: `16`
- dev-version recheck after dry-run: pass
- artifact: `target\release-smoke\prepare-release-alpha1-dryrun.json`

## Read

The release path now has guarded version preparation. A real alpha release can
intentionally run the same command with `--apply`, then verify Rust/npm tag
coherence before pushing a tag. This does not create a release or publish
packages by itself.
