//! Differential renderer execution test.
//!
//! Per the implementation plan §Verification#5: every dialect renderer
//! must produce semantically equivalent output. This test materialises
//! a small fixture in-memory, renders the same NatSQL input through
//! every dialect that SQLite can host (PG / MySQL / SQLite / DuckDB /
//! Snowflake — all of which SQLite parses via its dual-quoting
//! support), executes each rendered string, and asserts the row sets
//! are identical.
//!
//! MSSQL `TOP n` + bracket quoting and BigQuery's `project.dataset.table`
//! paths require their own engines and are excluded — the structural
//! invariants (TOP placement, bracket emission, backtick guard) are
//! enforced by the unit tests in `src/lib.rs`.

use rusqlite::{types::Value, Connection};
use semsql_natsql::parse;
use semsql_renderer::{render, Dialect};

/// Build an in-memory SQLite with a simple `users` schema + fixture
/// rows that exercise integers, booleans (stored as `INTEGER`), and
/// nullable strings.
fn make_db() -> Connection {
    let conn = Connection::open_in_memory().expect("open sqlite");
    conn.execute_batch(
        r#"
        CREATE TABLE users (
            id           INTEGER PRIMARY KEY,
            email        TEXT NOT NULL,
            balance      INTEGER NOT NULL,
            is_active    INTEGER NOT NULL,   -- 0 / 1
            status_code  INTEGER NOT NULL,
            deleted_at   TEXT
        );
        INSERT INTO users VALUES
            (1, 'a@x', 50,   1, 1, NULL),
            (2, 'b@x', 100,  1, 2, NULL),
            (3, 'c@x', 250,  0, 2, '2026-01-01'),
            (4, 'd@x', 999,  1, 3, NULL);
        "#,
    )
    .expect("seed schema");
    conn
}

/// Execute a SQL string against the connection and return rows as a
/// canonical `Vec<Vec<Value>>`. Rows are sorted (deterministic order)
/// so dialects whose `LIMIT` lands at different syntactic positions
/// still compare equal when no `ORDER BY` is supplied.
fn run(conn: &Connection, sql: &str) -> Vec<Vec<Value>> {
    let mut stmt = conn.prepare(sql).unwrap_or_else(|e| {
        panic!("prepare failed for `{sql}`: {e}");
    });
    let col_count = stmt.column_count();
    let rows = stmt
        .query_map([], |row| {
            let mut out = Vec::with_capacity(col_count);
            for i in 0..col_count {
                out.push(row.get::<_, Value>(i)?);
            }
            Ok(out)
        })
        .expect("query_map")
        .collect::<Result<Vec<_>, _>>()
        .expect("collect rows");
    let mut sorted = rows;
    sorted.sort_by(|a, b| format!("{a:?}").cmp(&format!("{b:?}")));
    sorted
}

/// Dialects whose output SQLite accepts directly. SQLite parses
/// double quotes as identifiers (PG/SQLite/DuckDB/Snowflake) and
/// backticks as identifiers (MySQL) via its compatibility mode.
const SQLITE_RUNNABLE: &[Dialect] = &[
    Dialect::Postgres,
    Dialect::MySql,
    Dialect::Sqlite,
    Dialect::DuckDb,
    Dialect::Snowflake,
];

/// For every input NatSQL string, render under every SQLite-runnable
/// dialect, execute against an in-memory fixture, and assert that
/// the row sets are equal across dialects.
#[test]
fn renders_produce_identical_row_sets() {
    let conn = make_db();
    let inputs = [
        "SELECT * FROM users",
        "SELECT COUNT(*) FROM users",
        "SELECT users.email FROM users WHERE users.balance > 75",
        "SELECT users.email FROM users WHERE users.is_active = TRUE",
        "SELECT users.email FROM users WHERE users.status_code IN (1, 2)",
        "SELECT users.email FROM users WHERE users.deleted_at IS NULL",
        "SELECT users.email FROM users WHERE users.balance BETWEEN 50 AND 250",
        "SELECT users.email FROM users ORDER BY users.balance DESC LIMIT 2",
    ];

    for input in inputs {
        let ast = parse(input).expect("parse");
        let mut prev_rows: Option<(Dialect, Vec<Vec<Value>>)> = None;
        for &d in SQLITE_RUNNABLE {
            let sql = render(&ast, d).expect("render");
            let rows = run(&conn, &sql);
            if let Some((prev_d, ref pr)) = prev_rows {
                assert_eq!(
                    pr, &rows,
                    "differential mismatch on `{input}` between {prev_d:?} and {d:?}\n\
                     {prev_d:?} rows = {pr:?}\n\
                     {d:?}      rows = {rows:?}"
                );
            }
            prev_rows = Some((d, rows));
        }
    }
}

/// Sanity guard: the renders are *structurally* different across
/// dialects (we want to catch a regression where we accidentally emit
/// the same string for two dialects), even when the rows agree.
#[test]
fn render_strings_differ_across_dialects() {
    let ast = parse("SELECT * FROM users").expect("parse");
    let pg = render(&ast, Dialect::Postgres).unwrap();
    let mysql = render(&ast, Dialect::MySql).unwrap();
    let mssql = render(&ast, Dialect::MsSql).unwrap();
    assert_ne!(pg, mysql, "PG and MySQL must differ on quoting");
    assert_ne!(pg, mssql, "PG and MSSQL must differ on quoting");
    assert_ne!(mysql, mssql, "MySQL and MSSQL must differ on quoting");
}

/// Boolean handling: SQLite renders `is_active = TRUE` as `= 1` so
/// the fixture matches stored ints; PG renders `= TRUE`. Both must
/// return identical rows when run on the same SQLite fixture (which
/// accepts both forms).
#[test]
fn boolean_dialect_divergence_yields_identical_rows() {
    let conn = make_db();
    let ast = parse("SELECT users.email FROM users WHERE users.is_active = TRUE").expect("parse");
    let pg_sql = render(&ast, Dialect::Postgres).unwrap();
    let sqlite_sql = render(&ast, Dialect::Sqlite).unwrap();
    assert_ne!(pg_sql, sqlite_sql, "PG keeps TRUE keyword, SQLite emits 1");
    assert_eq!(run(&conn, &pg_sql), run(&conn, &sqlite_sql));
}
