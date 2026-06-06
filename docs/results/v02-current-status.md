# v0.2 Current Status
2026-06-06: pilot-safe for grounded read routes; not broad NL-to-SQL go-live.
Evidence: Python/Rust+`onnx`/pnpm pass; Pathway `31/31`, rejects `13/13`, wrong SQL `0`; QueryFrame `144/144`, rejects `18/18`. See [v02-evidence-ledger.md](v02-evidence-ledger.md).
Caveat: BIRD100 `5/100` is research-only. `alpha.1`-`alpha.3` failed before release creation on CI/package wiring.
Next: cut `v0.1.0-alpha.4`, pass GitHub binary release, then publish npm manually and smoke public packages.
