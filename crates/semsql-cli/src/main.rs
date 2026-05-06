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

        Cmd::Query { graph, nl } => cmd_query(&graph, &nl),

        Cmd::Doctor {
            graph,
            db_url,
            eval_report,
        } => cmd_doctor(&graph, db_url.as_deref(), eval_report.as_deref()).await,

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

fn cmd_query(graph: &std::path::Path, nl: &str) -> Result<()> {
    let cascade = semsql_runtime::Cascade::load(graph, None)
        .with_context(|| format!("loading cascade from {}", graph.display()))?;
    match cascade.run(nl) {
        Ok(out) => {
            println!("{}", out.sql_text);
            // Tag the stage that pinned this query so downstream eval
            // tooling (`semsql_eval.cascade_runner`) can bin examples
            // by which stage they exited at. Today only Stage 0a /
            // Stage 4 reach the success path; once Stage 1+ models
            // ship, the runtime will emit stage_1/stage_2/stage_3
            // here too.
            eprintln!("stage_pinned=stage_0a");
            eprintln!(
                "stage_0a={}us stage_0b={}us stage_4={}us",
                out.timings_us.stage_0a, out.timings_us.stage_0b, out.timings_us.stage_4
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

async fn cmd_doctor(
    graph: &std::path::Path,
    db_url: Option<&str>,
    eval_report: Option<&std::path::Path>,
) -> Result<()> {
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

    let mut rls_problems = 0usize;
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
        }
    }

    let mut cascade_problems = 0usize;
    if let Some(report_path) = eval_report {
        println!();
        cascade_problems = render_eval_report(report_path)?;
    }

    if !conflicts.is_empty() || rls_problems > 0 || cascade_problems > 0 {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn report(stage_breakdown: &[(&str, u64)], total: u64, correct: u64) -> EvalReport {
        EvalReport {
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
