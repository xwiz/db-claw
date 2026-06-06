# Release Workflow Dry Run v1

Date: 2026-06-06

## Result

PASS for workflow structure. `act` can parse the release workflow and enumerate
the native build, npm pack, release, npm publish, and public smoke jobs without
creating a tag, release, or remote workflow run.

## Evidence

- command: `act workflow_dispatch -W .github/workflows/release-binaries.yml -n`
- command: `act -W .github/workflows/release-binaries.yml -l`
- result: dry-run job graph enumerated successfully
- YAML parse: pass via PyYAML
- version checker: Rust/npm `0.1.0-dev` pass

## Limits

No local job execution occurred. Local Docker is unavailable, and `act` skipped
GitHub runner labels instead of running containers. The local `act` binary also
warned that it should be upgraded. This does not replace a real non-draft
pre-release tag workflow run with GitHub-hosted runners, npm publish, public
smoke, and executed attestations.
