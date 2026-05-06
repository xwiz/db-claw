//! C ABI / WASM / Node-API / PyO3 bindings for SemanticSQL.
//!
//! v0.1 ships an empty crate that compiles as both `cdylib` and `rlib` so
//! downstream language bindings (Python via maturin, Node via napi-rs, PHP
//! via FFI) have a stable surface to depend on. Real bindings land in v0.2
//! once the cascade is wired.

#![warn(missing_docs)]

/// Crate version, exposed to FFI consumers for compatibility checks.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
