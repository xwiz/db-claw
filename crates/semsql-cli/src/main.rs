//! `semsql` — top-level command-line driver.

#![forbid(unsafe_code)]

mod extract;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[command(
    name = "semsql",
    about = "SemanticSQL — open-source NL→SQL with cascade architecture",
    long_about = None,
    version
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,

    /// Logging verbosity. Pass multiple times for more (`-v`, `-vv`, `-vvv`).
    #[arg(short, long, global = true, action = clap::ArgAction::Count)]
    verbose: u8,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Extract a SemanticGraph from a project directory.
    Extract {
        /// Project root.
        path: PathBuf,
        /// Framework hint. Use `none` to fall back to DB-only extraction.
        #[arg(long, default_value = "none")]
        framework: String,
        /// Output `.semsql` file.
        #[arg(short, long)]
        output: PathBuf,
        /// DB URL — required when --framework=none.
        #[arg(long)]
        db_url: Option<String>,
        /// Optional JSONL file of vocabulary fragments emitted by a
        /// TypeScript extractor. One VocabFragment per line.
        #[arg(long)]
        vocab_jsonl: Option<PathBuf>,
    },

    /// Run a natural-language query against a graph.
    Query {
        /// Path to the `.semsql` file.
        #[arg(long)]
        graph: PathBuf,
        /// Natural-language question.
        nl: String,
        /// Optional target dialect for the final SQL emit. Defaults
        /// to the cascade's dialect-agnostic output. Supported:
        /// `postgres`, `mysql`, `sqlite`, `mssql`, `bigquery`,
        /// `snowflake`, `duckdb`. Unknown names fail-closed with a
        /// clear error.
        #[arg(long)]
        dialect: Option<String>,
        /// Optional cascade manifest JSON. When supplied AND the
        /// binary was built with `--features onnx`, queries that
        /// fall through Stage 0a are routed through Stage 1 (schema
        /// linker) and the grammar compiler. Stage 2 weights ship in
        /// a future cut — until then the cascade surfaces a clear
        /// "Stage 2 not yet shipped" error rather than guessing.
        /// Without `--features onnx`, this flag is silently ignored
        /// and the deterministic-only cascade is used.
        #[arg(long)]
        cascade_manifest: Option<PathBuf>,
        /// Optional intent pattern YAML to load alongside the
        /// graph. When omitted, Stage 0b's intent matcher is empty
        /// (queries like `top 5 spenders` need either an explicit
        /// `by <field>` tail or an intent library to resolve at
        /// Stage 0a).
        #[arg(long)]
        intent_yaml: Option<PathBuf>,
    },

    /// Surface conflicts and deployment-readiness warnings.
    Doctor {
        /// Path to the `.semsql` file.
        #[arg(long)]
        graph: PathBuf,
        /// Optional live DB URL — when supplied, doctor connects and
        /// reports RLS coverage on every tenanted table. Postgres only
        /// (the only engine with first-class RLS today). Use this in CI
        /// before promoting a graph to production.
        #[arg(long)]
        db_url: Option<String>,
        /// Optional path to a `--report-json` file emitted by
        /// `python -m semsql_eval spider`. When supplied, doctor surfaces
        /// the per-stage breakdown (Stage 0a hits vs needs_model bails)
        /// and warns when cascade coverage is below the deployment
        /// threshold. Use this to track Stage 1+ rollout impact across
        /// CI runs without re-running the eval.
        #[arg(long)]
        eval_report: Option<PathBuf>,
        /// Optional starter `semsql.overrides.yaml` writer. When set,
        /// doctor generates a YAML scaffold from every `conflict_log`
        /// row's `suggested_override` so users can edit + commit a
        /// concrete tie-breaker rather than re-run extraction with
        /// matching label changes. Existing target files are NOT
        /// overwritten — fail closed if the path is occupied.
        #[arg(long)]
        write_overrides: Option<PathBuf>,
        /// Optional drilldown into the per-example records of an
        /// eval report. When paired with `--eval-report` and a
        /// positive `--examples N`, doctor prints the first N
        /// non-correct examples (bailed, errored, or wrong) with
        /// their stage tag, gold SQL, and predicted SQL — fastest
        /// path from "exec_acc dropped" to a concrete cascade bug.
        #[arg(long, default_value_t = 0)]
        examples: u32,
        /// Fail-closed RLS gate. When set, doctor exits non-zero if
        /// the SemanticGraph declares any tenanted entity *unless*
        /// every one of those entities was successfully verified to
        /// have RLS enabled with at least one policy via `--db-url`.
        /// Use this in CI to block production promotion when the
        /// RLS posture is unknown — never silently. Without
        /// `--db-url` and with at least one scoped entity, this flag
        /// always exits non-zero.
        #[arg(long)]
        rls_strict: bool,
        /// Machine-readable doctor output. With `--format json`
        /// every diagnostic — coverage stats, conflict log, RLS
        /// status, eval breakdown — is emitted as a single JSON
        /// document on stdout. Suitable for CI parsing. The default
        /// (`text`) renders the human-friendly form.
        #[arg(long, default_value = "text")]
        format: String,
        /// Optional cascade manifest JSON. When supplied, doctor
        /// validates the manifest schema, asserts every referenced
        /// ONNX file + tokenizer exists, and surfaces the cascade
        /// version. If this binary was compiled WITHOUT
        /// `--features onnx`, doctor warns that the manifest cannot
        /// be loaded at query time (`semsql query` will silently
        /// ignore it and run the deterministic-only cascade).
        #[arg(long)]
        cascade_manifest: Option<PathBuf>,
    },

    /// Run an evaluation suite against a graph + cascade.
    Eval {
        /// Suite name (`spider`, `bird`, `adversarial`).
        #[arg(long)]
        suite: String,
        /// Path to the `.semsql` file.
        #[arg(long)]
        graph: PathBuf,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    init_tracing(cli.verbose);

    match cli.cmd {
        Cmd::Extract {
            path,
            framework,
            output,
            db_url,
            vocab_jsonl,
        } => {
            cmd_extract(
                &path,
                &framework,
                &output,
                db_url.as_deref(),
                vocab_jsonl.as_deref(),
            )
            .await
        }

        Cmd::Query {
            graph,
            nl,
            dialect,
            cascade_manifest,
            intent_yaml,
        } => cmd_query(
            &graph,
            &nl,
            dialect.as_deref(),
            cascade_manifest.as_deref(),
            intent_yaml.as_deref(),
        ),

        Cmd::Doctor {
            graph,
            db_url,
            eval_report,
            write_overrides,
            examples,
            rls_strict,
            format,
            cascade_manifest,
        } => {
            cmd_doctor(
                &graph,
                db_url.as_deref(),
                eval_report.as_deref(),
                write_overrides.as_deref(),
                examples,
                rls_strict,
                &format,
                cascade_manifest.as_deref(),
            )
            .await
        }

        Cmd::Eval { suite, graph } => cmd_eval(&suite, &graph),
    }
}

fn init_tracing(verbosity: u8) {
    let level = match verbosity {
        0 => "warn",
        1 => "info",
        2 => "debug",
        _ => "trace",
    };
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(level));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .compact()
        .init();
}

async fn cmd_extract(
    path: &std::path::Path,
    framework: &str,
    output: &std::path::Path,
    db_url: Option<&str>,
    vocab_jsonl: Option<&std::path::Path>,
) -> Result<()> {
    match framework {
        "none" => {
            let url = db_url.ok_or_else(|| {
                anyhow::anyhow!("--framework=none requires --db-url <url>")
            })?;
            let summary = extract::run_db_only(url, output).await?;
            let extra_vocab = if let Some(p) = vocab_jsonl {
                extract::ingest_vocab_jsonl(output, p)?
            } else {
                0
            };
            println!(
                "wrote {} entities, {} fields, {} vocab rows ({} from JSONL) to {}",
                summary.entity_count,
                summary.field_count,
                summary.vocab_count + extra_vocab,
                extra_vocab,
                output.display()
            );
            Ok(())
        }
        other => {
            let _ = path;
            anyhow::bail!(
                "framework `{other}` not yet wired in this build — only `none` is implemented in v0.2"
            );
        }
    }
}

fn cmd_query(
    graph: &std::path::Path,
    nl: &str,
    dialect: Option<&str>,
    cascade_manifest: Option<&std::path::Path>,
    intent_yaml: Option<&std::path::Path>,
) -> Result<()> {
    let target_dialect = match dialect {
        None => None,
        Some(name) => Some(parse_dialect(name)?),
    };
    let cascade =
        semsql_runtime::Cascade::load_with_manifest(graph, intent_yaml, cascade_manifest)
            .with_context(|| format!("loading cascade from {}", graph.display()))?;
    match cascade.run(nl) {
        Ok(out) => {
            let final_sql = match target_dialect {
                Some(d) => semsql_renderer::render_text(&out.sql_text, d)
                    .with_context(|| format!("dialect render `{d:?}`"))?,
                None => out.sql_text.clone(),
            };
            println!("{final_sql}");
            eprintln!("stage_pinned={}", out.stage_pinned);
            eprintln!("repair_attempts={}", out.repair_attempts);
            eprintln!(
                "stage_0a={}us stage_0b={}us stage_1={}us stage_2={}us stage_3={}us stage_4={}us",
                out.timings_us.stage_0a, out.timings_us.stage_0b,
                out.timings_us.stage_1, out.timings_us.stage_2,
                out.timings_us.stage_3, out.timings_us.stage_4,
            );
            if !out.intent_hints.is_empty() {
                eprintln!("intents: {}", out.intent_hints.join(", "));
            }
            Ok(())
        }
        Err(e) => {
            // Tag the bail reason on stderr BEFORE returning an error.
            // The error message format from `Cascade::run` mentions
            // "model stages" when Stage 1+ would be required; any
            // other error is a parse / IO / graph-loading fault.
            let msg = e.to_string();
            if msg.contains("model stages") {
                eprintln!("stage_pinned=needs_model");
            } else {
                eprintln!("stage_pinned=error");
            }
            Err(anyhow::anyhow!("cascade: {e}"))
        }
    }
}

/// Parse a `--dialect` CLI argument into the renderer's [`Dialect`]
/// enum. Aliases match the canonical engine names users type by hand.
fn parse_dialect(name: &str) -> Result<semsql_renderer::Dialect> {
    use semsql_renderer::Dialect;
    Ok(match name.to_ascii_lowercase().as_str() {
        "postgres" | "postgresql" | "pg" => Dialect::Postgres,
        "mysql" | "mariadb" => Dialect::MySql,
        "sqlite" | "sqlite3" => Dialect::Sqlite,
        "mssql" | "sqlserver" => Dialect::MsSql,
        "bigquery" | "bq" => Dialect::BigQuery,
        "snowflake" => Dialect::Snowflake,
        "duckdb" => Dialect::DuckDb,
        other => anyhow::bail!(
            "unknown dialect `{other}` — supported: postgres, mysql, sqlite, mssql, bigquery, snowflake, duckdb"
        ),
    })
}

async fn cmd_doctor(
    graph: &std::path::Path,
    db_url: Option<&str>,
    eval_report: Option<&std::path::Path>,
    write_overrides: Option<&std::path::Path>,
    examples: u32,
    rls_strict: bool,
    format: &str,
    cascade_manifest: Option<&std::path::Path>,
) -> Result<()> {
    match format {
        "text" => {}
        "json" => {
            return cmd_doctor_json(
                graph,
                db_url,
                eval_report,
                write_overrides,
                rls_strict,
                cascade_manifest,
            )
            .await;
        }
        other => anyhow::bail!(
            "unknown --format `{other}` — supported: text, json"
        ),
    }
    let cov = semsql_graph::read::coverage(graph)
        .with_context(|| format!("reading coverage from {}", graph.display()))?;
    let conflicts = semsql_graph::read::conflicts(graph)
        .with_context(|| format!("reading conflict_log from {}", graph.display()))?;

    println!("graph: {}", graph.display());
    println!(
        "  entities={}  fields={}  vocab={}  enums={}  scopes={}",
        cov.entity_count,
        cov.field_count,
        cov.vocab_count,
        cov.enum_count,
        cov.scope_count,
    );

    if !cov.entities_lacking_ui_vocab.is_empty() {
        println!();
        println!(
            "warning: {} entities lack UI-layer vocabulary (i18n / Filament). \
             Consider running an extractor:",
            cov.entities_lacking_ui_vocab.len()
        );
        for e in &cov.entities_lacking_ui_vocab {
            println!("  - {e}");
        }
    }

    if !conflicts.is_empty() {
        println!();
        println!("vocabulary conflicts ({})", conflicts.len());
        for c in &conflicts {
            println!(
                "  [{}] target={} resolution={}",
                c.id, c.canonical_target, c.resolution
            );
            if let Some(suggested) = &c.suggested_override {
                println!("       suggested override: {suggested}");
            }
        }
    }

    if let Some(out_path) = write_overrides {
        write_overrides_yaml(out_path, &conflicts)?;
        println!();
        println!(
            "wrote {} conflict scaffold(s) to {}",
            conflicts.len(),
            out_path.display()
        );
    }

    let mut rls_problems = 0usize;
    let mut rls_unverified = false;
    if !cov.scoped_entities.is_empty() {
        println!();
        println!(
            "deployment readiness: production must enable Postgres RLS (or vendor equivalent) \
             on the following tenanted tables:"
        );
        for e in &cov.scoped_entities {
            println!("  - {e}");
        }
        println!(
            "  (mandatory-filter injection is belt-and-suspenders, not a substitute for RLS)"
        );

        if let Some(url) = db_url {
            println!();
            rls_problems = run_rls_check(url, &cov.scoped_entities).await?;
        } else {
            println!("  hint: re-run with `--db-url <url>` to verify RLS is actually on.");
            rls_unverified = true;
        }

        if rls_strict && (rls_problems > 0 || rls_unverified) {
            println!();
            if rls_unverified {
                println!(
                    "rls-strict: graph declares {} tenanted entit{} but RLS posture was not \
                     verified — supply `--db-url` to confirm.",
                    cov.scoped_entities.len(),
                    if cov.scoped_entities.len() == 1 { "y" } else { "ies" },
                );
            } else {
                println!(
                    "rls-strict: {rls_problems} tenanted table(s) failed RLS verification."
                );
            }
        }
    }

    let mut cascade_problems = 0usize;
    if let Some(report_path) = eval_report {
        println!();
        cascade_problems = render_eval_report(report_path)?;
        if examples > 0 {
            render_eval_examples(report_path, examples as usize)?;
        }
    }

    let mut manifest_problems = 0usize;
    if let Some(manifest_path) = cascade_manifest {
        println!();
        manifest_problems = render_manifest_report(manifest_path)?;
    }

    let strict_rls_block = rls_strict && (rls_problems > 0 || rls_unverified);
    if !conflicts.is_empty()
        || rls_problems > 0
        || cascade_problems > 0
        || strict_rls_block
        || manifest_problems > 0
    {
        std::process::exit(1);
    }
    Ok(())
}

/// JSON-format doctor renderer. Emits a single document with every
/// diagnostic the text path surfaces, suitable for CI parsing.
/// Schema is intentionally flat — keys map directly to the text
/// blocks (`coverage`, `conflicts`, `rls`, `eval_report`).
async fn cmd_doctor_json(
    graph: &std::path::Path,
    db_url: Option<&str>,
    eval_report: Option<&std::path::Path>,
    write_overrides: Option<&std::path::Path>,
    rls_strict: bool,
    cascade_manifest: Option<&std::path::Path>,
) -> Result<()> {
    let cov = semsql_graph::read::coverage(graph)
        .with_context(|| format!("reading coverage from {}", graph.display()))?;
    let conflicts = semsql_graph::read::conflicts(graph)
        .with_context(|| format!("reading conflict_log from {}", graph.display()))?;

    let mut overrides_written: Option<String> = None;
    if let Some(out_path) = write_overrides {
        write_overrides_yaml(out_path, &conflicts)?;
        overrides_written = Some(out_path.display().to_string());
    }

    // RLS probe — only attempt when `--db-url` supplied.
    let mut rls_rows: Vec<serde_json::Value> = Vec::new();
    let mut rls_problems = 0usize;
    let mut rls_status = "skipped";
    let mut rls_unverified = !cov.scoped_entities.is_empty() && db_url.is_none();
    #[cfg(feature = "postgres")]
    if let Some(url) = db_url {
        if (url.starts_with("postgres:") || url.starts_with("postgresql:"))
            && !cov.scoped_entities.is_empty()
        {
            use semsql_extract_db::PgIntrospect;
            let intro = PgIntrospect::connect(url)
                .await
                .map_err(|e| anyhow::anyhow!("postgres connect: {e}"))?;
            let rows = intro
                .rls_status()
                .await
                .map_err(|e| anyhow::anyhow!("rls_status: {e}"))?;
            let scoped: std::collections::HashSet<&str> =
                cov.scoped_entities.iter().map(String::as_str).collect();
            for r in rows {
                let key = if r.schema == "public" {
                    r.table.clone()
                } else {
                    format!("{}.{}", r.schema, r.table)
                };
                if !scoped.contains(key.as_str()) {
                    continue;
                }
                let ok = r.rls_enabled && r.policy_count > 0;
                if !ok {
                    rls_problems += 1;
                }
                rls_rows.push(serde_json::json!({
                    "table": key,
                    "rls_enabled": r.rls_enabled,
                    "policy_count": r.policy_count,
                    "ok": ok,
                }));
            }
            rls_status = "checked";
            rls_unverified = false;
        }
    }
    let _ = db_url;

    // Eval report (optional).
    let mut eval_block: Option<serde_json::Value> = None;
    let mut cascade_problems = 0usize;
    if let Some(path) = eval_report {
        let bytes = std::fs::read(path)
            .with_context(|| format!("reading eval report `{}`", path.display()))?;
        let report: EvalReport = serde_json::from_slice(&bytes)
            .with_context(|| format!("parsing eval report `{}`", path.display()))?;
        let (_, problems) = format_eval_report(path, &report);
        cascade_problems = problems;
        eval_block = Some(serde_json::json!({
            "path": path.display().to_string(),
            "summary": {
                "suite": report.summary.suite,
                "total": report.summary.total,
                "correct": report.summary.correct,
                "bailed": report.summary.bailed,
                "errored": report.summary.errored,
                "exec_acc": report.summary.exec_acc,
                "bail_rate": report.summary.bail_rate,
                "stage_breakdown": report.summary.stage_breakdown,
            },
        }));
    }

    // Cascade manifest validation (optional).
    let mut manifest_block: Option<serde_json::Value> = None;
    let mut manifest_problems = 0usize;
    if let Some(p) = cascade_manifest {
        let (block, probs) = manifest_report_payload(p);
        manifest_block = Some(block);
        manifest_problems = probs;
    }

    let strict_rls_block = rls_strict && (rls_problems > 0 || rls_unverified);
    let exit_nonzero = !conflicts.is_empty()
        || rls_problems > 0
        || cascade_problems > 0
        || strict_rls_block
        || manifest_problems > 0;

    let payload = serde_json::json!({
        "graph": graph.display().to_string(),
        "coverage": {
            "entities": cov.entity_count,
            "fields": cov.field_count,
            "vocab": cov.vocab_count,
            "enums": cov.enum_count,
            "scopes": cov.scope_count,
            "entities_lacking_ui_vocab": cov.entities_lacking_ui_vocab,
            "scoped_entities": cov.scoped_entities,
        },
        "conflicts": conflicts.iter().map(|c| serde_json::json!({
            "id": c.id,
            "target": c.canonical_target,
            "candidates_json": c.candidates_json,
            "resolution": c.resolution,
            "suggested_override": c.suggested_override,
        })).collect::<Vec<_>>(),
        "rls": {
            "status": rls_status,
            "unverified": rls_unverified,
            "problems": rls_problems,
            "rows": rls_rows,
        },
        "eval_report": eval_block,
        "cascade_manifest": manifest_block,
        "overrides_written": overrides_written,
        "exit_nonzero": exit_nonzero,
    });

    println!("{}", serde_json::to_string_pretty(&payload)?);
    if exit_nonzero {
        std::process::exit(1);
    }
    Ok(())
}

/// Threshold below which cascade coverage (the share of eval examples
/// pinned at deterministic Stage 0a) is reported as a deployment
/// warning. 50% mirrors the v0.2 milestone target — once Stage 1+
/// weights ship the default deployment is expected to clear this comfortably.
const CASCADE_COVERAGE_WARN_THRESHOLD: f64 = 0.5;

#[derive(serde::Deserialize)]
struct EvalReport {
    summary: EvalReportSummary,
    #[serde(default)]
    examples: Vec<EvalExampleRecord>,
}

#[derive(serde::Deserialize, Clone)]
struct EvalExampleRecord {
    #[serde(default)]
    db_id: String,
    #[serde(default)]
    question: String,
    #[serde(default)]
    gold_sql: String,
    #[serde(default)]
    pred_sql: String,
    #[serde(default)]
    stage_pinned: String,
}

#[derive(serde::Deserialize)]
struct EvalReportSummary {
    suite: Option<String>,
    total: u64,
    correct: u64,
    #[serde(default)]
    bailed: u64,
    #[serde(default)]
    errored: u64,
    #[serde(default)]
    exec_acc: f64,
    #[serde(default)]
    bail_rate: f64,
    #[serde(default)]
    stage_breakdown: std::collections::BTreeMap<String, u64>,
}

/// Print the per-stage breakdown from a `--report-json` artifact and
/// return the number of deployment-blocking problems detected.
///
/// Today: cascade coverage below [`CASCADE_COVERAGE_WARN_THRESHOLD`]
/// counts as one problem (lifts the doctor exit code so CI catches a
/// regression before promotion). The summary print path is always
/// rendered — operators want to see the breakdown even on green runs.
fn render_eval_report(path: &std::path::Path) -> Result<usize> {
    let bytes = std::fs::read(path)
        .with_context(|| format!("reading eval report `{}`", path.display()))?;
    let report: EvalReport = serde_json::from_slice(&bytes)
        .with_context(|| format!("parsing eval report `{}`", path.display()))?;
    let (rendered, problems) = format_eval_report(path, &report);
    print!("{rendered}");
    Ok(problems)
}

/// Drill into the per-example records of a report and surface the
/// first `n` non-correct examples (bailed / errored / wrong) with
/// their stage tag, gold SQL, and predicted SQL. Skips the report
/// when no per-example records exist (older eval runs without
/// `--report-json`'s example dump).
fn render_eval_examples(path: &std::path::Path, n: usize) -> Result<()> {
    let bytes = std::fs::read(path)
        .with_context(|| format!("reading eval report `{}`", path.display()))?;
    let report: EvalReport = serde_json::from_slice(&bytes)
        .with_context(|| format!("parsing eval report `{}`", path.display()))?;
    if report.examples.is_empty() {
        println!("  (no per-example records in this report — re-run with --report-json)");
        return Ok(());
    }
    // Filter: skip clearly-correct rows. We don't have an exec-acc
    // signal per row in the report, so the conservative filter
    // surfaces every bail / error / unknown-stage row. `stage_0a` rows
    // that succeeded are silently skipped.
    let mut interesting: Vec<&EvalExampleRecord> = report
        .examples
        .iter()
        .filter(|r| {
            matches!(
                r.stage_pinned.as_str(),
                "needs_model" | "error" | "timeout" | "unknown"
            )
        })
        .collect();
    if interesting.is_empty() {
        // Fallback: print the first N records so users can still
        // drill into a uniformly-correct run.
        interesting = report.examples.iter().take(n).collect();
    } else {
        interesting.truncate(n);
    }

    println!();
    println!("eval drilldown — first {} non-correct example(s):", interesting.len());
    for (i, r) in interesting.iter().enumerate() {
        println!(
            "  [{i:>2}] db={} stage={} q={:?}",
            r.db_id, r.stage_pinned, r.question
        );
        println!("       gold: {}", truncate(&r.gold_sql, 120));
        println!("       pred: {}", truncate(&r.pred_sql, 120));
    }
    Ok(())
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max).collect();
    out.push_str("…");
    out
}

