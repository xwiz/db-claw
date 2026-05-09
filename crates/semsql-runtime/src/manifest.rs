//! Cascade manifest loader.
//!
//! Mirrors the JSON shape produced by
//! `python/semsql_train/src/semsql_train/onnx_export.py`. The runtime
//! consumes this manifest at start-up: each stage opens its ONNX file
//! via `ort` and its tokenizer via `tokenizers` lazily.
//!
//! Schema versioning policy: the runtime supports manifests up to and
//! including `MANIFEST_SCHEMA_VERSION`. Newer manifests are rejected with
//! a clear error pointing at the version mismatch — we *never* try to
//! best-effort load a future schema, because that's how silent
//! mis-quantisation lands in production.
//!
//! All paths in the manifest are stored relative to the manifest file's
//! parent directory. The loader resolves them eagerly so callers don't
//! have to track the manifest's location separately.

use semsql_core::{Result, SemsqlError};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Maximum manifest schema version this runtime supports. Bump on every
/// breaking change to the JSON shape; the Python writer in
/// `onnx_export.py` carries the same constant under the same name.
pub const MANIFEST_SCHEMA_VERSION: u32 = 1;

/// One stage's artifacts on disk.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct StageArtifact {
    /// ONNX file path, resolved against the manifest's parent directory.
    ///
    /// For seq2seq stages (Stage 2 / skeleton generator), this may point
    /// to a **directory** produced by `optimum` rather than a single ONNX
    /// file. The directory is expected to contain `encoder_model.onnx` and
    /// `decoder_model.onnx`. Use `is_seq2seq_dir()` to detect this case.
    pub path: PathBuf,
    /// Tokenizer file path, resolved against the manifest's parent.
    pub tokenizer: PathBuf,
    /// Total parameter count — surfaced by `semsql doctor` for
    /// "is this the model you think it is?" checks. May be 0 if the
    /// exporter couldn't read the ONNX initialisers.
    pub params: u64,
}

impl StageArtifact {
    /// True when `path` is a directory (seq2seq model exported by optimum
    /// into an `encoder_model.onnx` + `decoder_model.onnx` pair) rather
    /// than a single-file ONNX artifact.
    pub fn is_seq2seq_dir(&self) -> bool {
        self.path.is_dir()
    }
}

/// Cascade manifest. Mirrors the Python `Manifest` dataclass.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct CascadeManifest {
    /// Manifest schema version. Rejected if newer than
    /// [`MANIFEST_SCHEMA_VERSION`].
    pub schema_version: u32,
    /// Cascade weight version, e.g. `"v0.2.0"`. Surfaced verbatim.
    pub cascade_version: String,
    /// Stage 1 — schema linker.
    pub linker: StageArtifact,
    /// Stage 2 — skeleton generator.
    pub skeleton: StageArtifact,
    /// Stage 3 — slot filler.
    pub slot_filler: StageArtifact,
    /// NatSQL grammar file path, resolved against the manifest parent.
    /// Defaults to `natsql.lark` if missing in older manifests.
    #[serde(default = "default_grammar")]
    pub natsql_grammar: PathBuf,
}

fn default_grammar() -> PathBuf {
    PathBuf::from("natsql.lark")
}

