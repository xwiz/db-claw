//! Phase E grammar-fuzz tests.
//!
//! Acceptance criterion (per `docs/completion-plan.md`):
//!
//! > grammar fuzz tests: every gold-decoded NatSQL is accepted
//!
//! This test walks a curated corpus of NatSQL v0.3 forms covering every
//! production the parser/transpiler/grammar quartet supports — single
//! FROM, INNER JOIN chains (1-3), HAVING, GROUP BY, ORDER BY, LIMIT,
//! OFFSET, BETWEEN, IN, LIKE, IS NULL, IS NOT NULL, arithmetic / CAST
//! expressions, COUNT(*), aggregates over fields. Each form is asserted
//! to pass `validate_skeleton_against_schema` against an appropriate
//! schema slice. Failure here indicates a missing arm in the grammar
//! validator and would imply Stage 2 outputs that look like valid gold
//! NatSQL but get rejected by the cascade — the highest-leverage class
//! of regression to catch before Phase D retraining.

use semsql_runtime::grammar::{
    build_natsql_grammar, validate_skeleton_against_schema, GrammarSchema,
};

fn schema(entities: &[&str], fields: &[&str]) -> GrammarSchema {
    GrammarSchema {
        entities: entities.iter().map(|s| s.to_string()).collect(),
        fields: fields.iter().map(|s| s.to_string()).collect(),
        value_slots: vec!["@val1".into(), "@val2".into(), "@val3".into()],
    }
}

#[test]
fn accepts_star_select() {
    let s = schema(&["users"], &["users.id"]);
    validate_skeleton_against_schema("SELECT * FROM users", &s).unwrap();
}

#[test]
fn accepts_single_field() {
    let s = schema(&["users"], &["users.email", "users.id"]);
    validate_skeleton_against_schema(
        "SELECT users.email FROM users",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_count_star() {
    let s = schema(&["users"], &["users.id"]);
    validate_skeleton_against_schema("SELECT COUNT(*) FROM users", &s).unwrap();
}

#[test]
fn accepts_aggregate_over_field() {
    let s = schema(&["users"], &["users.balance"]);
    validate_skeleton_against_schema(
        "SELECT SUM(users.balance) FROM users",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_where_compare_with_param() {
    let s = schema(&["users"], &["users.status_code"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users WHERE users.status_code = :status",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_where_in_list() {
    let s = schema(&["users"], &["users.status_code"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users WHERE users.status_code IN (1, 2, 3)",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_where_between() {
    let s = schema(&["users"], &["users.balance"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users WHERE users.balance BETWEEN 0 AND 100",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_where_is_null_and_is_not_null() {
    let s = schema(&["users"], &["users.deleted_at", "users.id"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users WHERE users.deleted_at IS NULL",
        &s,
    )
    .unwrap();
    validate_skeleton_against_schema(
        "SELECT * FROM users WHERE users.id IS NOT NULL",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_where_like() {
    let s = schema(&["users"], &["users.name"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users WHERE users.name LIKE 'A%'",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_one_inner_join() {
    let s = schema(
        &["users", "orders"],
        &["users.id", "orders.user_id"],
    );
    validate_skeleton_against_schema(
        "SELECT * FROM users INNER JOIN orders ON orders.user_id = users.id",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_three_inner_joins() {
    let s = schema(
        &["a", "b", "c", "d"],
        &[
            "a.id", "a.x",
            "b.id", "b.a_id",
            "c.id", "c.b_id",
            "d.id", "d.c_id",
        ],
    );
    validate_skeleton_against_schema(
        "SELECT a.x FROM a \
         INNER JOIN b ON b.a_id = a.id \
         INNER JOIN c ON c.b_id = b.id \
         INNER JOIN d ON d.c_id = c.id",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_group_by_having() {
    let s = schema(
        &["users"],
        &["users.status_code", "users.balance"],
    );
    validate_skeleton_against_schema(
        "SELECT users.status_code FROM users \
         GROUP BY users.status_code \
         HAVING users.status_code > 2",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_order_by_limit() {
    let s = schema(&["users"], &["users.balance"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_limit_offset() {
    let s = schema(&["users"], &["users.id"]);
    validate_skeleton_against_schema(
        "SELECT * FROM users LIMIT 10 OFFSET 20",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_offset_only() {
    let s = schema(&["users"], &["users.id"]);
    validate_skeleton_against_schema("SELECT * FROM users OFFSET 5", &s).unwrap();
}

#[test]
fn accepts_arithmetic_select_expr() {
    // SelectItem::Expr round-trip — division ratio with CAST, the BIRD
    // shape that 20% of failures in v2-bird-smoke-failures.md hit.
    let s = schema(&["frpm"], &["frpm.free_meals", "frpm.enrollment"]);
    validate_skeleton_against_schema(
        "SELECT CAST(frpm.free_meals AS REAL) / frpm.enrollment FROM frpm",
        &s,
    )
    .unwrap();
}

#[test]
fn accepts_bare_field_unambiguous() {
    let s = schema(&["users"], &["users.email", "users.id"]);
    validate_skeleton_against_schema("SELECT email FROM users", &s).unwrap();
}

#[test]
fn grammar_renders_for_typical_top_k_slice() {
    // Stage 1 top-2/4 emits ~2 entities × ~10 fields. The grammar must
    // build cleanly for that shape — a fuzz that the productions don't
    // depend on a minimum entity count or fixed field-name pattern.
    let s = schema(
        &["e_one", "e_two"],
        &[
            "e_one.f_a", "e_one.f_b", "e_one.f_c", "e_one.f_d", "e_one.f_e",
            "e_two.f_a", "e_two.f_b", "e_two.f_c", "e_two.f_d", "e_two.f_e",
        ],
    );
    let g = build_natsql_grammar(&s);
    assert!(g.contains("INNER"));
    assert!(g.contains("HAVING"));
    assert!(g.contains("arith_expr"));
    assert!(g.contains("\"e_one\""));
    assert!(g.contains("\"e_two.f_a\""));
}

#[test]
fn fuzz_every_form_against_universal_schema() {
    // One big universal schema slice that covers every form below.
    let s = schema(
        &["users", "orders", "items"],
        &[
            "users.id", "users.email", "users.balance", "users.status_code",
            "users.name", "users.deleted_at",
            "orders.id", "orders.user_id", "orders.total",
            "items.id", "items.order_id",
        ],
    );
    let forms = [
        "SELECT * FROM users",
        "SELECT users.email FROM users",
        "SELECT COUNT(*) FROM users",
        "SELECT SUM(users.balance) FROM users",
        "SELECT * FROM users WHERE users.id = 1",
        "SELECT * FROM users WHERE users.status_code IN (1, 2)",
        "SELECT * FROM users WHERE users.balance BETWEEN 0 AND 100",
        "SELECT * FROM users WHERE users.deleted_at IS NULL",
        "SELECT * FROM users WHERE users.email LIKE 'a%'",
        "SELECT * FROM users INNER JOIN orders ON orders.user_id = users.id",
        "SELECT users.email FROM users \
         INNER JOIN orders ON orders.user_id = users.id \
         INNER JOIN items ON items.order_id = orders.id",
        "SELECT users.status_code FROM users \
         GROUP BY users.status_code HAVING users.status_code > 1",
        "SELECT * FROM users ORDER BY users.balance DESC LIMIT 5",
        "SELECT CAST(orders.total AS REAL) / users.balance FROM orders",
    ];
    for form in forms {
        let r = validate_skeleton_against_schema(form, &s);
        assert!(r.is_ok(), "validator rejected gold form `{form}`: {:?}", r.err());
    }
}