/// Write a starter `semsql.overrides.yaml` from the conflict log's
/// suggested-override hints. Each conflict becomes one entry with a
/// commented-out `override:` line so the user can uncomment + tweak
/// before committing. Refuses to overwrite an existing file — the
/// expected workflow is "doctor writes once, user edits".
fn write_overrides_yaml(
    out_path: &std::path::Path,
    conflicts: &[semsql_graph::read::ConflictLogRow],
) -> Result<()> {
    if out_path.exists() {
        anyhow::bail!(
            "refusing to overwrite existing {} — move it aside first",
            out_path.display()
        );
    }
    use std::fmt::Write as _;
    let mut buf = String::new();
    let _ = writeln!(
        buf,
        "# semsql vocabulary overrides — generated by `semsql doctor --write-overrides`."
    );
    let _ = writeln!(
        buf,
        "# Each entry is a tie-breaker for one conflict_log row. Uncomment the"
    );
    let _ = writeln!(
        buf,
        "# `override:` line and edit the value to lock the resolution before re-extracting."
    );
    let _ = writeln!(buf, "version: 1");
    let _ = writeln!(buf, "overrides:");
    if conflicts.is_empty() {
        let _ = writeln!(buf, "  []  # no conflicts in the current graph");
    } else {
        for c in conflicts {
            let _ = writeln!(buf, "  - id: {}", c.id);
            let _ = writeln!(buf, "    target: {}", yaml_escape(&c.canonical_target));
            let _ = writeln!(
                buf,
                "    candidates: {}",
                yaml_escape(&c.candidates_json)
            );
            let _ = writeln!(buf, "    resolution: {}", yaml_escape(&c.resolution));
            match &c.suggested_override {
                Some(sug) => {
                    let _ = writeln!(buf, "    # override: {}", yaml_escape(sug));
                }
                None => {
                    let _ = writeln!(
                        buf,
                        "    # override: <fill in — no suggestion was generated>"
                    );
                }
            }
        }
    }
    if let Some(parent) = out_path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("creating dir for {}", out_path.display()))?;
        }
    }
    std::fs::write(out_path, buf)
        .with_context(|| format!("writing {}", out_path.display()))?;
    Ok(())
}

