# Contributing to SemanticSQL

Thanks for considering a contribution. SemanticSQL is built to be reviewed and adopted by senior engineers worldwide — we keep the bar high so the codebase rewards careful reading.

## Pick your layer

Each layer has its own README with focused setup instructions:

- **Rust** → see each crate under `crates/`.
- **Python** → [`python/semsql_rewriter/README.md`](../python/semsql_rewriter/README.md) is the security-critical layer; start there.
- **TypeScript extractors** → [`packages/extractor-sdk`](../packages/extractor-sdk).
- **Intent patterns** → [`intent-library/README.md`](../intent-library/README.md). No code required.

You don't need to know every layer to contribute. The protobuf schema is the contract; everything else is layer-local.

## Conventions

- **Comments**: explain the *why*, not the *what*. Well-named identifiers describe what the code does. Comments are reserved for non-obvious constraints, hidden invariants, and references to external incidents (e.g. CVEs the design defends against).
- **Error handling**: every Rust crate re-exports `semsql_core::SemsqlError`. New errors are additive; never repurpose a discriminant.
- **Security**: any change that touches the validator, injector, sanitiser, or second-pass requires:
    1. A unit test for the new behaviour.
    2. An adversarial test in `python/semsql_eval/src/semsql_eval/adversarial.py`.
    3. Confirmation that both parsers (sqlglot + sqlparser-rs) still agree.
- **Sanitisation parity**: the canonical-name allow-list is mirrored in three places (Rust, Python, TypeScript). Change one, change all. CI enforces this with a parity test.

## Running the suite locally

```bash
just build
just test
```

## License

By contributing, you agree your work will ship under the project's dual-license: Apache-2.0 OR MIT.
