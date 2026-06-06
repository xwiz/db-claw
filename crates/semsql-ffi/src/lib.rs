//! C ABI / WASM / Node-API / PyO3 bindings for SemanticSQL.
//!
//! This crate currently exposes the shared version surface while the
//! benchmark runtime remains the active integration path.

#![warn(missing_docs)]

/// Crate version, exposed to FFI consumers for compatibility checks.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