impl CascadeManifest {
    /// Read the manifest at `path`, resolve every relative artifact path
    /// against the manifest's parent directory, and validate schema
    /// version + file existence.
    ///
    /// File-existence checks are deliberately performed here (not at
    /// model-load time) so a misconfigured deployment fails fast with a
    /// clear "manifest references a missing file" message instead of an
    /// opaque ort error mid-query.
    pub fn load(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path)
            .map_err(|e| SemsqlError::Other(format!("read manifest `{}`: {e}", path.display())))?;
        let mut raw: CascadeManifest = serde_json::from_str(&text).map_err(|e| {
            SemsqlError::Other(format!("parse manifest `{}`: {e}", path.display()))
        })?;
        if raw.schema_version > MANIFEST_SCHEMA_VERSION {
            return Err(SemsqlError::Other(format!(
                "manifest `{}` schema_version={} is newer than runtime supports ({})",
                path.display(),
                raw.schema_version,
                MANIFEST_SCHEMA_VERSION,
            )));
        }
        let parent = path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        raw.linker = resolve_stage(&parent, &raw.linker, "linker", path)?;
        raw.skeleton = resolve_stage(&parent, &raw.skeleton, "skeleton", path)?;
        raw.slot_filler = resolve_stage(&parent, &raw.slot_filler, "slot_filler", path)?;
        // Capture the *pre-resolution* grammar path so we can tell the
        // serde-default ("natsql.lark") apart from a user-supplied one.
        // After `resolve_relative` both forms become absolute, which
        // erases the distinction the missing-file check needs.
        let grammar_was_default = raw.natsql_grammar == default_grammar();
        raw.natsql_grammar = resolve_relative(&parent, &raw.natsql_grammar);
        if !raw.natsql_grammar.exists() && !grammar_was_default {
            return Err(SemsqlError::Other(format!(
                "manifest `{}` references missing grammar file `{}`",
                path.display(),
                raw.natsql_grammar.display()
            )));
        }
        Ok(raw)
    }
}

fn resolve_stage(
    parent: &Path,
    stage: &StageArtifact,
    name: &str,
    manifest_path: &Path,
) -> Result<StageArtifact> {
    let onnx = resolve_relative(parent, &stage.path);
    let tok = resolve_relative(parent, &stage.tokenizer);
    if !onnx.exists() {
        return Err(SemsqlError::Other(format!(
            "manifest `{}` references missing {name} ONNX/directory `{}`",
            manifest_path.display(),
            onnx.display(),
        )));
    }
    // For seq2seq stages the path may be a directory — verify that the
    // expected sub-files exist so operators see a clear error instead of
    // a cryptic ort::Error mid-inference.
    if onnx.is_dir() {
        let enc = onnx.join("encoder_model.onnx");
        let dec = onnx.join("decoder_model.onnx");
        if !enc.exists() {
            return Err(SemsqlError::Other(format!(
                "manifest `{}`: {name} directory `{}` missing encoder_model.onnx",
                manifest_path.display(),
                onnx.display(),
            )));
        }
        if !dec.exists() {
            return Err(SemsqlError::Other(format!(
                "manifest `{}`: {name} directory `{}` missing decoder_model.onnx",
                manifest_path.display(),
                onnx.display(),
            )));
        }
    }
    if !tok.exists() {
        return Err(SemsqlError::Other(format!(
            "manifest `{}` references missing {name} tokenizer `{}`",
            manifest_path.display(),
            tok.display(),
        )));
    }
    Ok(StageArtifact {
        path: onnx,
        tokenizer: tok,
        params: stage.params,
    })
}

