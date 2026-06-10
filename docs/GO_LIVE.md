# Private-Alpha Packaging Plan

Date: 2026-06-09. Release path, not a result archive.

This document is about packaging and distribution only. Product readiness lives
in `docs/results/v02-current-status.md`. DB Claw is not a broad production
NL-to-SQL system. The resolution-decision loop now chooses `execute`,
`ask_user`, `ask_llm`, or `reject` and persists approved mappings; the remaining
release work is listed below.

## Ship Surface

Ship native `semsql` binaries on GitHub Releases, `@semsql/cli` as the npm
launcher/downloader, and framework extractor packages starting with Laravel;
non-release demos stay private. Laravel users should not need Rust tooling.
A Composer wrapper can come later as
a thin shell over the same binary and extractor CLI.

## Binary Launcher

`@semsql/cli` should:

- honor `SEMSQL_BIN`;
- choose the OS/arch target;
- read `semsql-downloads.json`;
- validate asset URL, 64-char SHA-256, optional size, byte length, and checksum;
- cache the binary;
- execute it as `semsql`.

Targets: `win32-x64`, `linux-x64`, `linux-arm64`, `darwin-x64`,
`darwin-arm64`. Manifest entries are `{url, sha256, size}` under
`semsql-downloads.json`.

Generate the manifest from built release assets:

```bash
uv run python scripts/generate_semsql_downloads_manifest.py \
  --version 0.1.0-alpha.5 \
  --base-url https://github.com/xwiz/db-claw/releases/download/v0.1.0-alpha.5 \
  --asset linux-x64=target/release/semsql \
  --asset win32-x64=target/release/semsql.exe \
  --out semsql-downloads.json
```

Retained proofs live in `docs/results`; before publishing, rehearse with
`python scripts/rehearse_release_packages.py --version 0.1.0-alpha.5`.
After publishing, run `python -m semsql_eval package-public-smoke --version <version>` without `--semsql-bin`.

Prepare the actual tag with `python scripts/prepare_release_versions.py
--version 0.1.0-alpha.5 --apply`.

Release automation must build/smoke every target, generate downloads metadata,
pack npm artifacts, reject `workspace:` leaks, version drift, and bad repository
metadata, attest assets, and upload GitHub release assets. Npm publication is a
separate `workflow_dispatch publish_npm=true draft=false` step, followed by
`package-public-smoke`.

## Laravel Alpha

```bash
pnpm --package @semsql/cli@0.1.0-alpha.5 \
  --package @semsql/extractor-cli@0.1.0-alpha.5 dlx \
  semsql extract . \
  --framework laravel \
  --db-url "$DATABASE_URL" \
  --no-sample-values \
  -o storage/semsql/app.semsql

pnpm --package @semsql/cli@0.1.0-alpha.5 dlx semsql doctor --graph storage/semsql/app.semsql
pnpm --package @semsql/cli@0.1.0-alpha.5 dlx semsql query --graph storage/semsql/app.semsql "count active users"
```

For production-like schemas:

- use a read-only DB user and start with `--no-sample-values`;
- capture rejected queries with `--rejection-packet-json`;
- route provider help only through typed proposals;
- execute selected SQL only after local validation/rendering.

## Composer Wrapper
A later thin Composer wrapper may expose Artisan `extract`, `doctor`, and `ask`
commands without duplicating extraction, planning, or rendering.
## Private Alpha Gate

Private alpha is reasonable after all of these are true:

- [x] query JSON includes the resolution decision and atlas-strength report;
- [x] rejected queries produce actionable user/LLM handoff packets;
- [x] approved mappings can be saved and reused without static query shortcuts;
- [ ] local resolver receives desktop/mobile visual QA;
- [x] generated Laravel fixture passes extract, query, resolve, save, and rerun;
- [ ] a held-out real Laravel app passes the same correction loop;
- the release workflow has passed on a real pre-release tag;
- release assets are signed or attested;
- `@semsql/cli` is published under the same non-dev version;
- fresh `pnpm --package @semsql/cli@<version> dlx semsql ...` downloads and runs the tagged binary;
- extractor package tarballs are clean and versioned;
- fresh installs expose `semsql-extract` to native `semsql extract`;
- `semsql doctor` gives actionable diagnostics;
- real MariaDB and Postgres read-only probes pass on disposable or approved
  targets;
- rejected queries produce bounded typed fallback packets;
- reviewed/provider typed proposals validate and render locally;
- direct provider SQL remains rejected;
- final quality gates in `docs/results/v02-quality-gate.md` are green.
Use `v02-current-status.md` for decisions and `v02-evidence-ledger.md` for
gate numbers. Do not copy run histories into this packaging plan.

Crates.io is later: first choose a public pre-release, add crate dependency
versions, publish in order, and document ONNX Runtime for `cargo install`.