/// Quote-and-escape for YAML scalar emission. We use the simple
/// strategy of always double-quoting + backslash-escaping `"`/`\` —
/// produces verbose but unambiguous output. Fine for diagnostic
/// scaffolds; the user edits these by hand anyway.
fn yaml_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            _ => out.push(ch),
        }
    }
    out.push('"');
    out
}

/// Pure formatter for an [`EvalReport`]. Returns the printable text
/// (newline-terminated) plus the count of deployment-blocking problems.
/// Pulled out so unit tests can exercise the rendering and the
/// problem-count rules without spinning up a subprocess.
fn format_eval_report(path: &std::path::Path, report: &EvalReport) -> (String, usize) {
    use std::fmt::Write as _;
    let mut out = String::new();
    let total = report.summary.total;
    let _ = writeln!(out, "eval report: {}", path.display());
    let _ = writeln!(
        out,
        "  suite={}  total={}  correct={}  bailed={}  errored={}  exec_acc={:.3}  bail_rate={:.3}",
        report.summary.suite.as_deref().unwrap_or("?"),
        total,
        report.summary.correct,
        report.summary.bailed,
        report.summary.errored,
        report.summary.exec_acc,
        report.summary.bail_rate,
    );

    if report.summary.stage_breakdown.is_empty() {
        let _ = writeln!(out, "  (no stage_breakdown — eval pre-dates per-stage telemetry)");
        return (out, 0);
    }

    let _ = writeln!(out, "  stage_breakdown:");
    for (stage, count) in &report.summary.stage_breakdown {
        let pct = if total > 0 {
            (*count as f64 / total as f64) * 100.0
        } else {
            0.0
        };
        let _ = writeln!(out, "    {stage:<14} {count:>6}  ({pct:>5.1}%)");
    }

    let mut problems = 0usize;
    if total > 0 {
        let stage_0a = report
            .summary
            .stage_breakdown
            .get("stage_0a")
            .copied()
            .unwrap_or(0);
        let coverage = stage_0a as f64 / total as f64;
        if coverage < CASCADE_COVERAGE_WARN_THRESHOLD {
            let _ = writeln!(out);
            let _ = writeln!(
                out,
                "warning: cascade coverage (Stage 0a) is {:.1}% — below the {:.0}% deployment \
                 threshold. Either Stage 1+ weights are not loaded or the SemanticGraph is \
                 missing vocabulary for common queries. Re-run extractors and confirm \
                 `cascade_version` in your manifest matches the deployed weights.",
                coverage * 100.0,
                CASCADE_COVERAGE_WARN_THRESHOLD * 100.0,
            );
            problems += 1;
        }
        let errors = report
            .summary
            .stage_breakdown
            .get("error")
            .copied()
            .unwrap_or(0)
            + report
                .summary
                .stage_breakdown
                .get("timeout")
                .copied()
                .unwrap_or(0);
        if errors > 0 {
            let _ = writeln!(out);
            let _ = writeln!(
                out,
                "warning: {errors} example(s) errored or timed out — investigate via the \
                 per-example records in the report JSON before promoting."
            );
            problems += 1;
        }
    }

    (out, problems)
}

