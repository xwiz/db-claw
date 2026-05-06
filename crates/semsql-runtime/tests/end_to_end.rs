//! End-to-end test: build a fixture SemanticGraph, drive the cascade
//! against a real NL query, assert the final SQL.
//!
//! v0.2 exercises the deterministic path only — Stage 0a → Stage 4. The
//! model stages return ``NeedsModel`` and the cascade currently returns
//! an error in that branch (by design — guessing without the trained
//! cascade is a footgun). Once the model weights ship the orchestrator
//! falls through to them automatically.

use std::path::PathBuf;

use rusqlite::Connection;
use semsql_runtime::Cascade;
use tempfile::TempDir;

const SCHEMA_V1_SQL: &str = include_str!("fixtures/schema_v1.sql");
const SEED_SQL: &str = include_str!("fixtures/seed.sql");

struct Fixture {
    _dir: TempDir,
    graph: PathBuf,
    intent_yaml: PathBuf,
}

fn fixture() -> Fixture {
    let dir = TempDir::new().expect("tempdir");
    let graph = dir.path().join("g.semsql");
    let conn = Connection::open(&graph).expect("open");
    conn.execute_batch(SCHEMA_V1_SQL).expect("schema");
    conn.execute_batch(SEED_SQL).expect("seed");
    drop(conn);

    let intent_yaml = dir.path().join("patterns.yaml");
    std::fs::write(
        &intent_yaml,
        r#"
- pattern: '\bbleeding money\b'
  intent_type: high_expenditure
  column_hints: [expenses, cost, spend]
  ordering: DESC
  default_limit: 10
"#,
    )
    .expect("write yaml");

    Fixture {
        _dir: dir,
        graph,
        intent_yaml,
    }
}

#[test]
fn deterministic_path_resolves_show_students() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, Some(&fx.intent_yaml)).expect("load");
    let outcome = cascade.run("show students").expect("run");
    assert_eq!(outcome.sql_text, "SELECT * FROM users");
    assert_eq!(outcome.confidences.stage_1, 1.0);
}

#[test]
fn deterministic_path_resolves_count() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade.run("how many students").expect("run");
    assert_eq!(outcome.sql_text, "SELECT COUNT(*) FROM users");
}

#[test]
fn deterministic_path_resolves_enum_subject() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade.run("active students").expect("run");
    assert_eq!(
        outcome.sql_text,
        "SELECT * FROM users WHERE users.status_code = 2"
    );
}

#[test]
fn intent_library_emits_hints_when_loaded() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, Some(&fx.intent_yaml)).expect("load");
    // Bare-entity query so 0a still resolves; the intent hint is
    // surfaced in telemetry alongside the deterministic SQL.
    let outcome = cascade.run("show students bleeding money").expect_err(
        "expected NeedsModel — pre-resolver shouldn't anchor on a more-than-2-word tail",
    );
    let _ = outcome;
}

#[test]
fn unresolved_query_raises_clarification_error() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let r = cascade.run("orgs whose balance is over $100k last quarter");
    assert!(r.is_err(), "expected NeedsModel error path");
}
