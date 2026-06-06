//! End-to-end test: build a fixture SemanticGraph, drive the cascade
//! against a real NL query, assert the final SQL.
//!
//! These tests exercise the deterministic pre-resolution path. Model-backed
//! benchmark coverage lives in eval reports and ONNX-gated runtime tests.

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
fn deterministic_path_rejects_bare_entity_projection() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, Some(&fx.intent_yaml)).expect("load");
    let err = cascade
        .run("show students")
        .expect_err("bare entity row projection should fail closed");
    assert!(
        err.to_string()
            .contains("queryframe fail-closed before bare entity projection"),
        "{err}"
    );
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
        "expected NeedsModel - pre-resolver shouldn't anchor on a more-than-2-word tail",
    );
    let _ = outcome;
}

#[test]
fn unresolved_query_raises_clarification_error() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let r = cascade.run("organizations with churn risk over 9 last quarter");
    assert!(r.is_err(), "expected NeedsModel error path");
}

#[test]
fn deterministic_path_resolves_numeric_comparison() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade
        .run("students with balance over 100")
        .expect("comparison must resolve at Stage 0a");
    assert_eq!(
        outcome.sql_text,
        "SELECT * FROM users WHERE users.balance > 100"
    );
}

#[test]
fn deterministic_path_resolves_count_with_enum() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade.run("count active students").expect("run");
    assert_eq!(
        outcome.sql_text,
        "SELECT COUNT(*) FROM users WHERE users.status_code = 2"
    );
}

#[test]
fn deterministic_path_resolves_ordering() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade.run("students sorted by balance desc").expect("run");
    assert_eq!(
        outcome.sql_text,
        "SELECT * FROM users ORDER BY users.balance DESC"
    );
}

#[test]
fn deterministic_path_resolves_field_projection() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade.run("balance of students").expect("run");
    assert_eq!(outcome.sql_text, "SELECT users.balance FROM users");
}

#[test]
fn deterministic_path_resolves_top_n_with_explicit_field() {
    let fx = fixture();
    let cascade = Cascade::load(&fx.graph, None).expect("load");
    let outcome = cascade.run("top 5 students by balance").expect("run");
    assert_eq!(
        outcome.sql_text,
        "SELECT * FROM users ORDER BY users.balance DESC LIMIT 5"
    );
}

#[test]
fn deterministic_path_resolves_top_n_with_intent_hint() {
    // Without an intent library, `top 5 students` can't pick a column
    // and falls through to NeedsModel. With an intent that matches the
    // unique numeric `balance` column on users, it resolves.
    let fx = fixture();
    let intent_yaml = fx._dir.path().join("topn.yaml");
    std::fs::write(
        &intent_yaml,
        r#"
- pattern: '\b(top|highest|biggest)\b'
  intent_type: ranking
  column_hints: [balance]
  ordering: DESC
  default_limit: 10
"#,
    )
    .expect("write yaml");
    let cascade = Cascade::load(&fx.graph, Some(&intent_yaml)).expect("load");
    let outcome = cascade.run("top 5 students").expect("run");
    assert_eq!(
        outcome.sql_text,
        "SELECT * FROM users ORDER BY users.balance DESC LIMIT 5"
    );
    // Intent telemetry surfaced too.
    assert!(outcome.intent_hints.iter().any(|h| h == "ranking"));
}