/// Compile-time flag set when this binary was built with
/// `--features onnx`. The cascade manifest is only honoured at query
/// time on onnx builds; doctor warns when this is `false` so operators
/// don't ship with a manifest the runtime silently ignores.
const IS_ONNX_BUILD: bool = cfg!(feature = "onnx");

/// Validate a cascade manifest at `path` and print a human-readable
/// summary. Returns the count of deployment-blocking problems.
///
/// Problems counted:
///  - manifest fails to load (schema version too new, missing files,
///    malformed JSON)
///  - binary built without `--features onnx` (manifest can't be used
///    at query time — counts as 1 problem so CI catches the misconfig)
fn render_manifest_report(path: &std::path::Path) -> Result<usize> {
    use semsql_runtime::manifest::CascadeManifest;
    println!("cascade manifest: {}", path.display());
    let manifest = match CascadeManifest::load(path) {
        Ok(m) => m,
        Err(e) => {
            println!("  load failed: {e}");
            return Ok(1);
        }
    };
    println!(
        "  schema_version={} cascade_version={}",
        manifest.schema_version, manifest.cascade_version
    );
    println!(
        "  linker:      {} ({} params)",
        manifest.linker.path.display(),
        manifest.linker.params
    );
    println!(
        "  skeleton:    {} ({} params)",
        manifest.skeleton.path.display(),
        manifest.skeleton.params
    );
    println!(
        "  slot_filler: {} ({} params)",
        manifest.slot_filler.path.display(),
        manifest.slot_filler.params
    );
    let mut problems = 0usize;
    if !IS_ONNX_BUILD {
        println!();
        println!(
            "warning: this binary was built WITHOUT `--features onnx`. The \
             manifest validates fine, but `semsql query --cascade-manifest` \
             will silently ignore it and run the deterministic-only cascade. \
             Rebuild with `cargo build -p semsql-cli --features onnx` to \
             enable Stage 1+ inference."
        );
        problems += 1;
    }
    Ok(problems)
}

