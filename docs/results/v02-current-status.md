# v0.2 Current Status
2026-06-06: pilot-safe for grounded read routes; not broad NL-to-SQL go-live.
Evidence: Python/Rust+`onnx`/pnpm gates pass; Pathway strict `31/31` routes, `13/13` fail-closed non-routes, `0` wrong SQL; QueryFrame suite `144/144` routed and `18/18` rejects. See [v02-evidence-ledger.md](v02-evidence-ledger.md).
Caveat: BIRD first-100 `5/100`; research-only, not a release gate.
Next: commit/push, run the real pre-release workflow for `v0.1.0-alpha.1`, then pass public package smoke.
