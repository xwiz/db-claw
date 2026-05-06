// Compiles schemas/*.proto into Rust bindings.
//
// Gated behind the `build-protos` feature so consumers without protoc
// installed can still build the crate (using the checked-in generated code).

fn main() {
    if std::env::var("CARGO_FEATURE_BUILD_PROTOS").is_err() {
        return;
    }

    let schemas_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("schemas");

    let protos = ["semantic_graph.proto", "training_pair.proto"]
        .map(|name| schemas_dir.join(name));

    for p in &protos {
        println!("cargo:rerun-if-changed={}", p.display());
    }

    prost_build::Config::new()
        .out_dir(std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/proto_gen"))
        .compile_protos(&protos, &[schemas_dir])
        .expect("failed to compile protobuf schemas");
}