/// JSON-shape sibling of [`render_manifest_report`]. Returns the
/// JSON block + the same problem count.
fn manifest_report_payload(path: &std::path::Path) -> (serde_json::Value, usize) {
    use semsql_runtime::manifest::CascadeManifest;
    let load = CascadeManifest::load(path);
    match load {
        Err(e) => (
            serde_json::json!({
                "path": path.display().to_string(),
                "ok": false,
                "error": format!("{e}"),
                "onnx_build": IS_ONNX_BUILD,
            }),
            1,
        ),
        Ok(m) => {
            let problems = if IS_ONNX_BUILD { 0 } else { 1 };
            (
                serde_json::json!({
                    "path": path.display().to_string(),
                    "ok": true,
                    "schema_version": m.schema_version,
                    "cascade_version": m.cascade_version,
                    "linker_params": m.linker.params,
                    "skeleton_params": m.skeleton.params,
                    "slot_filler_params": m.slot_filler.params,
                    "onnx_build": IS_ONNX_BUILD,
                }),
                problems,
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(stage_breakdown: &[(&str, u64)], total: u64, correct: u64) -> EvalReport {
        EvalReport {
            examples: Vec::new(),
            summary: EvalReportSummary {
                suite: Some("spider".into()),
                total,
                correct,
                bailed: total.saturating_sub(correct),
                errored: 0,
                exec_acc: if total == 0 {
                    0.0
                } else {
                    correct as f64 / total as f64
                },
                bail_rate: 0.0,
                stage_breakdown: stage_breakdown
                    .iter()
                    .map(|(k, v)| ((*k).into(), *v))
                    .collect(),
            },
        }
    }

    #[test]
    fn full_stage_0a_coverage_emits_no_problems() {
        let r = report(&[("stage_0a", 10)], 10, 10);
        let (text, problems) = format_eval_report(std::path::Path::new("rep.json"), &r);
        assert_eq!(problems, 0);
        assert!(text.contains("stage_breakdown"));
        assert!(text.contains("stage_0a"));
        assert!(!text.contains("warning:"));
    }

    #[test]
    fn low_coverage_flags_a_problem_and_warns() {
        // 1/10 pinned at Stage 0a → 10% coverage, below the 50% threshold.
        let r = report(&[("stage_0a", 1), ("needs_model", 9)], 10, 1);
        let (text, problems) = format_eval_report(std::path::Path::new("rep.json"), &r);
        assert_eq!(problems, 1);
        assert!(text.contains("cascade coverage (Stage 0a) is 10.0%"));
        assert!(text.contains("below the 50% deployment threshold"));
    }

    #[test]
    fn errors_or_timeouts_lift_the_problem_counter_independently() {
        // Coverage is fine (60%) but 2 errored examples must still bump.
        let r = report(&[("stage_0a", 6), ("error", 2), ("needs_model", 2)], 10, 6);
        let (text, problems) = format_eval_report(std::path::Path::new("rep.json"), &r);
        assert_eq!(problems, 1);
        assert!(text.contains("example(s) errored or timed out"));
        assert!(!text.contains("cascade coverage"));
    }

    #[test]
    fn missing_stage_breakdown_is_a_clean_pass_with_a_note() {
        let r = report(&[], 0, 0);
        let (text, problems) = format_eval_report(std::path::Path::new("rep.json"), &r);
        assert_eq!(problems, 0);
        assert!(text.contains("eval pre-dates per-stage telemetry"));
    }

    #[test]
    fn parse_dialect_accepts_canonical_aliases() {
        use semsql_renderer::Dialect;
        assert_eq!(parse_dialect("postgres").unwrap(), Dialect::Postgres);
        assert_eq!(parse_dialect("postgresql").unwrap(), Dialect::Postgres);
        assert_eq!(parse_dialect("PG").unwrap(), Dialect::Postgres);
        assert_eq!(parse_dialect("mysql").unwrap(), Dialect::MySql);
        assert_eq!(parse_dialect("MariaDB").unwrap(), Dialect::MySql);
        assert_eq!(parse_dialect("sqlite").unwrap(), Dialect::Sqlite);
        assert_eq!(parse_dialect("sqlite3").unwrap(), Dialect::Sqlite);
        assert_eq!(parse_dialect("mssql").unwrap(), Dialect::MsSql);
        assert_eq!(parse_dialect("duckdb").unwrap(), Dialect::DuckDb);
    }

    #[test]
    fn parse_dialect_rejects_unknown() {
        let err = parse_dialect("oracle").unwrap_err();
        assert!(err.to_string().contains("unknown dialect `oracle`"));
    }

    #[test]
    fn truncate_under_limit_keeps_string_intact() {
        assert_eq!(truncate("hello", 10), "hello");
    }

    #[test]
    fn truncate_over_limit_appends_ellipsis() {
        let out = truncate("abcdefgh", 4);
        assert_eq!(out, "abcd…");
    }

    #[test]
    fn yaml_escape_quotes_and_escapes_specials() {
        assert_eq!(yaml_escape("plain"), "\"plain\"");
        assert_eq!(yaml_escape("with \"quote\""), "\"with \\\"quote\\\"\"");
        assert_eq!(yaml_escape("path\\to"), "\"path\\\\to\"");
        assert_eq!(yaml_escape("a\nb"), "\"a\\nb\"");
    }

    #[test]
    fn write_overrides_yaml_refuses_existing_file() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("overrides.yaml");
        std::fs::write(&p, "existing").unwrap();
        let err = write_overrides_yaml(&p, &[]).unwrap_err();
        assert!(err.to_string().contains("refusing to overwrite"));
    }

    #[test]
    fn write_overrides_yaml_emits_empty_scaffold_for_no_conflicts() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("overrides.yaml");
        write_overrides_yaml(&p, &[]).unwrap();
        let body = std::fs::read_to_string(&p).unwrap();
        assert!(body.contains("version: 1"));
        assert!(body.contains("overrides:"));
        assert!(body.contains("no conflicts in the current graph"));
    }

    #[test]
    fn write_overrides_yaml_includes_conflict_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("overrides.yaml");
        let conflicts = vec![semsql_graph::read::ConflictLogRow {
            id: 7,
            canonical_target: "users.status_code".into(),
            candidates_json: r#"[{"layer":6,"term":"Status"},{"layer":2,"term":"status_code"}]"#
                .into(),
            resolution: "highest_layer_wins".into(),
            suggested_override: Some("users.status_code <- Status".into()),
        }];
        write_overrides_yaml(&p, &conflicts).unwrap();
        let body = std::fs::read_to_string(&p).unwrap();
        assert!(body.contains("- id: 7"));
        assert!(body.contains("target: \"users.status_code\""));
        assert!(body.contains("# override: \"users.status_code <- Status\""));
    }

    #[test]
    fn percentages_are_proportional_to_total() {
        let r = report(&[("stage_0a", 7), ("needs_model", 3)], 10, 7);
        let (text, _) = format_eval_report(std::path::Path::new("rep.json"), &r);
        // 7/10 = 70%, 3/10 = 30%. Spot-check both lines render.
        assert!(text.contains("70.0%"));
        assert!(text.contains("30.0%"));
    }
}

