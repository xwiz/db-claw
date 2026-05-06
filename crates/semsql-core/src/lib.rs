//! Core types shared across every SemanticSQL crate.
//!
//! - Protobuf bindings for `schemas/semantic_graph.proto` and
//!   `schemas/training_pair.proto`.
//! - Newtypes around the canonical names that flow through the cascade.
//! - `SemsqlError` — the one error type every crate re-exports.

#![forbid(unsafe_code)]
#![warn(missing_docs)]

pub mod error;
pub mod ids;

#[allow(missing_docs, clippy::all)]
pub mod proto {
    //! Generated protobuf bindings. Until `cargo build --features build-protos`
    //! has been run at least once, the `proto_gen/` directory may be empty —
    //! in that case this module is intentionally empty as well.

    #[cfg(feature = "build-protos")]
    include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/proto_gen/semsql.v1.rs"));
}

pub use error::{Result, SemsqlError};
pub use ids::{CanonicalName, EntityName, EnumName, FieldName, IntentType};