fn resolve_relative(parent: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        parent.join(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn touch(path: &Path) {
        if let Some(p) = path.parent() {
            std::fs::create_dir_all(p).unwrap();
        }
        std::fs::write(path, b"").unwrap();
    }

    #[test]
    fn loads_well_formed_manifest_resolving_relative_paths() {
        let dir = tempdir().unwrap();
        let root = dir.path();
        touch(&root.join("linker.onnx"));
        touch(&root.join("linker.tok.json"));
        touch(&root.join("skeleton.onnx"));
        touch(&root.join("skeleton.tok.json"));
        touch(&root.join("slot.onnx"));
        touch(&root.join("slot.tok.json"));
        touch(&root.join("natsql.lark"));

        let json = serde_json::json!({
            "schema_version": 1,
            "cascade_version": "v0.2.0",
            "linker":      {"path": "linker.onnx",   "tokenizer": "linker.tok.json",   "params": 9_500_000},
            "skeleton":    {"path": "skeleton.onnx", "tokenizer": "skeleton.tok.json", "params": 19_800_000},
            "slot_filler": {"path": "slot.onnx",     "tokenizer": "slot.tok.json",     "params": 4_900_000},
            "natsql_grammar": "natsql.lark"
        });
        let mp = root.join("manifest.json");
        std::fs::write(&mp, json.to_string()).unwrap();

        let m = CascadeManifest::load(&mp).unwrap();
        assert_eq!(m.cascade_version, "v0.2.0");
        assert!(m.linker.path.is_absolute() || m.linker.path.starts_with(root));
        assert_eq!(m.linker.params, 9_500_000);
    }

    #[test]
    fn rejects_newer_schema_version() {
        let dir = tempdir().unwrap();
        let mp = dir.path().join("m.json");
        std::fs::write(
            &mp,
            r#"{"schema_version": 999, "cascade_version": "x", "linker":{"path":"a","tokenizer":"b","params":0}, "skeleton":{"path":"c","tokenizer":"d","params":0}, "slot_filler":{"path":"e","tokenizer":"f","params":0}}"#,
        )
        .unwrap();
        let err = CascadeManifest::load(&mp).unwrap_err();
        let msg = format!("{err}");
        assert!(msg.contains("schema_version=999"), "got: {msg}");
    }

    #[test]
    fn missing_default_grammar_is_tolerated_but_explicit_one_is_not() {
        let dir = tempdir().unwrap();
        let root = dir.path();
        for name in ["linker.onnx", "linker.tok", "sk.onnx", "sk.tok", "sl.onnx", "sl.tok"] {
            touch(&root.join(name));
        }

        // (a) Manifest with no grammar field → serde default `natsql.lark`,
        // file absent → must succeed (tolerated).
        let json_default = serde_json::json!({
            "schema_version": 1, "cascade_version": "v0",
            "linker":      {"path": "linker.onnx", "tokenizer": "linker.tok", "params": 0},
            "skeleton":    {"path": "sk.onnx",     "tokenizer": "sk.tok",     "params": 0},
            "slot_filler": {"path": "sl.onnx",     "tokenizer": "sl.tok",     "params": 0}
        });
        let mp = root.join("default.json");
        std::fs::write(&mp, json_default.to_string()).unwrap();
        CascadeManifest::load(&mp).expect("missing default grammar must be tolerated");

        // (b) Manifest naming an *explicit* missing grammar file → reject.
        let json_explicit = serde_json::json!({
            "schema_version": 1, "cascade_version": "v0",
            "linker":      {"path": "linker.onnx", "tokenizer": "linker.tok", "params": 0},
            "skeleton":    {"path": "sk.onnx",     "tokenizer": "sk.tok",     "params": 0},
            "slot_filler": {"path": "sl.onnx",     "tokenizer": "sl.tok",     "params": 0},
            "natsql_grammar": "custom_grammar.lark"
        });
        let mp2 = root.join("explicit.json");
        std::fs::write(&mp2, json_explicit.to_string()).unwrap();
        let err = CascadeManifest::load(&mp2).unwrap_err();
        assert!(
            format!("{err}").contains("missing grammar"),
            "got: {err}"
        );
    }

    #[test]
    fn rejects_manifest_referencing_missing_onnx() {
        let dir = tempdir().unwrap();
        let root = dir.path();
        touch(&root.join("linker.tok.json"));
        // Skip linker.onnx — the loader must reject.
        let json = serde_json::json!({
            "schema_version": 1,
            "cascade_version": "v0",
            "linker":      {"path": "linker.onnx",   "tokenizer": "linker.tok.json",   "params": 0},
            "skeleton":    {"path": "x.onnx",        "tokenizer": "x.tok",             "params": 0},
            "slot_filler": {"path": "y.onnx",        "tokenizer": "y.tok",             "params": 0}
        });
        let mp = root.join("m.json");
        std::fs::write(&mp, json.to_string()).unwrap();
        let err = CascadeManifest::load(&mp).unwrap_err();
        assert!(format!("{err}").contains("missing linker ONNX"));
    }
}