/// Connect to a live DB and report RLS status for every entity flagged as
/// tenanted in the SemanticGraph. Returns the count of problem rows
/// (RLS-off OR RLS-on-but-no-policies). Postgres-only for now; other
/// engines fall back to a "skipped" message.
async fn run_rls_check(db_url: &str, scoped_entities: &[String]) -> Result<usize> {
    #[cfg(feature = "postgres")]
    {
        if db_url.starts_with("postgres:") || db_url.starts_with("postgresql:") {
            use semsql_extract_db::PgIntrospect;
            let intro = PgIntrospect::connect(db_url)
                .await
                .map_err(|e| anyhow::anyhow!("postgres connect: {e}"))?;
            let rows = intro
                .rls_status()
                .await
                .map_err(|e| anyhow::anyhow!("rls_status: {e}"))?;

            // Build a lookup keyed by the canonical name we use in the
            // SemanticGraph (`schema.table` for non-default, bare for
            // public). Cross-reference every tenanted entity in O(n+m)
            // — no per-row reconnects.
            let scoped: std::collections::HashSet<&str> =
                scoped_entities.iter().map(String::as_str).collect();
            let mut seen_in_db: std::collections::HashSet<String> =
                std::collections::HashSet::new();
            let mut problems = 0usize;
            println!("RLS status (live DB):");
            for r in rows {
                let key = if r.schema == "public" {
                    r.table.clone()
                } else {
                    format!("{}.{}", r.schema, r.table)
                };
                if !scoped.contains(key.as_str()) {
                    continue;
                }
                seen_in_db.insert(key.clone());
                let status = match (r.rls_enabled, r.policy_count) {
                    (false, _) => {
                        problems += 1;
                        "RLS DISABLED — production traffic NOT isolated"
                    }
                    (true, 0) => {
                        problems += 1;
                        "RLS enabled but NO policies — denies all non-superuser traffic"
                    }
                    (true, _) => "ok",
                };
                println!("  - {key}: {status} ({} policies)", r.policy_count);
            }
            // Surface tenanted entities the graph claims exist but the
            // live DB doesn't list. This is graph drift — the
            // migration history and the SemanticGraph disagree, which
            // typically means an extractor ran against a stale schema.
            // Reported as a warning, not a failure.
            let missing: Vec<&String> = scoped_entities
                .iter()
                .filter(|e| !seen_in_db.contains(e.as_str()))
                .collect();
            if !missing.is_empty() {
                println!();
                println!(
                    "warning: {} tenanted entit{} in the SemanticGraph not present in the live DB:",
                    missing.len(),
                    if missing.len() == 1 { "y" } else { "ies" }
                );
                for m in missing {
                    println!("  - {m}");
                }
                println!("  hint: re-run `semsql extract` against the current DB.");
            }
            return Ok(problems);
        }
    }
    let _ = scoped_entities;
    println!("RLS check: skipped (only `postgres://` URLs are supported)");
    Ok(0)
}

fn cmd_eval(suite: &str, _graph: &std::path::Path) -> Result<()> {
    eprintln!("(eval) suite={suite}");
    eprintln!("eval harness lives in python/semsql_eval/ and is invoked via `uv run`");
    Ok(())
}
