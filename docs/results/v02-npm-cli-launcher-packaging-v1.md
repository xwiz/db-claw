# v0.2 npm CLI Launcher Packaging

Date: 2026-06-05

## Summary

- Verified `@semsql/cli` as the npm launcher package for the native `semsql`
  binary.
- The package does not download during `postinstall`; it resolves/downloads on
  first command run or respects `SEMSQL_BIN`.
- Hardened the release manifest contract:
  - each asset must include a URL;
  - SHA-256 must be a 64-character hex digest;
  - optional `size` must be a non-negative integer;
  - downloaded byte length must match manifest `size` when provided;
  - SHA-256 mismatch still prevents caching.
- Repacked the npm tarball locally at `C:\tmp\semsql-cli-0.1.0-dev.tgz`.
- Added `scripts/generate_semsql_downloads_manifest.py` to generate
  `semsql-downloads.json` from built binaries with URL, SHA-256, and size.
- `@semsql/cli` now reads its installed `package.json` version at runtime
  instead of carrying a second hardcoded TypeScript version.
- Added `.github/workflows/release-binaries.yml`:
  - builds `linux-x64`, `linux-arm64`, `win32-x64`, `darwin-x64`, and
    `darwin-arm64`;
  - smokes every native binary with `semsql --version`;
  - packs the npm launcher plus framework extractor tarballs;
  - rejects packed package metadata that still contains `workspace:`;
  - generates `semsql-downloads.json` from the actual downloaded artifacts;
  - creates or updates the GitHub Release and uploads assets.

## Verification

- `pnpm --filter @semsql/cli test`: pass, `8` downloader tests
- `pnpm --filter @semsql/cli typecheck`: pass
- `pnpm --filter @semsql/cli lint`: pass
- `pnpm --filter @semsql/cli build`: pass
- built `dist/version.js` reports `0.1.0-dev` from package metadata
- `pnpm --dir packages/semsql-cli pack --pack-destination ../../target/release-smoke/npm`:
  pass
- Packed tarball contains only `dist/*` plus `package.json`.
- Packed `dist/launcher.js` keeps `#!/usr/bin/env node`.
- Packed downloader includes manifest size and SHA validation.
- Expanded package pack smoke passes for 9 release packages:
  `@semsql/extractor-sdk`, `@semsql/extractor-i18n`,
  `@semsql/extractor-django`, `@semsql/extractor-laravel`,
  `@semsql/extractor-nextjs`, `@semsql/extractor-rails`,
  `@semsql/extractor-vue`, `@semsql/extractor-cli`, and `@semsql/cli`.
- Packed release package metadata has no unresolved `workspace:` dependencies.
- `scripts/check_npm_release_packages.py` now owns the version, check, pack,
  and `workspace:` validation used locally and by release CI.
- `uv run ruff check scripts/generate_semsql_downloads_manifest.py`: pass
- Manifest generator smoke with dummy assets for all five launcher targets
  writes URL, SHA-256, and size fields under `target/release-smoke`.
- PyYAML parses all `.github/workflows/*.yml`.
- `python scripts/check_git_artifacts.py --all`: pass.

## Remaining Release Work

- Run the workflow on a real pre-release tag.
- Execute the wired release-asset attestation on a real tag run.
- Publish `@semsql/cli` and extractor packages under the same non-dev version.
- Verify fresh `pnpm --package @semsql/cli@<version> dlx semsql ...` install/download behavior.
