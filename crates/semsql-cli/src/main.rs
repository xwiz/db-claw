//! `semsql` — top-level command-line driver.

#![forbid(unsafe_code)]
#![allow(clippy::items_after_test_module)]

mod extract;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{OsStr, OsString};
use std::path::{Path, PathBuf};
use std::process::Command;

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
        /// Framework hint. Use `none` for DB-only extraction. Use
        /// `auto`, `laravel`, `rails`, `django`, `nextjs`, or `vue` to
        /// enrich the DB graph with source-level vocabulary via
        /// `semsql-extract`.
        #[arg(long, default_value = "none")]
        framework: String,
        /// Output `.semsql` file.
        #[arg(short, long)]
        output: PathBuf,
        /// DB URL. Required for all extraction modes; framework adapters add
        /// vocabulary to this DB-grounded graph.
        #[arg(long)]
        db_url: Option<String>,
        /// Optional JSONL file of vocabulary fragments emitted by a
        /// TypeScript extractor. One VocabFragment per line.
        #[arg(long)]
        vocab_jsonl: Option<PathBuf>,
        /// Disable DB value sampling. Use this for high-risk production DBs
        /// when schema/relationship probing is enough.
        #[arg(long = "no-sample-values", action = clap::ArgAction::SetFalse, default_value_t = true)]
        sample_values: bool,
        /// Optional directory containing table/column description CSVs.
        /// When omitted, `extract` auto-detects `database_description/`
        /// under the project path.
        #[arg(long)]
        schema_description_dir: Option<PathBuf>,
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
        /// fall through Stage 0a are routed through the model-backed
        /// cascade. Missing model artifacts surface a clear error
        /// rather than guessing. Without `--features onnx`, this flag
        /// is silently ignored and the deterministic-only cascade is used.
        #[arg(long)]
        cascade_manifest: Option<PathBuf>,
        /// Optional intent pattern YAML to load alongside the
        /// graph. When omitted, Stage 0b's intent matcher is empty
        /// (queries like `top 5 spenders` need either an explicit
        /// `by <field>` tail or an intent library to resolve at
        /// Stage 0a).
        #[arg(long)]
        intent_yaml: Option<PathBuf>,
        /// Diagnostic-only Stage 2 oracle skeleton override.
        #[arg(long, hide = true)]
        oracle_skeleton: Option<String>,
        /// Diagnostic-only Stage 1 oracle schema slice as compact JSON.
        #[arg(long, hide = true)]
        oracle_schema_json: Option<String>,
        /// Diagnostic-only Stage 3 oracle slot map as compact JSON.
        #[arg(long, hide = true)]
        oracle_slots_json: Option<String>,
        /// Diagnostic-only query-frame JSON output path.
        #[arg(long, hide = true)]
        query_frame_json: Option<PathBuf>,
        /// Optional rejected-query packet output path. Written only when local
        /// routing fails closed; successful local routes do not produce a
        /// fallback packet.
        #[arg(long)]
        rejection_packet_json: Option<PathBuf>,
        /// Include non-redacted graph sample values in
        /// `--rejection-packet-json`. Omitted by default for production
        /// safety.
        #[arg(long)]
        rejection_include_samples: bool,
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
            sample_values,
            schema_description_dir,
        } => {
            cmd_extract(
                &path,
                &framework,
                &output,
                db_url.as_deref(),
                vocab_jsonl.as_deref(),
                sample_values,
                schema_description_dir.as_deref(),
            )
            .await
        }

        Cmd::Query {
            graph,
            nl,
            dialect,
            cascade_manifest,
            intent_yaml,
            oracle_skeleton,
            oracle_schema_json,
            oracle_slots_json,
            query_frame_json,
            rejection_packet_json,
            rejection_include_samples,
        } => cmd_query(QueryArgs {
            graph: &graph,
            nl: &nl,
            dialect: dialect.as_deref(),
            cascade_manifest: cascade_manifest.as_deref(),
            intent_yaml: intent_yaml.as_deref(),
            oracle_skeleton: oracle_skeleton.as_deref(),
            oracle_schema_json: oracle_schema_json.as_deref(),
            oracle_slots_json: oracle_slots_json.as_deref(),
            query_frame_json: query_frame_json.as_deref(),
            rejection_packet_json: rejection_packet_json.as_deref(),
            rejection_include_samples,
        }),

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
    sample_values: bool,
    schema_description_dir: Option<&std::path::Path>,
) -> Result<()> {
    cmd_extract_with_source_runner(
        ExtractCommandOptions {
            path,
            framework,
            output,
            db_url,
            vocab_jsonl,
            sample_values,
            schema_description_dir,
        },
        run_framework_extractor_jsonl,
    )
    .await
}

struct ExtractCommandOptions<'a> {
    path: &'a std::path::Path,
    framework: &'a str,
    output: &'a std::path::Path,
    db_url: Option<&'a str>,
    vocab_jsonl: Option<&'a std::path::Path>,
    sample_values: bool,
    schema_description_dir: Option<&'a std::path::Path>,
}

async fn cmd_extract_with_source_runner(
    options: ExtractCommandOptions<'_>,
    run_source_extractor: impl FnOnce(&std::path::Path, &str, &std::path::Path) -> Result<usize>,
) -> Result<()> {
    let ExtractCommandOptions {
        path,
        framework,
        output,
        db_url,
        vocab_jsonl,
        sample_values,
        schema_description_dir,
    } = options;
    let framework = normalize_framework_name(framework);
    match framework.as_str() {
        "none" => {
            let url = db_url
                .ok_or_else(|| anyhow::anyhow!("--framework=none requires --db-url <url>"))?;
            let discovered_description_dir = schema_description_dir
                .map(std::path::Path::to_path_buf)
                .or_else(|| extract::discover_schema_description_dir(path));
            let summary = extract::run_db_only_with_options(
                url,
                output,
                extract::DbOnlyExtractOptions {
                    sample_values,
                    schema_description_dir: discovered_description_dir,
                },
            )
            .await?;
            let extra_vocab = if let Some(p) = vocab_jsonl {
                extract::ingest_vocab_jsonl(output, p)?
            } else {
                extract::JsonlIngestSummary::default()
            };
            println!(
                "wrote {} entities, {} fields, {} relationships, {} sample-value rows, {} vocab rows, {} metric definitions ({} schema-value predicates, {} vocab and {} metrics from JSONL) to {}",
                summary.entity_count,
                summary.field_count,
                summary.relationship_count,
                summary.sample_value_count,
                summary.vocab_count + extra_vocab.vocab_count,
                extra_vocab.metric_definition_count,
                summary.value_description_predicate_count,
                extra_vocab.vocab_count,
                extra_vocab.metric_definition_count,
                output.display()
            );
            Ok(())
        }
        other => {
            let url = db_url.ok_or_else(|| {
                anyhow::anyhow!(
                    "--framework={other} requires --db-url <url>; source evidence enriches a DB-grounded graph"
                )
            })?;
            let framework_vocab = temporary_framework_vocab_path(output);
            let source_vocab = match run_source_extractor(path, other, &framework_vocab) {
                Ok(count) => count,
                Err(err) => {
                    let _ = std::fs::remove_file(&framework_vocab);
                    return Err(err);
                }
            };
            let discovered_description_dir = schema_description_dir
                .map(std::path::Path::to_path_buf)
                .or_else(|| extract::discover_schema_description_dir(path));
            let summary = extract::run_db_only_with_options(
                url,
                output,
                extract::DbOnlyExtractOptions {
                    sample_values,
                    schema_description_dir: discovered_description_dir,
                },
            )
            .await?;
            let source_written = extract::ingest_vocab_jsonl(output, &framework_vocab)
                .with_context(|| format!("ingesting framework vocabulary for `{other}`"))?;
            let _ = std::fs::remove_file(&framework_vocab);
            let extra_vocab = if let Some(p) = vocab_jsonl {
                extract::ingest_vocab_jsonl(output, p)?
            } else {
                extract::JsonlIngestSummary::default()
            };
            println!(
                "wrote {} entities, {} fields, {} relationships, {} sample-value rows, {} vocab rows, {} metric definitions ({} schema-value predicates, {} vocab and {} metrics from framework `{}`, {} vocab and {} metrics from JSONL) to {}",
                summary.entity_count,
                summary.field_count,
                summary.relationship_count,
                summary.sample_value_count,
                summary.vocab_count + source_written.vocab_count + extra_vocab.vocab_count,
                source_written.metric_definition_count + extra_vocab.metric_definition_count,
                summary.value_description_predicate_count,
                source_written.vocab_count,
                source_written.metric_definition_count,
                other,
                extra_vocab.vocab_count,
                extra_vocab.metric_definition_count,
                output.display()
            );
            if source_vocab == 0 {
                eprintln!("warning: framework `{other}` produced no vocabulary fragments");
            }
            Ok(())
        }
    }
}

fn normalize_framework_name(framework: &str) -> String {
    match framework.trim().to_ascii_lowercase().as_str() {
        "next" | "next.js" | "next-js" => "nextjs".to_string(),
        other => other.to_string(),
    }
}

fn temporary_framework_vocab_path(output: &std::path::Path) -> PathBuf {
    let stem = output
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("graph")
        .chars()
        .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '_' })
        .collect::<String>();
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    std::env::temp_dir().join(format!(
        "semsql-framework-vocab-{}-{}-{stem}.jsonl",
        std::process::id(),
        nanos
    ))
}

fn run_framework_extractor_jsonl(
    project_path: &std::path::Path,
    framework: &str,
    output_jsonl: &std::path::Path,
) -> Result<usize> {
    let mut command = framework_extractor_command();
    command
        .arg(project_path)
        .arg("--framework")
        .arg(framework)
        .arg("--output")
        .arg(output_jsonl);
    let output = command
        .output()
        .with_context(framework_extractor_error_hint)?;
    if !output.status.success() {
        anyhow::bail!(
            "framework extractor failed for `{framework}` with status {}{}\n{}",
            output.status,
            formatted_child_output("stdout", &output.stdout),
            formatted_child_output("stderr", &output.stderr)
        );
    }
    count_jsonl_records(output_jsonl)
}

fn framework_extractor_command() -> Command {
    if let Some(bin) = std::env::var_os("SEMSQL_EXTRACTOR_BIN") {
        return command_for_path(PathBuf::from(bin));
    }
    if let Some(script) = workspace_extractor_cli_script() {
        let mut command = Command::new("node");
        command.arg(script);
        return command;
    }
    if let Some(path) = find_command_on_path("semsql-extract") {
        return command_for_path(path);
    }
    Command::new("semsql-extract")
}

fn workspace_extractor_cli_script() -> Option<PathBuf> {
    workspace_extractor_cli_script_from(
        Path::new(env!("CARGO_MANIFEST_DIR")),
        workspace_extractor_lookup_disabled(),
    )
}

fn workspace_extractor_cli_script_from(manifest_dir: &Path, disabled: bool) -> Option<PathBuf> {
    if disabled {
        return None;
    }
    let script = manifest_dir
        .parent()
        .and_then(std::path::Path::parent)?
        .join("packages")
        .join("extractor-cli")
        .join("dist")
        .join("cli.js");
    script.is_file().then_some(script)
}

fn workspace_extractor_lookup_disabled() -> bool {
    std::env::var_os("SEMSQL_EXTRACTOR_DISABLE_WORKSPACE")
        .map(|value| {
            let value = value.to_string_lossy();
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn find_command_on_path(name: &str) -> Option<PathBuf> {
    find_command_on_path_with_env(
        name,
        std::env::var_os("PATH"),
        std::env::var_os("PATHEXT"),
        cfg!(windows),
    )
}

fn find_command_on_path_with_env(
    name: &str,
    path_env: Option<OsString>,
    pathext: Option<OsString>,
    windows: bool,
) -> Option<PathBuf> {
    let path_env = path_env?;
    let candidates = path_command_candidates(name, pathext.as_deref(), windows);
    for dir in std::env::split_paths(&path_env) {
        for candidate in &candidates {
            let path = dir.join(candidate);
            if path.is_file() {
                return Some(path);
            }
        }
    }
    None
}

fn path_command_candidates(name: &str, pathext: Option<&OsStr>, windows: bool) -> Vec<OsString> {
    if !windows || Path::new(name).extension().is_some() {
        return vec![OsString::from(name)];
    }
    let mut candidates = Vec::new();
    let extensions = pathext
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".to_string());
    for extension in extensions.split(';') {
        let trimmed = extension.trim();
        if trimmed.is_empty() {
            continue;
        }
        let extension = if trimmed.starts_with('.') {
            trimmed.to_string()
        } else {
            format!(".{trimmed}")
        };
        let candidate = OsString::from(format!("{name}{extension}"));
        if !candidates.iter().any(|existing| existing == &candidate) {
            candidates.push(candidate);
        }
    }
    let bare_name = OsString::from(name);
    if !candidates.iter().any(|existing| existing == &bare_name) {
        candidates.push(bare_name);
    }
    candidates
}

fn command_for_path(path: PathBuf) -> Command {
    if cfg!(windows) && is_windows_command_script(&path) {
        let mut command = Command::new("cmd");
        command.arg("/D").arg("/C").arg(path);
        command
    } else {
        Command::new(path)
    }
}

fn is_windows_command_script(path: &Path) -> bool {
    matches!(
        path.extension()
            .and_then(OsStr::to_str)
            .map(str::to_ascii_lowercase)
            .as_deref(),
        Some("cmd" | "bat")
    )
}

fn framework_extractor_error_hint() -> String {
    "running semsql-extract; install/build @semsql/extractor-cli, ensure its bin is on PATH, or set SEMSQL_EXTRACTOR_BIN"
        .to_string()
}

fn formatted_child_output(label: &str, bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    let trimmed = text.trim();
    if trimmed.is_empty() {
        String::new()
    } else {
        format!("\n{label}:\n{}", truncate(trimmed, 2000))
    }
}

fn count_jsonl_records(path: &std::path::Path) -> Result<usize> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("read framework vocabulary {}", path.display()))?;
    Ok(text.lines().filter(|line| !line.trim().is_empty()).count())
}

struct QueryArgs<'a> {
    graph: &'a std::path::Path,
    nl: &'a str,
    dialect: Option<&'a str>,
    cascade_manifest: Option<&'a std::path::Path>,
    intent_yaml: Option<&'a std::path::Path>,
    oracle_skeleton: Option<&'a str>,
    oracle_schema_json: Option<&'a str>,
    oracle_slots_json: Option<&'a str>,
    query_frame_json: Option<&'a std::path::Path>,
    rejection_packet_json: Option<&'a std::path::Path>,
    rejection_include_samples: bool,
}

fn cmd_query(args: QueryArgs<'_>) -> Result<()> {
    let QueryArgs {
        graph,
        nl,
        dialect,
        cascade_manifest,
        intent_yaml,
        oracle_skeleton,
        oracle_schema_json,
        oracle_slots_json,
        query_frame_json,
        rejection_packet_json,
        rejection_include_samples,
    } = args;
    let target_dialect = match dialect {
        None => None,
        Some(name) => Some(parse_dialect(name)?),
    };
    let cascade = semsql_runtime::Cascade::load_with_manifest(graph, intent_yaml, cascade_manifest)
        .with_context(|| format!("loading cascade from {}", graph.display()))?;
    let run_options = semsql_runtime::CascadeRunOptions {
        oracle_linked: oracle_schema_json
            .map(parse_oracle_schema_json)
            .transpose()?,
        oracle_skeleton: oracle_skeleton.map(str::to_owned),
        oracle_slot_map: oracle_slots_json.map(parse_oracle_slots_json).transpose()?,
        capture_runtime_query_frame: query_frame_json.is_some() || rejection_packet_json.is_some(),
    };
    match cascade.run_with_options(nl, &run_options) {
        Ok(out) => {
            if let Some(path) = rejection_packet_json {
                remove_stale_file(path)?;
                eprintln!("rejection_packet_json_skipped=local_routed");
            }
            let final_sql = match target_dialect {
                Some(d) => semsql_renderer::render_text(&out.sql_text, d)
                    .with_context(|| format!("dialect render `{d:?}`"))?,
                None => out.sql_text.clone(),
            };
            println!("{final_sql}");
            eprintln!("stage_pinned={}", out.stage_pinned);
            eprintln!("repair_attempts={}", out.repair_attempts);
            if !out.slot_decisions.is_empty() {
                eprintln!(
                    "stage3_slots={}",
                    serde_json::to_string(&out.slot_decisions)
                        .context("serialising Stage 3 slot diagnostics")?
                );
            }
            if let Some(path) = query_frame_json {
                let payload = query_frame_payload(nl, &out, &final_sql);
                let bytes =
                    serde_json::to_vec_pretty(&payload).context("serialising query frame")?;
                std::fs::write(path, bytes)
                    .with_context(|| format!("writing query frame `{}`", path.display()))?;
                eprintln!("query_frame_json={}", path.display());
            }
            eprintln!(
                "stage_0a={}us stage_0b={}us stage_1={}us stage_2={}us stage_3={}us stage_4={}us",
                out.timings_us.stage_0a,
                out.timings_us.stage_0b,
                out.timings_us.stage_1,
                out.timings_us.stage_2,
                out.timings_us.stage_3,
                out.timings_us.stage_4,
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
            let stage_pinned = if msg.contains("stage2_structural_error") {
                "stage2_structural_error"
            } else if msg.contains("stage2_constraint_error") {
                "stage2_constraint_error"
            } else if msg.contains("stage3_slot_unfilled") {
                "stage3_slot_unfilled"
            } else if msg.contains("stage4_render_error") {
                "stage4_render_error"
            } else if msg.contains("model stages") {
                "needs_model"
            } else {
                "error"
            };
            eprintln!("stage_pinned={stage_pinned}");
            if let Some(path) = query_frame_json {
                let payload = query_frame_error_payload(nl, &cascade, &msg, stage_pinned);
                let bytes = serde_json::to_vec_pretty(&payload)
                    .context("serialising query frame error diagnostics")?;
                std::fs::write(path, bytes)
                    .with_context(|| format!("writing query frame `{}`", path.display()))?;
                eprintln!("query_frame_json={}", path.display());
                if let Some(packet_path) = rejection_packet_json {
                    let packet = rejected_query_packet_payload(
                        graph,
                        nl,
                        stage_pinned,
                        payload,
                        rejection_include_samples,
                    )?;
                    let bytes = serde_json::to_vec_pretty(&packet)
                        .context("serialising rejection packet")?;
                    std::fs::write(packet_path, bytes).with_context(|| {
                        format!("writing rejection packet `{}`", packet_path.display())
                    })?;
                    eprintln!("rejection_packet_json={}", packet_path.display());
                }
            } else if let Some(packet_path) = rejection_packet_json {
                let payload = query_frame_error_payload(nl, &cascade, &msg, stage_pinned);
                let packet = rejected_query_packet_payload(
                    graph,
                    nl,
                    stage_pinned,
                    payload,
                    rejection_include_samples,
                )?;
                let bytes =
                    serde_json::to_vec_pretty(&packet).context("serialising rejection packet")?;
                std::fs::write(packet_path, bytes).with_context(|| {
                    format!("writing rejection packet `{}`", packet_path.display())
                })?;
                eprintln!("rejection_packet_json={}", packet_path.display());
            }
            Err(anyhow::anyhow!("cascade: {e}"))
        }
    }
}

fn remove_stale_file(path: &std::path::Path) -> Result<()> {
    if path.exists() {
        std::fs::remove_file(path)
            .with_context(|| format!("removing stale file `{}`", path.display()))?;
    }
    Ok(())
}

const REJECTION_MAX_ENTITIES: usize = 80;
const REJECTION_MAX_FIELDS_PER_ENTITY: usize = 24;
const REJECTION_MAX_RELATIONSHIPS: usize = 120;
const REJECTION_MAX_SAMPLE_VALUES: usize = 5;
const REJECTION_MAX_VALUE_TERMS: usize = 12;
const REJECTION_MAX_LOCAL_HITS: usize = 40;
const REJECTION_MAX_PHYSICAL_FAMILIES: usize = 24;
const REJECTION_MAX_PHYSICAL_FAMILY_MEMBERS: usize = 24;
const QUERY_FRAME_MAX_ATLAS_ENTITIES: usize = 40;
const QUERY_FRAME_MAX_ATLAS_FIELDS: usize = 160;
const QUERY_FRAME_MAX_ATLAS_RELATIONSHIPS: usize = 120;
const QUERY_FRAME_MAX_ATLAS_VALUE_ALIASES: usize = 160;
const QUERY_FRAME_MAX_ATLAS_METRIC_CANDIDATES: usize = 120;

fn rejected_query_packet_payload(
    graph: &std::path::Path,
    question: &str,
    route_reason: &str,
    query_frame: serde_json::Value,
    include_samples: bool,
) -> Result<serde_json::Value> {
    let schema_card = schema_card_payload(graph, include_samples, Some(question))?;
    let local_candidates = local_candidate_payload(question, &schema_card);
    Ok(serde_json::json!({
        "schema_version": 1,
        "source": "semsql_rejected_query_packet",
        "question": question,
        "route_reason": route_reason,
        "schema_card": schema_card,
        "local_candidates": local_candidates,
        "query_frame": query_frame,
        "allowed_resolution_contract": {
            "llm_output": "resolution_proposal_json",
            "must_not_emit_final_sql": true,
            "must_reference_schema_card_entities_and_fields": true,
            "value_filters_should_use_schema_card_value_dictionary": true,
            "must_ask_clarifying_questions_on_ambiguity": true,
            "semsql_must_validate_before_execution": true,
        },
    }))
}

fn schema_card_payload(
    graph: &std::path::Path,
    include_samples: bool,
    question: Option<&str>,
) -> Result<serde_json::Value> {
    let entity_rows = semsql_graph::read::entities(graph)
        .with_context(|| format!("reading entities from `{}`", graph.display()))?;
    let field_rows = semsql_graph::read::fields(graph)
        .with_context(|| format!("reading fields from `{}`", graph.display()))?;
    let relationship_rows = semsql_graph::read::relationships(graph)
        .with_context(|| format!("reading relationships from `{}`", graph.display()))?;
    let metric_rows = semsql_graph::read::metric_definitions(graph)
        .with_context(|| format!("reading metric definitions from `{}`", graph.display()))?;
    let vocabulary_rows = semsql_graph::read::vocabulary(graph)
        .with_context(|| format!("reading vocabulary from `{}`", graph.display()))?;
    let sample_rows = if include_samples {
        semsql_graph::read::sample_values(graph)
            .with_context(|| format!("reading sample values from `{}`", graph.display()))?
    } else {
        Vec::new()
    };

    let mut fields_by_entity: BTreeMap<String, Vec<&semsql_graph::read::FieldRow>> =
        BTreeMap::new();
    for field in &field_rows {
        fields_by_entity
            .entry(field.entity.clone())
            .or_default()
            .push(field);
    }
    let samples_by_field: BTreeMap<String, Vec<String>> = sample_rows
        .into_iter()
        .map(|row| (row.field_canonical, row.examples))
        .collect();
    let value_dictionary = scope_predicates_by_field(&vocabulary_rows);
    let question_tokens = question.map(text_tokens).unwrap_or_default();
    let relationship_endpoint_fields = relationship_endpoint_fields(&relationship_rows);
    let physical_family_cards =
        physical_table_family_cards(&entity_rows, &fields_by_entity, &question_tokens);

    let mut selected_entities: Vec<&semsql_graph::read::EntityRow> = entity_rows.iter().collect();
    if !question_tokens.is_empty() {
        selected_entities.sort_by(|left, right| {
            let left_score = schema_entity_question_score(
                left,
                fields_by_entity
                    .get(&left.canonical_name)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]),
                &question_tokens,
            );
            let right_score = schema_entity_question_score(
                right,
                fields_by_entity
                    .get(&right.canonical_name)
                    .map(Vec::as_slice)
                    .unwrap_or(&[]),
                &question_tokens,
            );
            right_score
                .cmp(&left_score)
                .then_with(|| left.canonical_name.cmp(&right.canonical_name))
        });
    }

    let entity_cards: Vec<serde_json::Value> = selected_entities
        .into_iter()
        .take(REJECTION_MAX_ENTITIES)
        .map(|entity| {
            let mut entity_fields = fields_by_entity
                .get(&entity.canonical_name)
                .cloned()
                .unwrap_or_default();
            if !question_tokens.is_empty() {
                entity_fields.sort_by(|left, right| {
                    let left_score = schema_field_question_score(
                        left,
                        &question_tokens,
                        &relationship_endpoint_fields,
                    );
                    let right_score = schema_field_question_score(
                        right,
                        &question_tokens,
                        &relationship_endpoint_fields,
                    );
                    right_score
                        .cmp(&left_score)
                        .then_with(|| left.canonical().cmp(&right.canonical()))
                });
            }
            let summarized_fields: Vec<serde_json::Value> = entity_fields
                .iter()
                .take(REJECTION_MAX_FIELDS_PER_ENTITY)
                .map(|field| {
                    field_card_payload(field, include_samples, &samples_by_field, &value_dictionary)
                })
                .collect();
            let display_fields = role_fields(&entity_fields, "display", 8);
            let id_fields = role_fields(&entity_fields, "id", 8);
            let date_fields = role_fields(&entity_fields, "date", 8);
            let status_fields = role_fields(&entity_fields, "status", 8);
            let numeric_fields = role_fields(&entity_fields, "numeric", 8);
            let labels = [
                entity.singular_label.as_deref(),
                entity.plural_label.as_deref(),
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
            serde_json::json!({
                "name": &entity.canonical_name,
                "db_table": &entity.db_table,
                "labels": labels,
                "field_count": entity_fields.len(),
                "fields": summarized_fields,
                "truncated_fields": entity_fields.len().saturating_sub(REJECTION_MAX_FIELDS_PER_ENTITY),
                "display_fields": display_fields,
                "id_fields": id_fields,
                "date_fields": date_fields,
                "status_fields": status_fields,
                "numeric_fields": numeric_fields,
                "sensitive": schema_entity_sensitive(entity, &entity_fields),
            })
        })
        .collect();

    let relationship_cards: Vec<serde_json::Value> = relationship_rows
        .iter()
        .take(REJECTION_MAX_RELATIONSHIPS)
        .map(|row| {
            serde_json::json!({
                "from": format!("{}.{}", row.from_entity, row.from_field),
                "to": format!("{}.{}", row.to_entity, row.to_field),
                "kind": row.kind,
            })
        })
        .collect();
    let metric_cards: Vec<serde_json::Value> = metric_rows
        .iter()
        .map(metric_definition_card_payload)
        .collect();

    Ok(serde_json::json!({
        "schema_version": 1,
        "source": "semsql_schema_card",
        "graph": graph.display().to_string(),
        "summary": {
            "entity_count": entity_rows.len(),
            "field_count": field_rows.len(),
            "relationship_count": relationship_rows.len(),
            "metric_definition_count": metric_rows.len(),
            "sample_values_included": include_samples,
            "value_dictionary_count": value_dictionary.values().map(Vec::len).sum::<usize>(),
            "ambiguous_physical_family_count": physical_family_cards.len(),
            "sensitive_entity_count": entity_cards
                .iter()
                .filter(|entity| entity["sensitive"].as_bool().unwrap_or(false))
                .count(),
            "entities_truncated": entity_rows.len().saturating_sub(REJECTION_MAX_ENTITIES),
            "relationships_truncated": relationship_rows
                .len()
                .saturating_sub(REJECTION_MAX_RELATIONSHIPS),
        },
        "entities": entity_cards,
        "relationships": relationship_cards,
        "physical_table_families": physical_family_cards,
        "metric_definitions": metric_cards,
        "safety": {
            "samples_policy": if include_samples {
                "included_non_redacted_graph_samples"
            } else {
                "omitted_by_default"
            },
            "llm_may_not_execute_sql": true,
            "llm_sql_must_be_revalidated": true,
            "ambiguous_physical_tables_fail_closed": true,
            "value_dictionary_policy": "field_scoped_scope_predicate_vocabulary_only",
        },
    }))
}

fn metric_definition_card_payload(
    metric: &semsql_graph::read::MetricDefinitionRow,
) -> serde_json::Value {
    serde_json::json!({
        "name": &metric.name,
        "display_label": &metric.display_label,
        "metric_kind": &metric.metric_kind,
        "subject_entity": &metric.subject_entity,
        "numerator_field": &metric.numerator_field,
        "numerator_operator": &metric.numerator_operator,
        "numerator_value": &metric.numerator_value,
        "numerator_value_kind": &metric.numerator_value_kind,
        "denominator_field": &metric.denominator_field,
        "scale": metric.scale,
        "measure_field": &metric.measure_field,
        "aggregate": &metric.aggregate,
        "distinct": metric.distinct_measure,
        "required_entities": &metric.required_entities,
        "aliases": &metric.aliases,
    })
}

fn field_card_payload(
    field: &semsql_graph::read::FieldRow,
    include_samples: bool,
    samples_by_field: &BTreeMap<String, Vec<String>>,
    value_dictionary: &BTreeMap<String, Vec<serde_json::Value>>,
) -> serde_json::Value {
    let canonical = field.canonical();
    let samples = if include_samples {
        samples_by_field
            .get(&canonical)
            .map(|values| {
                values
                    .iter()
                    .take(REJECTION_MAX_SAMPLE_VALUES)
                    .cloned()
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    serde_json::json!({
        "name": field.field,
        "db_column": field.db_column,
        "type": field.field_type,
        "display_label": field.display_label,
        "role": schema_field_role(field),
        "samples": samples,
        "value_dictionary": value_dictionary.get(&canonical).cloned().unwrap_or_default(),
    })
}

fn relationship_endpoint_fields(
    relationships: &[semsql_graph::read::RelationshipRow],
) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for relationship in relationships {
        out.insert(format!(
            "{}.{}",
            relationship.from_entity, relationship.from_field
        ));
        out.insert(format!(
            "{}.{}",
            relationship.to_entity, relationship.to_field
        ));
    }
    out
}

fn schema_entity_question_score(
    entity: &semsql_graph::read::EntityRow,
    fields: &[&semsql_graph::read::FieldRow],
    question_tokens: &BTreeSet<String>,
) -> usize {
    let entity_tokens = text_tokens(&format!(
        "{} {} {} {}",
        entity.canonical_name,
        entity.db_table,
        entity.singular_label.as_deref().unwrap_or(""),
        entity.plural_label.as_deref().unwrap_or("")
    ));
    let entity_overlap = entity_tokens.intersection(question_tokens).count();
    let field_overlap = fields
        .iter()
        .map(|field| {
            let tokens = text_tokens(&format!(
                "{} {} {}",
                field.field,
                field.db_column,
                field.display_label.as_deref().unwrap_or("")
            ));
            tokens.intersection(question_tokens).count()
        })
        .max()
        .unwrap_or(0);
    entity_overlap * 8 + field_overlap * 3
}

fn schema_field_question_score(
    field: &semsql_graph::read::FieldRow,
    question_tokens: &BTreeSet<String>,
    relationship_endpoint_fields: &BTreeSet<String>,
) -> usize {
    let tokens = text_tokens(&format!(
        "{} {} {}",
        field.field,
        field.db_column,
        field.display_label.as_deref().unwrap_or("")
    ));
    let mut score = tokens.intersection(question_tokens).count() * 8;
    let role = schema_field_role(field);
    if matches!(role, "status" | "numeric" | "boolean" | "date" | "display") {
        score += 2;
    }
    if relationship_endpoint_fields.contains(&field.canonical()) {
        score += 3;
    }
    if field.field.eq_ignore_ascii_case("id") || field.db_column.eq_ignore_ascii_case("id") {
        score += 1;
    }
    score
}

fn physical_table_family_cards(
    entities: &[semsql_graph::read::EntityRow],
    fields_by_entity: &BTreeMap<String, Vec<&semsql_graph::read::FieldRow>>,
    question_tokens: &BTreeSet<String>,
) -> Vec<serde_json::Value> {
    semsql_graph::read::physical_table_families_from_entities(entities)
        .into_iter()
        .take(REJECTION_MAX_PHYSICAL_FAMILIES)
        .map(|family| {
            let member_count = family.members.len();
            let members = family
                .members
                .iter()
                .take(REJECTION_MAX_PHYSICAL_FAMILY_MEMBERS)
                .map(|member| {
                    let role = if member.db_table.eq_ignore_ascii_case(&family.base_table) {
                        "base_table"
                    } else {
                        "physical_partition"
                    };
                    serde_json::json!({
                        "entity": member.entity,
                        "db_table": member.db_table,
                        "role": role,
                        "field_count": fields_by_entity
                            .get(&member.entity)
                            .map(Vec::len)
                            .unwrap_or(0),
                        "matched_tokens": name_matched_tokens(
                            &format!("{} {}", member.entity, member.db_table),
                            question_tokens,
                        ),
                    })
                })
                .collect::<Vec<_>>();
            serde_json::json!({
                "base_table": family.base_table,
                "anchor": family.anchor,
                "member_count": member_count,
                "members": members,
                "truncated_members": member_count.saturating_sub(REJECTION_MAX_PHYSICAL_FAMILY_MEMBERS),
                "matched_tokens": name_matched_tokens(&family.base_table, question_tokens),
                "requires_clarification": true,
                "resolution_hint": "multiple physical tables look like one logical table family; choose only with app metadata, user clarification, or an explicit metric/table catalog",
            })
        })
        .collect()
}

fn name_matched_tokens(name: &str, question_tokens: &BTreeSet<String>) -> Vec<String> {
    let mut matched = BTreeSet::new();
    for raw in name
        .to_ascii_lowercase()
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| token.len() >= 2)
    {
        let variants = text_tokens(raw);
        if let Some(hit) = variants
            .iter()
            .find(|variant| question_tokens.contains(*variant))
        {
            matched.insert(hit.clone());
        }
    }
    matched.into_iter().collect()
}

fn scope_predicates_by_field(
    vocabulary: &[semsql_graph::read::VocabularyEntry],
) -> BTreeMap<String, Vec<serde_json::Value>> {
    let mut out: BTreeMap<String, Vec<serde_json::Value>> = BTreeMap::new();
    for entry in vocabulary {
        if entry.canonical_kind != "scope_predicate" {
            continue;
        }
        let Ok(value) = serde_json::from_str::<serde_json::Value>(&entry.canonical_value) else {
            continue;
        };
        let Some(field) = value.get("field").and_then(serde_json::Value::as_str) else {
            continue;
        };
        let raw_value = value
            .get("rawValue")
            .cloned()
            .or_else(|| value.get("raw_value").cloned())
            .unwrap_or(serde_json::Value::Null);
        let operator = value
            .get("operator")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("=");
        let scope = value
            .get("scope")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("");
        out.entry(field.to_string())
            .or_default()
            .push(serde_json::json!({
                "term": entry.term,
                "operator": operator,
                "raw_value": raw_value,
                "scope": scope,
                "confidence": entry.confidence,
                "source_layer": entry.source_layer,
            }));
    }
    for values in out.values_mut() {
        values.sort_by(|left, right| {
            let left_key = format!(
                "{}\u{0}{}",
                left["term"].as_str().unwrap_or(""),
                left["scope"].as_str().unwrap_or("")
            );
            let right_key = format!(
                "{}\u{0}{}",
                right["term"].as_str().unwrap_or(""),
                right["scope"].as_str().unwrap_or("")
            );
            left_key.cmp(&right_key)
        });
        values.truncate(REJECTION_MAX_VALUE_TERMS);
    }
    out
}

fn role_fields(fields: &[&semsql_graph::read::FieldRow], role: &str, limit: usize) -> Vec<String> {
    fields
        .iter()
        .filter(|field| schema_field_role(field) == role)
        .map(|field| field.field.clone())
        .take(limit)
        .collect()
}

fn schema_field_role(field: &semsql_graph::read::FieldRow) -> &'static str {
    let field_lower = field.field.to_ascii_lowercase();
    let column_lower = field.db_column.to_ascii_lowercase();
    let label_lower = field
        .display_label
        .as_deref()
        .unwrap_or("")
        .to_ascii_lowercase();
    let tokens = text_tokens(&format!("{field_lower} {column_lower} {label_lower}"));
    if field_lower == "id" || column_lower == "id" {
        return "id";
    }
    if tokens.contains("date")
        || tokens.contains("time")
        || tokens.contains("timestamp")
        || tokens.contains("created")
        || tokens.contains("updated")
        || tokens.contains("resolved")
        || tokens.contains("closed")
        || field_type_kind(&field.field_type) == "date"
    {
        return "date";
    }
    if tokens.contains("status")
        || tokens.contains("state")
        || tokens.contains("stage")
        || tokens.contains("lifecycle")
    {
        return "status";
    }
    if tokens.contains("name")
        || tokens.contains("title")
        || tokens.contains("label")
        || tokens.contains("subject")
        || tokens.contains("email")
        || tokens.contains("description")
    {
        return "display";
    }
    if field_type_kind(&field.field_type) == "number" {
        return "numeric";
    }
    "attribute"
}

fn field_type_kind(field_type: &str) -> &'static str {
    let raw = field_type.to_ascii_lowercase();
    if ["int", "real", "float", "double", "decimal", "numeric"]
        .iter()
        .any(|needle| raw.contains(needle))
    {
        return "number";
    }
    if raw.contains("bool") {
        return "boolean";
    }
    if raw.contains("timestamp") || raw.contains("datetime") || raw == "date" {
        return "date";
    }
    "text"
}

fn schema_entity_sensitive(
    entity: &semsql_graph::read::EntityRow,
    fields: &[&semsql_graph::read::FieldRow],
) -> bool {
    let sensitive_tokens: BTreeSet<String> = [
        "api",
        "auth",
        "credential",
        "encrypted",
        "hash",
        "key",
        "login",
        "password",
        "secret",
        "session",
        "token",
    ]
    .into_iter()
    .map(str::to_string)
    .collect();
    let mut tokens = text_tokens(&format!(
        "{} {} {} {}",
        entity.canonical_name,
        entity.db_table,
        entity.singular_label.as_deref().unwrap_or(""),
        entity.plural_label.as_deref().unwrap_or("")
    ));
    for field in fields {
        tokens.extend(text_tokens(&format!(
            "{} {} {}",
            field.field,
            field.db_column,
            field.display_label.as_deref().unwrap_or("")
        )));
    }
    !tokens.is_disjoint(&sensitive_tokens)
}

fn local_candidate_payload(question: &str, schema_card: &serde_json::Value) -> serde_json::Value {
    let question_tokens = text_tokens(question);
    let mut entity_hits = Vec::new();
    let mut field_hits = Vec::new();
    let mut value_hits = Vec::new();
    let metric_catalog_hits = metric_catalog_hits_payload(&question_tokens, schema_card);
    let metric_catalog_ambiguous = metric_catalog_hits.len() > 1;
    let physical_family_mentions =
        physical_table_family_mentions_payload(&question_tokens, schema_card);
    let Some(entities) = schema_card["entities"].as_array() else {
        return serde_json::json!({
            "entity_hits": entity_hits,
            "field_hits": field_hits,
            "value_dictionary_hits": value_hits,
            "metric_catalog_hits": metric_catalog_hits,
            "metric_catalog_ambiguous": metric_catalog_ambiguous,
            "ambiguous_physical_families_mentioned": physical_family_mentions,
        });
    };
    for entity in entities {
        let entity_name = entity["name"].as_str().unwrap_or("");
        let entity_tokens = text_tokens(&format!(
            "{} {} {}",
            entity_name,
            entity["db_table"].as_str().unwrap_or(""),
            entity["labels"]
                .as_array()
                .map(|labels| labels
                    .iter()
                    .filter_map(serde_json::Value::as_str)
                    .collect::<Vec<_>>()
                    .join(" "))
                .unwrap_or_default()
        ));
        let matched_entity_tokens = sorted_intersection(&question_tokens, &entity_tokens);
        if !matched_entity_tokens.is_empty() {
            entity_hits.push(serde_json::json!({
                "entity": entity_name,
                "matched_tokens": matched_entity_tokens,
            }));
        }
        let Some(fields) = entity["fields"].as_array() else {
            continue;
        };
        for field in fields {
            let field_name = field["name"].as_str().unwrap_or("");
            let field_ref = format!("{entity_name}.{field_name}");
            let field_tokens = text_tokens(&format!(
                "{} {} {}",
                field_name,
                field["db_column"].as_str().unwrap_or(""),
                field["display_label"].as_str().unwrap_or("")
            ));
            let matched_field_tokens = sorted_intersection(&question_tokens, &field_tokens);
            if !matched_field_tokens.is_empty() {
                field_hits.push(serde_json::json!({
                    "field": field_ref,
                    "role": field["role"].as_str().unwrap_or("attribute"),
                    "matched_tokens": matched_field_tokens,
                }));
            }
            let Some(value_dictionary) = field["value_dictionary"].as_array() else {
                continue;
            };
            for value in value_dictionary {
                let term = value["term"].as_str().unwrap_or("");
                let term_tokens = text_tokens(term);
                if term_tokens.is_empty() || !term_tokens.is_subset(&question_tokens) {
                    continue;
                }
                value_hits.push(serde_json::json!({
                    "field": field_ref,
                    "term": term,
                    "operator": value["operator"].as_str().unwrap_or("="),
                    "raw_value": value["raw_value"].clone(),
                    "scope": value["scope"].as_str().unwrap_or(""),
                    "matched_tokens": term_tokens.into_iter().collect::<Vec<_>>(),
                    "confidence": value["confidence"].as_f64().unwrap_or(0.0),
                }));
            }
        }
    }
    entity_hits.truncate(REJECTION_MAX_LOCAL_HITS);
    field_hits.truncate(REJECTION_MAX_LOCAL_HITS);
    value_hits.truncate(REJECTION_MAX_LOCAL_HITS);
    serde_json::json!({
        "entity_hits": entity_hits,
        "field_hits": field_hits,
        "value_dictionary_hits": value_hits,
        "metric_catalog_hits": metric_catalog_hits,
        "metric_catalog_ambiguous": metric_catalog_ambiguous,
        "ambiguous_physical_families_mentioned": physical_family_mentions,
    })
}

fn metric_catalog_hits_payload(
    question_tokens: &BTreeSet<String>,
    schema_card: &serde_json::Value,
) -> Vec<serde_json::Value> {
    if question_tokens.is_empty() {
        return Vec::new();
    }
    let mut entity_names = BTreeSet::new();
    let mut field_names = BTreeSet::new();
    if let Some(entities) = schema_card["entities"].as_array() {
        for entity in entities {
            let Some(entity_name) = entity["name"].as_str() else {
                continue;
            };
            entity_names.insert(entity_name.to_string());
            if let Some(fields) = entity["fields"].as_array() {
                for field in fields {
                    if let Some(field_name) = field["name"].as_str() {
                        field_names.insert(format!("{entity_name}.{field_name}"));
                    }
                }
            }
        }
    }
    let mut hits = Vec::new();
    let Some(metrics) = schema_card["metric_definitions"].as_array() else {
        return hits;
    };
    for metric in metrics {
        let metric_kind = metric["metric_kind"].as_str().unwrap_or("");
        let subject_entity = metric["subject_entity"].as_str().unwrap_or("");
        if !entity_names.contains(subject_entity) {
            continue;
        }
        let Some(matched_tokens) = best_metric_label_match(question_tokens, metric) else {
            continue;
        };
        let required_entities = metric["required_entities"]
            .as_array()
            .map(|entities| {
                entities
                    .iter()
                    .filter_map(serde_json::Value::as_str)
                    .filter(|entity| entity_names.contains(*entity))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_else(|| vec![subject_entity]);
        if metric_kind == "conditional_rate" {
            let numerator_field = metric["numerator_field"].as_str().unwrap_or("");
            let denominator_field = metric["denominator_field"].as_str().unwrap_or("");
            if !field_names.contains(numerator_field) || !field_names.contains(denominator_field) {
                continue;
            }
            hits.push(serde_json::json!({
                "metric_kind": "conditional_rate",
                "name": metric["name"].as_str().unwrap_or(""),
                "display_label": metric["display_label"].as_str().unwrap_or(""),
                "alias": metric["name"].as_str().unwrap_or("metric"),
                "subject_entity": subject_entity,
                "numerator_field": numerator_field,
                "numerator_operator": metric["numerator_operator"].as_str().unwrap_or("="),
                "numerator_value": metric["numerator_value"].clone(),
                "numerator_value_kind": metric["numerator_value_kind"].as_str().unwrap_or("literal"),
                "denominator_field": denominator_field,
                "scale": metric["scale"].as_f64().unwrap_or(100.0),
                "required_entities": required_entities,
                "matched_tokens": matched_tokens,
                "source": "metric_definition",
            }));
        } else if metric_kind == "aggregate" {
            let measure_field = metric["measure_field"].as_str().unwrap_or("");
            let aggregate = metric["aggregate"]
                .as_str()
                .unwrap_or("")
                .to_ascii_uppercase();
            if !field_names.contains(measure_field)
                || !matches!(aggregate.as_str(), "AVG" | "COUNT" | "MAX" | "MIN" | "SUM")
            {
                continue;
            }
            hits.push(serde_json::json!({
                "metric_kind": "aggregate",
                "name": metric["name"].as_str().unwrap_or(""),
                "display_label": metric["display_label"].as_str().unwrap_or(""),
                "alias": metric["name"].as_str().unwrap_or("metric"),
                "subject_entity": subject_entity,
                "measure_field": measure_field,
                "aggregate": aggregate,
                "distinct": metric["distinct"].as_bool().unwrap_or(false),
                "scale": metric["scale"].as_f64().unwrap_or(1.0),
                "required_entities": required_entities,
                "matched_tokens": matched_tokens,
                "source": "metric_definition",
            }));
        }
        if hits.len() == REJECTION_MAX_LOCAL_HITS {
            break;
        }
    }
    hits
}

fn physical_table_family_mentions_payload(
    question_tokens: &BTreeSet<String>,
    schema_card: &serde_json::Value,
) -> Vec<serde_json::Value> {
    let Some(families) = schema_card["physical_table_families"].as_array() else {
        return Vec::new();
    };
    let mut hits = Vec::new();
    for family in families {
        let base_table = family["base_table"].as_str().unwrap_or("");
        let matched_tokens = if let Some(tokens) = family["matched_tokens"].as_array() {
            tokens
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(str::to_string)
                .collect::<Vec<_>>()
        } else {
            name_matched_tokens(base_table, question_tokens)
        };
        if matched_tokens.is_empty() {
            continue;
        }
        let member_mentions = family["members"]
            .as_array()
            .map(|members| {
                members
                    .iter()
                    .filter_map(|member| {
                        let member_tokens = member["matched_tokens"].as_array()?;
                        if member_tokens.is_empty() {
                            return None;
                        }
                        Some(serde_json::json!({
                            "entity": member["entity"].as_str().unwrap_or(""),
                            "db_table": member["db_table"].as_str().unwrap_or(""),
                            "role": member["role"].as_str().unwrap_or("physical_partition"),
                            "matched_tokens": member_tokens,
                        }))
                    })
                    .take(REJECTION_MAX_PHYSICAL_FAMILY_MEMBERS)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let exactly_one_specific_partition = member_mentions.len() == 1
            && member_mentions[0]["role"] == serde_json::json!("physical_partition");
        hits.push(serde_json::json!({
            "base_table": base_table,
            "anchor": family["anchor"].as_str().unwrap_or(""),
            "member_count": family["member_count"].as_u64().unwrap_or(0),
            "matched_tokens": matched_tokens,
            "member_mentions": member_mentions,
            "requires_clarification": !exactly_one_specific_partition,
            "resolution_hint": "do not pick a physical partition from the base word alone; use app metadata, a table catalog, or ask which partition/scope is intended",
        }));
        if hits.len() == REJECTION_MAX_LOCAL_HITS {
            break;
        }
    }
    hits
}

fn best_metric_label_match(
    question_tokens: &BTreeSet<String>,
    metric: &serde_json::Value,
) -> Option<Vec<String>> {
    let mut labels = vec![
        metric["name"].as_str().unwrap_or("").to_string(),
        metric["display_label"].as_str().unwrap_or("").to_string(),
    ];
    if let Some(aliases) = metric["aliases"].as_array() {
        labels.extend(
            aliases
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(str::to_string),
        );
    }
    labels
        .into_iter()
        .filter_map(|label| {
            let tokens = text_tokens(&label);
            if tokens.is_empty()
                || !tokens.is_subset(question_tokens)
                || !metric_tokens_have_meaning(&tokens)
            {
                return None;
            }
            Some(tokens.into_iter().collect::<Vec<_>>())
        })
        .max_by(|left, right| left.len().cmp(&right.len()).then_with(|| left.cmp(right)))
}

fn metric_tokens_have_meaning(tokens: &BTreeSet<String>) -> bool {
    tokens.iter().any(|token| {
        !matches!(
            token.as_str(),
            "metric" | "rate" | "percent" | "percentage" | "pct" | "total" | "count"
        )
    })
}

fn sorted_intersection(left: &BTreeSet<String>, right: &BTreeSet<String>) -> Vec<String> {
    left.intersection(right).cloned().collect()
}

fn text_tokens(text: &str) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for raw in text
        .to_ascii_lowercase()
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| token.len() >= 2)
    {
        out.insert(raw.to_string());
        if raw.ends_with("ies") && raw.len() > 3 {
            out.insert(format!("{}y", &raw[..raw.len() - 3]));
        } else if raw.ends_with('s')
            && raw.len() > 3
            && !raw.ends_with("ss")
            && !raw.ends_with("us")
            && !raw.ends_with("is")
        {
            out.insert(raw[..raw.len() - 1].to_string());
        }
    }
    out
}

fn query_frame_error_payload(
    nl: &str,
    cascade: &semsql_runtime::Cascade,
    error: &str,
    stage_pinned: &str,
) -> serde_json::Value {
    let (runtime_query_frame, semantic_atlas, intent_frame, bound_query_plan) =
        cascade.runtime_query_diagnostics(nl);
    let mut payload = serde_json::json!({
        "schema_version": 3,
        "source": "query_frame_error",
        "question": nl,
        "final_sql": null,
        "stage_pinned": stage_pinned,
        "error": error,
        "runtime_query_frame": runtime_query_frame,
        "semantic_atlas": semantic_atlas,
        "intent_frame": intent_frame,
        "bound_query_plan": bound_query_plan,
        "note": "diagnostic packet written after a fail-closed cascade error; no SQL was promoted",
    });
    cap_query_frame_diagnostic_payload(&mut payload);
    payload
}

fn query_frame_payload(
    nl: &str,
    out: &semsql_runtime::CascadeOutcome,
    final_sql: &str,
) -> serde_json::Value {
    let mut projections = Vec::new();
    let mut predicates = Vec::new();
    let mut joins = Vec::new();
    let mut groups = Vec::new();
    let mut orderings = Vec::new();
    let mut limits = Vec::new();

    for decision in &out.slot_decisions {
        let picked = decision.picked.clone();
        match decision.slot_role.as_str() {
            "projection_field" => projections.push(serde_json::json!({
                "slot": &decision.slot_name,
                "field": picked,
            })),
            "predicate_field" => predicates.push(serde_json::json!({
                "field_slot": &decision.slot_name,
                "field": picked,
                "operator": &decision.predicate_operator,
            })),
            "predicate_value" => predicates.push(serde_json::json!({
                "value_slot": &decision.slot_name,
                "field_slot": &decision.predicate_field_slot,
                "field": &decision.predicate_field,
                "operator": &decision.predicate_operator,
                "value": picked,
            })),
            "join_key" => joins.push(serde_json::json!({
                "slot": &decision.slot_name,
                "field": picked,
            })),
            "group_field" => groups.push(serde_json::json!({
                "slot": &decision.slot_name,
                "field": picked,
            })),
            "order_field" => orderings.push(serde_json::json!({
                "slot": &decision.slot_name,
                "field": picked,
                "operator": &decision.predicate_operator,
            })),
            "limit_value" => limits.push(serde_json::json!({
                "slot": &decision.slot_name,
                "value": picked,
            })),
            _ => {}
        }
    }

    let slots: Vec<serde_json::Value> = out
        .slot_decisions
        .iter()
        .map(|decision| {
            let pre_slot = out.query_frame.as_ref().and_then(|frame| {
                frame
                    .slots
                    .iter()
                    .find(|slot| slot.slot == decision.slot_name)
            });
            let candidates: Vec<serde_json::Value> = decision
                .candidates
                .iter()
                .map(|candidate| {
                    serde_json::json!({
                        "value": &candidate.value,
                        "score": candidate.score,
                        "biased_score": candidate.biased_score,
                        "source_fields": &candidate.source_fields,
                    })
                })
                .collect();
            let rejected_candidates = rejected_candidates_for_decision(decision, pre_slot);
            serde_json::json!({
                "slot": &decision.slot_name,
                "kind": &decision.slot_kind,
                "role": &decision.slot_role,
                "picked": &decision.picked,
                "picked_index": decision.picked_index,
                "predicate_field_slot": &decision.predicate_field_slot,
                "predicate_field": &decision.predicate_field,
                "predicate_operator": &decision.predicate_operator,
                "candidate_count": decision.original_candidate_count,
                "scored_candidate_count": decision.candidates.len(),
                "candidates": candidates,
                "rejected_candidates": rejected_candidates,
                "escalated": decision.escalated,
                "context_window": &decision.context_window,
            })
        })
        .collect();
    let selected_bindings = selected_query_frame_bindings(out);
    let result_shape = result_shape_hint(final_sql);

    let mut payload = serde_json::json!({
        "schema_version": 3,
        "source": "query_frame",
        "question": nl,
        "sql": final_sql,
        "stage_pinned": &out.stage_pinned,
        "repair_attempts": out.repair_attempts,
        "intent_hints": &out.intent_hints,
        "runtime_query_frame": &out.runtime_query_frame,
        "semantic_atlas": &out.semantic_atlas,
        "intent_frame": &out.intent_frame,
        "bound_query_plan": &out.bound_query_plan,
        "pre_stage3": &out.query_frame,
        "stage3": {
            "frame": {
                "projections": projections,
                "predicates": predicates,
                "joins": joins,
                "groups": groups,
                "orderings": orderings,
                "limits": limits,
            },
            "slots": slots,
            "selected_bindings": selected_bindings,
        },
        "result_shape": result_shape,
        "diagnostics": {
            "note": "runtime_query_frame records the graph-backed state-machine route decision; semantic_atlas, intent_frame, and bound_query_plan are the typed planning boundary; pre_stage3 is the candidate contract before Stage 3 scoring; stage3 records the scored/picked decisions when available.",
            "renderability": renderability_diagnostics(final_sql),
        },
    });
    cap_query_frame_diagnostic_payload(&mut payload);
    payload
}

fn cap_query_frame_diagnostic_payload(payload: &mut serde_json::Value) {
    let Some(atlas) = payload
        .get_mut("semantic_atlas")
        .and_then(serde_json::Value::as_object_mut)
    else {
        return;
    };
    let entities_truncated = truncate_json_array(atlas, "entities", QUERY_FRAME_MAX_ATLAS_ENTITIES);
    let fields_truncated = truncate_json_array(atlas, "fields", QUERY_FRAME_MAX_ATLAS_FIELDS);
    let relationships_truncated =
        truncate_json_array(atlas, "relationships", QUERY_FRAME_MAX_ATLAS_RELATIONSHIPS);
    let value_aliases_truncated =
        truncate_json_array(atlas, "value_aliases", QUERY_FRAME_MAX_ATLAS_VALUE_ALIASES);
    let metric_candidates_truncated = truncate_json_array(
        atlas,
        "metric_candidates",
        QUERY_FRAME_MAX_ATLAS_METRIC_CANDIDATES,
    );
    let truncated = entities_truncated > 0
        || fields_truncated > 0
        || relationships_truncated > 0
        || value_aliases_truncated > 0
        || metric_candidates_truncated > 0;
    atlas.insert(
        "diagnostic_truncated".to_string(),
        serde_json::json!(truncated),
    );
    atlas.insert(
        "diagnostic_limits".to_string(),
        serde_json::json!({
            "entities": QUERY_FRAME_MAX_ATLAS_ENTITIES,
            "fields": QUERY_FRAME_MAX_ATLAS_FIELDS,
            "relationships": QUERY_FRAME_MAX_ATLAS_RELATIONSHIPS,
            "value_aliases": QUERY_FRAME_MAX_ATLAS_VALUE_ALIASES,
            "metric_candidates": QUERY_FRAME_MAX_ATLAS_METRIC_CANDIDATES,
        }),
    );
    atlas.insert(
        "diagnostic_truncated_counts".to_string(),
        serde_json::json!({
            "entities": entities_truncated,
            "fields": fields_truncated,
            "relationships": relationships_truncated,
            "value_aliases": value_aliases_truncated,
            "metric_candidates": metric_candidates_truncated,
        }),
    );
}

fn truncate_json_array(
    object: &mut serde_json::Map<String, serde_json::Value>,
    key: &str,
    limit: usize,
) -> usize {
    let Some(values) = object
        .get_mut(key)
        .and_then(serde_json::Value::as_array_mut)
    else {
        return 0;
    };
    let truncated = values.len().saturating_sub(limit);
    values.truncate(limit);
    truncated
}

#[derive(Debug, Clone)]
struct SelectShapeItem {
    label: String,
    aggregate: bool,
    time_like: bool,
    numeric_like: bool,
}

fn result_shape_hint(sql: &str) -> serde_json::Value {
    let items = select_shape_items(sql);
    if items.is_empty() {
        return serde_json::json!({
            "schema_version": 1,
            "kind": "unknown",
            "default_view": "table",
            "chartjs": null,
            "reason": "select list could not be inspected",
        });
    }

    let has_group_by = sql_has_keyword(sql, "group by");
    let dimensions: Vec<&SelectShapeItem> = items
        .iter()
        .filter(|item| !item.aggregate && !item.numeric_like)
        .collect();
    let measures: Vec<&SelectShapeItem> = items
        .iter()
        .filter(|item| item.aggregate || item.numeric_like)
        .collect();

    if items.len() == 1 && items[0].aggregate && !has_group_by {
        return serde_json::json!({
            "schema_version": 1,
            "kind": "scalar_metric",
            "default_view": "metric",
            "columns": [{"name": items[0].label, "role": "measure"}],
            "chartjs": null,
            "reason": "single aggregate without GROUP BY",
        });
    }

    if has_group_by && dimensions.len() >= 2 && !measures.is_empty() {
        let label_index = dimensions
            .iter()
            .position(|dimension| dimension.time_like)
            .unwrap_or(0);
        let label_dimension = dimensions[label_index];
        let series_dimension = dimensions
            .iter()
            .enumerate()
            .find_map(|(idx, dimension)| (idx != label_index).then_some(*dimension))
            .unwrap_or(dimensions[1]);
        let chart_type = if label_dimension.time_like {
            "line"
        } else {
            "bar"
        };
        let datasets: Vec<serde_json::Value> = measures
            .iter()
            .map(|measure| {
                serde_json::json!({
                    "label": measure.label,
                    "data_from": measure.label,
                })
            })
            .collect();
        return serde_json::json!({
            "schema_version": 1,
            "kind": "multi_series_chart",
            "default_view": "chart",
            "columns": [
                {"name": label_dimension.label, "role": "dimension"},
                {"name": series_dimension.label, "role": "series"},
                {"name": measures[0].label, "role": "measure"}
            ],
            "chartjs": {
                "type": chart_type,
                "mapping": {
                    "labels_from": label_dimension.label,
                    "series_from": series_dimension.label,
                    "datasets": datasets,
                }
            },
            "fallback_view": "table",
            "reason": "two grouped dimensions with one or more measures",
        });
    }

    if has_group_by && dimensions.len() == 1 && !measures.is_empty() {
        let dimension = dimensions[0];
        let chart_type = if dimension.time_like { "line" } else { "bar" };
        let kind = if dimension.time_like {
            "time_series_chart"
        } else {
            "categorical_chart"
        };
        let datasets: Vec<serde_json::Value> = measures
            .iter()
            .map(|measure| {
                serde_json::json!({
                    "label": measure.label,
                    "data_from": measure.label,
                })
            })
            .collect();
        return serde_json::json!({
            "schema_version": 1,
            "kind": kind,
            "default_view": "chart",
            "columns": [
                {"name": dimension.label, "role": "dimension"},
                {"name": measures[0].label, "role": "measure"}
            ],
            "chartjs": {
                "type": chart_type,
                "mapping": {
                    "labels_from": dimension.label,
                    "datasets": datasets,
                }
            },
            "fallback_view": "table",
            "reason": "one grouped dimension with one or more measures",
        });
    }

    serde_json::json!({
        "schema_version": 1,
        "kind": "tabular",
        "default_view": "table",
        "columns": items.iter().map(|item| {
            let role = if item.aggregate || item.numeric_like {
                "measure"
            } else if item.time_like {
                "time_dimension"
            } else {
                "dimension"
            };
            serde_json::json!({"name": item.label, "role": role})
        }).collect::<Vec<_>>(),
        "chartjs": null,
        "reason": "shape is best represented as rows",
    })
}

fn select_shape_items(sql: &str) -> Vec<SelectShapeItem> {
    let Some(select_clause) = top_level_select_clause(sql) else {
        return Vec::new();
    };
    split_top_level_commas(&select_clause)
        .into_iter()
        .filter_map(|expression| {
            let trimmed = expression.trim();
            if trimmed.is_empty() {
                return None;
            }
            let lower = trimmed.to_ascii_lowercase();
            let aggregate = looks_like_aggregate_expr(&lower);
            let label = select_item_label(trimmed);
            let label_lower = label.to_ascii_lowercase();
            let time_like =
                looks_like_time_dimension(&lower) || looks_like_time_dimension(&label_lower);
            let numeric_like = aggregate
                || looks_like_numeric_measure(&lower)
                || looks_like_numeric_measure(&label_lower);
            Some(SelectShapeItem {
                label,
                aggregate,
                time_like,
                numeric_like,
            })
        })
        .collect()
}

fn top_level_select_clause(sql: &str) -> Option<String> {
    let lower = sql.to_ascii_lowercase();
    let select_at = lower.find("select")?;
    let from_at = find_top_level_keyword_after(sql, "from", select_at + "select".len())?;
    Some(sql[select_at + "select".len()..from_at].trim().to_string())
}

fn find_top_level_keyword_after(sql: &str, keyword: &str, start: usize) -> Option<usize> {
    let lower = sql.to_ascii_lowercase();
    let needle = keyword.to_ascii_lowercase();
    let bytes = sql.as_bytes();
    let mut depth = 0_i32;
    let mut in_single = false;
    let mut in_double = false;
    let mut in_backtick = false;
    let mut i = start;
    while i < bytes.len() {
        let b = bytes[i];
        match b {
            b'\'' if !in_double && !in_backtick => in_single = !in_single,
            b'"' if !in_single && !in_backtick => in_double = !in_double,
            b'`' if !in_single && !in_double => in_backtick = !in_backtick,
            b'(' if !in_single && !in_double && !in_backtick => depth += 1,
            b')' if !in_single && !in_double && !in_backtick && depth > 0 => depth -= 1,
            _ => {}
        }
        if depth == 0
            && !in_single
            && !in_double
            && !in_backtick
            && lower[i..].starts_with(&needle)
            && keyword_boundary(&lower, i, i + needle.len())
        {
            return Some(i);
        }
        i += 1;
    }
    None
}

fn keyword_boundary(lower: &str, start: usize, end: usize) -> bool {
    let before = lower[..start]
        .chars()
        .next_back()
        .map_or(true, |c| !c.is_ascii_alphanumeric() && c != '_');
    let after = lower[end..]
        .chars()
        .next()
        .map_or(true, |c| !c.is_ascii_alphanumeric() && c != '_');
    before && after
}

fn split_top_level_commas(text: &str) -> Vec<String> {
    let mut items = Vec::new();
    let mut start = 0;
    let mut depth = 0_i32;
    let mut in_single = false;
    let mut in_double = false;
    let mut in_backtick = false;
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'\'' if !in_double && !in_backtick => in_single = !in_single,
            b'"' if !in_single && !in_backtick => in_double = !in_double,
            b'`' if !in_single && !in_double => in_backtick = !in_backtick,
            b'(' if !in_single && !in_double && !in_backtick => depth += 1,
            b')' if !in_single && !in_double && !in_backtick && depth > 0 => depth -= 1,
            b',' if depth == 0 && !in_single && !in_double && !in_backtick => {
                items.push(text[start..i].trim().to_string());
                start = i + 1;
            }
            _ => {}
        }
        i += 1;
    }
    items.push(text[start..].trim().to_string());
    items
}

fn select_item_label(expression: &str) -> String {
    if let Some(alias) = select_item_alias(expression) {
        return alias;
    }
    let without_quotes = expression.trim().trim_matches('"').trim_matches('`');
    let tail = without_quotes
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(without_quotes);
    tail.trim_matches('"')
        .trim_matches('`')
        .trim_matches(')')
        .trim()
        .to_string()
}

fn select_item_alias(expression: &str) -> Option<String> {
    let lower = expression.to_ascii_lowercase();
    let mut found = None;
    let mut search_start = 0;
    while let Some(offset) = lower[search_start..].find(" as ") {
        let idx = search_start + offset;
        found = Some(idx + " as ".len());
        search_start = idx + " as ".len();
    }
    let alias_start = found?;
    let alias = expression[alias_start..]
        .trim()
        .trim_matches('"')
        .trim_matches('`')
        .trim();
    if alias.is_empty() {
        None
    } else {
        Some(alias.to_string())
    }
}

fn looks_like_aggregate_expr(lower: &str) -> bool {
    ["count(", "sum(", "avg(", "min(", "max("]
        .iter()
        .any(|needle| lower.contains(needle))
}

fn looks_like_time_dimension(lower: &str) -> bool {
    if lower.contains("strftime") {
        return true;
    }
    let tokens = shape_tokens(lower);
    [
        "date",
        "month",
        "year",
        "week",
        "day",
        "created",
        "updated",
        "resolved",
        "closed",
        "opened",
        "on",
        "at",
        "timestamp",
    ]
    .iter()
    .any(|needle| tokens.contains(needle))
}

fn looks_like_numeric_measure(lower: &str) -> bool {
    let tokens = shape_tokens(lower);
    [
        "count", "sum", "avg", "average", "amount", "total", "revenue", "price", "cost", "score",
        "rate", "ratio", "percent", "hours", "duration", "quantity",
    ]
    .iter()
    .any(|needle| tokens.contains(needle))
}

fn shape_tokens(text: &str) -> Vec<&str> {
    text.split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|token| !token.is_empty())
        .collect()
}

fn sql_has_keyword(sql: &str, keyword: &str) -> bool {
    find_top_level_keyword_after(sql, keyword, 0).is_some()
}

fn rejected_candidates_for_decision(
    decision: &semsql_runtime::stage_slotfiller::SlotDecision,
    pre_slot: Option<&semsql_runtime::QueryFrameSlot>,
) -> Vec<serde_json::Value> {
    let Some(pre_slot) = pre_slot else {
        return Vec::new();
    };
    let scored: BTreeSet<&str> = decision
        .candidates
        .iter()
        .map(|candidate| candidate.value.as_str())
        .collect();
    pre_slot
        .candidates
        .iter()
        .enumerate()
        .filter(|(_, candidate)| !scored.contains(candidate.value.as_str()))
        .map(|(idx, candidate)| {
            serde_json::json!({
                "value": &candidate.value,
                "source_fields": &candidate.source_fields,
                "original_index": idx,
                "reason": rejected_candidate_reason(decision, candidate, idx),
            })
        })
        .collect()
}

fn selected_query_frame_bindings(out: &semsql_runtime::CascadeOutcome) -> Vec<serde_json::Value> {
    let Some(frame) = out.query_frame.as_ref() else {
        return Vec::new();
    };
    let decisions_by_slot: BTreeMap<&str, &semsql_runtime::stage_slotfiller::SlotDecision> = out
        .slot_decisions
        .iter()
        .map(|decision| (decision.slot_name.as_str(), decision))
        .collect();

    frame
        .bindings
        .iter()
        .filter_map(|binding| {
            let value_decision = decisions_by_slot.get(binding.slot.as_str())?;
            if !matches!(
                value_decision.slot_role.as_str(),
                "predicate_value" | "limit_value"
            ) {
                return None;
            }
            let selected_field = value_decision.predicate_field.as_deref().or_else(|| {
                binding
                    .predicate_field_slot
                    .as_deref()
                    .and_then(|field_slot| decisions_by_slot.get(field_slot))
                    .and_then(|field_decision| field_decision.picked.as_deref())
            });
            let scored_candidate = value_decision
                .candidates
                .iter()
                .find(|candidate| candidate.value == binding.candidate_value)?;
            let source_fields = if scored_candidate.source_fields.is_empty() {
                binding.candidate_source_fields.clone()
            } else {
                scored_candidate.source_fields.clone()
            };
            if !selected_binding_allowed(binding, selected_field, &source_fields) {
                return None;
            }
            let picked = value_decision
                .picked
                .as_deref()
                .is_some_and(|picked| picked == binding.candidate_value);
            Some(serde_json::json!({
                "mention_kind": &binding.mention_kind,
                "mention_text": &binding.mention_text,
                "mention_normalized": &binding.mention_normalized,
                "slot": &binding.slot,
                "slot_role": &binding.slot_role,
                "selected_predicate_field": selected_field,
                "predicate_field_slot": &binding.predicate_field_slot,
                "predicate_operator": &value_decision.predicate_operator,
                "candidate_value": &binding.candidate_value,
                "candidate_source_fields": source_fields,
                "match_kind": &binding.match_kind,
                "confidence": binding.confidence,
                "score": scored_candidate.score,
                "biased_score": scored_candidate.biased_score,
                "picked": picked,
            }))
        })
        .collect()
}

fn selected_binding_allowed(
    binding: &semsql_runtime::QueryFrameBinding,
    selected_field: Option<&str>,
    source_fields: &[String],
) -> bool {
    if !source_fields.is_empty() {
        return match selected_field {
            Some(field) => {
                source_fields
                    .iter()
                    .any(|source| source.eq_ignore_ascii_case(field))
                    || field_tail_compatible_with_mention(&binding.mention_kind, field)
            }
            None => true,
        };
    }
    if binding.slot_role == "limit_value" {
        return binding.mention_kind == "number";
    }
    let Some(field) = selected_field else {
        return false;
    };
    source_free_binding_field_compatible(&binding.mention_kind, field)
}

fn source_free_binding_field_compatible(kind: &str, field: &str) -> bool {
    matches!(kind, "date" | "number" | "percentage" | "range")
        && field_tail_compatible_with_mention(kind, field)
}

fn field_tail_compatible_with_mention(kind: &str, field: &str) -> bool {
    let lower = field.to_ascii_lowercase();
    let tail = lower
        .rsplit_once('.')
        .map(|(_, tail)| tail)
        .unwrap_or(&lower);
    match kind {
        "zip" => tail.contains("zip") || tail.contains("postal"),
        "code" => [
            "code",
            "fips",
            "id",
            "identifier",
            "nces",
            "num",
            "number",
            "status",
            "type",
        ]
        .iter()
        .any(|needle| tail.contains(needle)),
        "date" => [
            "birth", "closed", "date", "day", "month", "open", "time", "year",
        ]
        .iter()
        .any(|needle| tail.contains(needle)),
        "percentage" => [
            "eligible",
            "pct",
            "percent",
            "percentage",
            "rate",
            "ratio",
            "share",
        ]
        .iter()
        .any(|needle| tail.contains(needle)),
        "number" | "range" => [
            "age",
            "amount",
            "avg",
            "average",
            "count",
            "eligible",
            "enrollment",
            "num",
            "number",
            "pct",
            "percent",
            "price",
            "rate",
            "ratio",
            "salary",
            "score",
            "sum",
            "total",
            "year",
        ]
        .iter()
        .any(|needle| tail.contains(needle)),
        "boolean" => {
            tail.ends_with("_y_n")
                || tail.ends_with("_yn")
                || tail.starts_with("is_")
                || tail.starts_with("has_")
                || tail.ends_with("_flag")
                || tail == "active"
        }
        "enum_keyword" => ["category", "grade", "status", "type", "virtual"]
            .iter()
            .any(|needle| tail.contains(needle)),
        "phrase" | "quoted_string" => [
            "admin", "agency", "city", "county", "district", "name", "owner", "state", "title",
        ]
        .iter()
        .any(|needle| tail.contains(needle)),
        _ => false,
    }
}

fn rejected_candidate_reason(
    decision: &semsql_runtime::stage_slotfiller::SlotDecision,
    candidate: &semsql_runtime::QueryFrameCandidate,
    original_index: usize,
) -> &'static str {
    if decision.slot_kind == "value" {
        if let Some(predicate_field) = decision.predicate_field.as_deref() {
            if !candidate.source_fields.is_empty()
                && !candidate
                    .source_fields
                    .iter()
                    .any(|source| source.eq_ignore_ascii_case(predicate_field))
            {
                return "predicate_value_source_mismatch";
            }
        }
        if original_index >= 32 {
            return "value_scoring_cap_or_late_candidate";
        }
        return "value_filter_before_scoring";
    }
    if decision.slot_kind == "entity" {
        return "entity_reuse_filter";
    }
    if decision.slot_kind == "field" && decision.slot_role == "join_key" {
        return "join_key_reuse_filter";
    }
    if decision.slot_kind == "field" {
        return "field_filter_or_reorder_before_scoring";
    }
    "not_scored_by_stage3"
}

fn renderability_diagnostics(final_sql: &str) -> serde_json::Value {
    let unresolved_placeholders = unresolved_placeholders(final_sql);
    if !unresolved_placeholders.is_empty() {
        return serde_json::json!({
            "sql_surface_valid": false,
            "plain_select": false,
            "error": "unresolved_placeholders",
            "unresolved_placeholders": unresolved_placeholders,
        });
    }
    match semsql_natsql::validate_select_sql_surface(final_sql) {
        Ok(()) => serde_json::json!({
            "sql_surface_valid": true,
            "plain_select": true,
            "error": serde_json::Value::Null,
            "unresolved_placeholders": [],
        }),
        Err(err) => serde_json::json!({
            "sql_surface_valid": false,
            "plain_select": false,
            "error": err.to_string(),
            "unresolved_placeholders": [],
        }),
    }
}

fn unresolved_placeholders(sql: &str) -> Vec<String> {
    let bytes = sql.as_bytes();
    let mut placeholders = BTreeSet::new();
    let mut i = 0usize;
    let mut quote: Option<u8> = None;
    while i < bytes.len() {
        if let Some(active_quote) = quote {
            if bytes[i] == active_quote {
                if active_quote == b'\'' && bytes.get(i + 1) == Some(&b'\'') {
                    i += 2;
                    continue;
                }
                quote = None;
            }
            i += 1;
            continue;
        }
        if matches!(bytes[i], b'\'' | b'"' | b'`') {
            quote = Some(bytes[i]);
            i += 1;
            continue;
        }
        if bytes[i] == b'@' {
            let start = i;
            i += 1;
            while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
                i += 1;
            }
            if i > start + 1 {
                placeholders.insert(sql[start..i].to_string());
            }
        } else {
            i += 1;
        }
    }
    placeholders.into_iter().collect()
}

#[derive(serde::Deserialize)]
struct OracleSchemaSlice {
    #[serde(default)]
    entities: Vec<String>,
    #[serde(default)]
    fields: Vec<String>,
    #[serde(default)]
    top_score: Option<f32>,
    #[serde(default)]
    ranked_schema: Vec<OracleSchemaItem>,
}

#[derive(serde::Deserialize)]
struct OracleSchemaItem {
    kind: String,
    target: String,
    #[serde(default)]
    score: Option<f32>,
}

fn parse_oracle_schema_json(raw: &str) -> Result<semsql_runtime::stage_linker::LinkerOutput> {
    let value: serde_json::Value =
        serde_json::from_str(raw).context("parsing --oracle-schema-json")?;
    let mut out = semsql_runtime::stage_linker::LinkerOutput::default();
    let mut observed_top_score: Option<f32> = None;
    let top_score_override;

    if value.is_array() {
        let items: Vec<OracleSchemaItem> =
            serde_json::from_value(value).context("parsing oracle ranked schema array")?;
        top_score_override = None;
        for item in items {
            apply_oracle_schema_item(&mut out, item, &mut observed_top_score);
        }
    } else {
        let slice: OracleSchemaSlice =
            serde_json::from_value(value).context("parsing oracle schema object")?;
        top_score_override = slice.top_score;
        for entity in slice.entities {
            push_unique(&mut out.top_entities, entity);
        }
        for field in slice.fields {
            push_unique(&mut out.top_fields, field);
        }
        for item in slice.ranked_schema {
            apply_oracle_schema_item(&mut out, item, &mut observed_top_score);
        }
    }

    if out.top_entities.is_empty() && out.top_fields.is_empty() {
        anyhow::bail!("--oracle-schema-json did not contain any entity or field targets");
    }
    out.top_score = top_score_override
        .or(observed_top_score)
        .unwrap_or(1.0)
        .clamp(0.0, 1.0);
    Ok(out)
}

fn parse_oracle_slots_json(raw: &str) -> Result<std::collections::HashMap<String, String>> {
    let value: serde_json::Value =
        serde_json::from_str(raw).context("parsing --oracle-slots-json")?;
    let object = value
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("--oracle-slots-json must be a JSON object"))?;
    let mut out = std::collections::HashMap::with_capacity(object.len());
    for (slot, value) in object {
        if slot.is_empty() {
            continue;
        }
        let rendered = match value {
            serde_json::Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        out.insert(slot.clone(), rendered);
    }
    if out.is_empty() {
        anyhow::bail!("--oracle-slots-json did not contain any slot mappings");
    }
    Ok(out)
}

fn apply_oracle_schema_item(
    out: &mut semsql_runtime::stage_linker::LinkerOutput,
    item: OracleSchemaItem,
    observed_top_score: &mut Option<f32>,
) {
    if let Some(score) = item.score {
        *observed_top_score = Some(observed_top_score.map_or(score, |current| current.max(score)));
    }
    match item.kind.as_str() {
        "entity" | "table" => push_unique(&mut out.top_entities, item.target),
        "field" | "column" => push_unique(&mut out.top_fields, item.target),
        _ => {}
    }
}

fn push_unique(values: &mut Vec<String>, value: String) {
    if !value.is_empty() && !values.contains(&value) {
        values.push(value);
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

#[allow(clippy::too_many_arguments)]
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
        other => anyhow::bail!("unknown --format `{other}` — supported: text, json"),
    }
    let cov = semsql_graph::read::coverage(graph)
        .with_context(|| format!("reading coverage from {}", graph.display()))?;
    let conflicts = semsql_graph::read::conflicts(graph)
        .with_context(|| format!("reading conflict_log from {}", graph.display()))?;

    println!("graph: {}", graph.display());
    println!(
        "  entities={}  fields={}  relationships={}  samples={}  metrics={}  vocab={}  enums={}  scopes={}",
        cov.entity_count,
        cov.field_count,
        cov.relationship_count,
        cov.sample_value_count,
        cov.metric_definition_count,
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
        println!("  (mandatory-filter injection is belt-and-suspenders, not a substitute for RLS)");

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
                    if cov.scoped_entities.len() == 1 {
                        "y"
                    } else {
                        "ies"
                    },
                );
            } else {
                println!("rls-strict: {rls_problems} tenanted table(s) failed RLS verification.");
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
#[allow(unused_mut, unused_variables)]
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
            "schema_version": report.schema_version,
            "summary": {
                "suite": report.summary.suite,
                "total": report.summary.total,
                "correct": report.summary.correct,
                "bailed": report.summary.bailed,
                "errored": report.summary.errored,
                "timeouts": report.summary.timeouts,
                "exec_acc": report.summary.exec_acc,
                "bail_rate": report.summary.bail_rate,
                "stage_breakdown": report.summary.stage_breakdown,
                "failure_buckets": report.summary.failure_buckets,
                "gate": eval_gate_status(&report),
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
            "relationships": cov.relationship_count,
            "sample_values": cov.sample_value_count,
            "metric_definitions": cov.metric_definition_count,
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
    #[serde(default)]
    schema_version: Option<u64>,
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
    #[serde(default)]
    exec_equal: Option<bool>,
    #[serde(default)]
    failure_bucket: String,
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
    timeouts: u64,
    #[serde(default)]
    stage_breakdown: std::collections::BTreeMap<String, u64>,
    #[serde(default)]
    failure_buckets: std::collections::BTreeMap<String, u64>,
}

/// Print the per-stage breakdown from a `--report-json` artifact and
/// return the number of deployment-blocking problems detected.
///
/// Today: cascade coverage below [`CASCADE_COVERAGE_WARN_THRESHOLD`]
/// counts as one problem (lifts the doctor exit code so CI catches a
/// regression before promotion). The summary print path is always
/// rendered — operators want to see the breakdown even on green runs.
fn render_eval_report(path: &std::path::Path) -> Result<usize> {
    let bytes =
        std::fs::read(path).with_context(|| format!("reading eval report `{}`", path.display()))?;
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
    let bytes =
        std::fs::read(path).with_context(|| format!("reading eval report `{}`", path.display()))?;
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
            let exec_equal = r.exec_equal.unwrap_or_else(|| {
                !r.gold_sql.is_empty()
                    && !r.pred_sql.is_empty()
                    && r.gold_sql.trim() == r.pred_sql.trim()
            });
            if exec_equal && (r.failure_bucket.is_empty() || r.failure_bucket == "correct") {
                return false;
            }
            if !r.failure_bucket.is_empty() && r.failure_bucket != "correct" {
                return true;
            }
            if !exec_equal {
                return true;
            }
            matches!(
                r.stage_pinned.as_str(),
                "needs_model"
                    | "error"
                    | "timeout"
                    | "unknown"
                    | "stage2_constraint_error"
                    | "stage2_structural_error"
                    | "stage4_render_error"
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
    println!(
        "eval drilldown — first {} non-correct example(s):",
        interesting.len()
    );
    for (i, r) in interesting.iter().enumerate() {
        println!(
            "  [{i:>2}] db={} stage={} bucket={} q={:?}",
            r.db_id,
            r.stage_pinned,
            if r.failure_bucket.is_empty() {
                "unknown"
            } else {
                &r.failure_bucket
            },
            r.question
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
    out.push('…');
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
            let _ = writeln!(buf, "    candidates: {}", yaml_escape(&c.candidates_json));
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
    std::fs::write(out_path, buf).with_context(|| format!("writing {}", out_path.display()))?;
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

#[derive(serde::Serialize)]
struct EvalGateStatus {
    profile: &'static str,
    status: &'static str,
    reasons: Vec<String>,
}

fn eval_gate_status(report: &EvalReport) -> Option<EvalGateStatus> {
    let suite = report.summary.suite.as_deref()?;
    if suite != "bird" {
        return None;
    }
    let mut reasons = Vec::new();
    if report.summary.total < 1534 {
        reasons.push(format!(
            "BIRD smoke/canary only: total {} is below full dev size 1534",
            report.summary.total
        ));
        return Some(EvalGateStatus {
            profile: "v0.2-bird",
            status: "smoke_only",
            reasons,
        });
    }
    if report.summary.exec_acc < 0.35 {
        reasons.push(format!(
            "exec_acc {:.3}% is below 35.000%",
            report.summary.exec_acc * 100.0
        ));
    }
    if report.summary.errored > 0 {
        reasons.push(format!("{} example(s) errored", report.summary.errored));
    }
    if report.summary.timeouts > 0 {
        reasons.push(format!("{} example(s) timed out", report.summary.timeouts));
    }
    Some(EvalGateStatus {
        profile: "v0.2-bird",
        status: if reasons.is_empty() {
            "passed"
        } else {
            "failed"
        },
        reasons,
    })
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
    if let Some(version) = report.schema_version {
        let _ = writeln!(out, "  schema_version={version}");
    }
    let _ = writeln!(
        out,
        "  suite={}  total={}  correct={}  bailed={}  errored={}  timeouts={}  exec_acc={:.3}  bail_rate={:.3}",
        report.summary.suite.as_deref().unwrap_or("?"),
        total,
        report.summary.correct,
        report.summary.bailed,
        report.summary.errored,
        report.summary.timeouts,
        report.summary.exec_acc,
        report.summary.bail_rate,
    );

    if report.summary.stage_breakdown.is_empty() {
        let _ = writeln!(
            out,
            "  (no stage_breakdown — eval pre-dates per-stage telemetry)"
        );
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

    if !report.summary.failure_buckets.is_empty() {
        let _ = writeln!(out, "  failure_buckets:");
        for (bucket, count) in &report.summary.failure_buckets {
            let pct = if total > 0 {
                (*count as f64 / total as f64) * 100.0
            } else {
                0.0
            };
            let _ = writeln!(out, "    {bucket:<24} {count:>6}  ({pct:>5.1}%)");
        }
    }

    if let Some(gate) = eval_gate_status(report) {
        let _ = writeln!(out, "  gate:");
        let _ = writeln!(out, "    profile: {}", gate.profile);
        let _ = writeln!(out, "    status: {}", gate.status);
        for reason in gate.reasons {
            let _ = writeln!(out, "    - {reason}");
        }
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
        let errors = report.summary.errored
            + report.summary.timeouts
            + report
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
             Rebuild with `cargo build -p semsql-cli --features semsql-cli/onnx` to \
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

    #[tokio::test]
    async fn framework_extract_runs_source_extractor_and_ingests_vocab() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("app.sqlite");
        let conn = rusqlite::Connection::open(&db).unwrap();
        conn.execute_batch("CREATE TABLE users (id INTEGER PRIMARY KEY);")
            .unwrap();
        drop(conn);

        let out = dir.path().join("app.semsql");
        let url = format!("sqlite:{}", db.display());
        let called = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let called_in_runner = std::sync::Arc::clone(&called);
        let root_path = dir.path().to_path_buf();
        cmd_extract_with_source_runner(
            ExtractCommandOptions {
                path: dir.path(),
                framework: "laravel",
                output: &out,
                db_url: Some(&url),
                vocab_jsonl: None,
                sample_values: false,
                schema_description_dir: None,
            },
            move |project, framework, jsonl| {
                called_in_runner.store(true, std::sync::atomic::Ordering::SeqCst);
                assert_eq!(project, root_path.as_path());
                assert_eq!(framework, "laravel");
                std::fs::write(
                    jsonl,
                    r#"{"term":"students","canonical":{"kind":"entity","entity":"users"},"confidence":0.96,"locator":{"file":"app/Filament/Resources/StudentResource.php","line":4,"layer":6,"extractor":"extractor-laravel:filament"}}
"#,
                )
                .unwrap();
                count_jsonl_records(jsonl)
            },
        )
        .await
        .unwrap();

        assert!(called.load(std::sync::atomic::Ordering::SeqCst));
        let cascade = semsql_runtime::Cascade::load(&out, None).unwrap();
        let outcome = cascade.run("how many students").unwrap();
        assert_eq!(outcome.sql_text, "SELECT COUNT(*) FROM users");
    }

    #[tokio::test]
    async fn framework_extract_ingests_authored_metrics_into_rejection_packets() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("app.sqlite");
        let conn = rusqlite::Connection::open(&db).unwrap();
        conn.execute_batch(
            "CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                status_code INTEGER NOT NULL
            );",
        )
        .unwrap();
        drop(conn);

        let out = dir.path().join("app.semsql");
        let url = format!("sqlite:{}", db.display());
        cmd_extract_with_source_runner(
            ExtractCommandOptions {
                path: dir.path(),
                framework: "laravel",
                output: &out,
                db_url: Some(&url),
                vocab_jsonl: None,
                sample_values: false,
                schema_description_dir: None,
            },
            |_project, _framework, jsonl| {
                std::fs::write(
                    jsonl,
                    r#"{"record_kind":"metric_definition","name":"active_user_rate","displayLabel":"Active user rate","metricKind":"conditional_rate","subjectEntity":"user","numeratorField":"user.status_code","numeratorOperator":"=","numeratorValue":"2","numeratorValueKind":"value_dictionary","denominatorField":"user.id","scale":100,"requiredEntities":["user"],"aliases":["active user rate"],"locator":{"file":"semsql.metrics.json","line":1,"layer":3,"extractor":"semsql:metrics"}}
"#,
                )
                .unwrap();
                count_jsonl_records(jsonl)
            },
        )
        .await
        .unwrap();

        let metrics = semsql_graph::read::metric_definitions(&out).unwrap();
        assert_eq!(metrics.len(), 1);
        assert_eq!(metrics[0].name, "active_user_rate");
        assert_eq!(metrics[0].subject_entity, "users");
        assert_eq!(metrics[0].numerator_field, "users.status_code");
        assert_eq!(metrics[0].denominator_field, "users.id");

        let packet = rejected_query_packet_payload(
            &out,
            "active user rate by status",
            "needs_model",
            serde_json::json!({"source": "query_frame_error"}),
            false,
        )
        .unwrap();
        assert_eq!(
            packet["schema_card"]["summary"]["metric_definition_count"],
            serde_json::json!(1)
        );
        assert_eq!(
            packet["local_candidates"]["metric_catalog_ambiguous"],
            serde_json::json!(false)
        );
        assert_eq!(
            packet["local_candidates"]["metric_catalog_hits"][0]["name"],
            serde_json::json!("active_user_rate")
        );
        assert_eq!(
            packet["local_candidates"]["metric_catalog_hits"][0]["numerator_field"],
            serde_json::json!("users.status_code")
        );
    }

    #[tokio::test]
    async fn framework_extract_requires_db_url() {
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("app.semsql");
        let err = cmd_extract_with_source_runner(
            ExtractCommandOptions {
                path: dir.path(),
                framework: "laravel",
                output: &out,
                db_url: None,
                vocab_jsonl: None,
                sample_values: false,
                schema_description_dir: None,
            },
            |_project, _framework, _jsonl| unreachable!("no source extraction without DB URL"),
        )
        .await
        .unwrap_err();

        assert!(err
            .to_string()
            .contains("--framework=laravel requires --db-url"));
        assert!(!out.exists());
    }

    #[test]
    fn normalize_framework_accepts_next_aliases() {
        assert_eq!(normalize_framework_name("Next.js"), "nextjs");
        assert_eq!(normalize_framework_name("next-js"), "nextjs");
        assert_eq!(normalize_framework_name("AUTO"), "auto");
    }

    #[test]
    fn workspace_extractor_lookup_can_be_disabled_for_package_probe() {
        let script =
            workspace_extractor_cli_script_from(Path::new(env!("CARGO_MANIFEST_DIR")), true);
        assert!(script.is_none());
    }

    #[test]
    fn path_lookup_finds_npm_cmd_shim() {
        let dir = tempfile::tempdir().unwrap();
        let shim = dir.path().join("semsql-extract.cmd");
        std::fs::write(&shim, "@echo off\r\n").unwrap();
        let found = find_command_on_path_with_env(
            "semsql-extract",
            Some(dir.path().as_os_str().to_owned()),
            Some(OsString::from(".cmd;.exe")),
            true,
        )
        .unwrap();
        assert_eq!(found, shim);
    }

    #[test]
    fn path_lookup_prefers_windows_shim_over_posix_script() {
        let dir = tempfile::tempdir().unwrap();
        let posix = dir.path().join("semsql-extract");
        let shim = dir.path().join("semsql-extract.cmd");
        std::fs::write(&posix, "#!/bin/sh\n").unwrap();
        std::fs::write(&shim, "@echo off\r\n").unwrap();
        let found = find_command_on_path_with_env(
            "semsql-extract",
            Some(dir.path().as_os_str().to_owned()),
            Some(OsString::from(".cmd;.exe")),
            true,
        )
        .unwrap();
        assert_eq!(found, shim);
    }

    #[test]
    fn command_script_detection_is_case_insensitive() {
        assert!(is_windows_command_script(Path::new("semsql-extract.CMD")));
        assert!(is_windows_command_script(Path::new("semsql-extract.bat")));
        assert!(!is_windows_command_script(Path::new("semsql-extract.js")));
    }

    fn report(stage_breakdown: &[(&str, u64)], total: u64, correct: u64) -> EvalReport {
        EvalReport {
            schema_version: None,
            examples: Vec::new(),
            summary: EvalReportSummary {
                suite: Some("spider".into()),
                total,
                correct,
                bailed: total.saturating_sub(correct),
                errored: 0,
                timeouts: 0,
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
                failure_buckets: std::collections::BTreeMap::new(),
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

    fn write_rejection_packet_test_graph(path: &std::path::Path) {
        let conn = semsql_graph::open(path).unwrap();
        conn.execute(
            "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label, proto_blob) \
             VALUES ('orders', 'orders', 'order', 'orders', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label, proto_blob) \
             VALUES ('customers', 'customers', 'customer', 'customers', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
             VALUES ('orders', 'id', 'id', 'integer', 'ID', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
             VALUES ('orders', 'status', 'status', 'text', 'Status', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
             VALUES ('orders', 'amount', 'amount', 'real', 'Amount', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
             VALUES ('orders', 'customer_id', 'customer_id', 'integer', 'Customer ID', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
             VALUES ('customers', 'id', 'id', 'integer', 'ID', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
             VALUES ('customers', 'name', 'name', 'text', 'Name', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind, proto_blob) \
             VALUES ('orders', 'customer_id', 'customers', 'id', 'many_to_one', X'')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO sample_values(field_canonical, examples, pii_redacted) \
             VALUES ('orders.status', '[\"shipped\", \"draft\"]', 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) \
             VALUES ('shipped', 'scope_predicate', ?1, 0.88, 2)",
            [serde_json::json!({
                "scope": "orders.status.shipped",
                "field": "orders.status",
                "operator": "=",
                "rawValue": "shipped",
            })
            .to_string()],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO metric_definitions(\
             name, display_label, metric_kind, subject_entity, numerator_field, \
             numerator_operator, numerator_value, numerator_value_kind, \
             denominator_field, scale, required_entities_json, aliases_json\
             ) VALUES (\
             'shipped_order_rate', 'Shipped order rate', 'conditional_rate', \
             'orders', 'orders.status', '=', 'shipped', 'value_dictionary', \
             'orders.id', 100.0, '[\"orders\"]', '[\"shipped order rate\"]')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO metric_definitions(\
             name, display_label, metric_kind, subject_entity, numerator_field, \
             numerator_operator, numerator_value, numerator_value_kind, \
             denominator_field, scale, required_entities_json, aliases_json, \
             measure_field, aggregate\
             ) VALUES (\
             'average_order_amount', 'Average order amount', 'aggregate', \
             'orders', 'orders.amount', '=', '', 'metric_definition', \
             'orders.id', 1.0, '[\"orders\"]', '[\"average order amount\"]', \
             'orders.amount', 'AVG')",
            [],
        )
        .unwrap();
    }

    fn write_sharded_rejection_packet_test_graph(path: &std::path::Path) {
        let conn = semsql_graph::open(path).unwrap();
        for entity in ["mails", "mails_organizations_1", "mails_organizations_2"] {
            conn.execute(
                "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label, proto_blob) \
                 VALUES (?1, ?1, ?1, ?1, X'')",
                [entity],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
                 VALUES (?1, 'id', 'id', 'integer', 'ID', X'')",
                [entity],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO fields(entity, field, db_column, type, display_label, proto_blob) \
                 VALUES (?1, 'status', 'status', 'text', 'Status', X'')",
                [entity],
            )
            .unwrap();
        }
    }

    #[test]
    fn rejection_packet_payload_omits_samples_by_default_and_keeps_contract() {
        let dir = tempfile::tempdir().unwrap();
        let graph = dir.path().join("graph.semsql");
        write_rejection_packet_test_graph(&graph);

        let packet = rejected_query_packet_payload(
            &graph,
            "show shipped orders",
            "needs_model",
            serde_json::json!({"source": "query_frame_error"}),
            false,
        )
        .unwrap();

        assert_eq!(packet["source"], "semsql_rejected_query_packet");
        assert_eq!(packet["route_reason"], "needs_model");
        assert_eq!(
            packet["allowed_resolution_contract"]["must_not_emit_final_sql"],
            serde_json::json!(true)
        );
        assert_eq!(
            packet["schema_card"]["summary"]["sample_values_included"],
            serde_json::json!(false)
        );
        assert_eq!(
            packet["schema_card"]["safety"]["llm_may_not_execute_sql"],
            serde_json::json!(true)
        );
        let entities = packet["schema_card"]["entities"].as_array().unwrap();
        let orders = entities
            .iter()
            .find(|entity| entity["name"] == serde_json::json!("orders"))
            .unwrap();
        let status = orders["fields"]
            .as_array()
            .unwrap()
            .iter()
            .find(|field| field["name"] == serde_json::json!("status"))
            .unwrap();
        assert_eq!(status["role"], serde_json::json!("status"));
        assert_eq!(status["samples"], serde_json::json!([]));
        assert_eq!(
            status["value_dictionary"][0]["raw_value"],
            serde_json::json!("shipped")
        );
        assert_eq!(
            packet["local_candidates"]["value_dictionary_hits"][0]["field"],
            serde_json::json!("orders.status")
        );
        assert_eq!(
            packet["schema_card"]["summary"]["metric_definition_count"],
            serde_json::json!(2)
        );

        let metric_packet = rejected_query_packet_payload(
            &graph,
            "shipped order rate by customer",
            "needs_model",
            serde_json::json!({"source": "query_frame_error"}),
            false,
        )
        .unwrap();
        assert_eq!(
            metric_packet["local_candidates"]["metric_catalog_ambiguous"],
            serde_json::json!(false)
        );
        assert_eq!(
            metric_packet["local_candidates"]["metric_catalog_hits"][0]["name"],
            serde_json::json!("shipped_order_rate")
        );
        assert_eq!(
            metric_packet["local_candidates"]["metric_catalog_hits"][0]["numerator_field"],
            serde_json::json!("orders.status")
        );

        let aggregate_packet = rejected_query_packet_payload(
            &graph,
            "average order amount by customer",
            "needs_model",
            serde_json::json!({"source": "query_frame_error"}),
            false,
        )
        .unwrap();
        assert_eq!(
            aggregate_packet["local_candidates"]["metric_catalog_ambiguous"],
            serde_json::json!(false)
        );
        assert_eq!(
            aggregate_packet["local_candidates"]["metric_catalog_hits"][0]["name"],
            serde_json::json!("average_order_amount")
        );
        assert_eq!(
            aggregate_packet["local_candidates"]["metric_catalog_hits"][0]["metric_kind"],
            serde_json::json!("aggregate")
        );
        assert_eq!(
            aggregate_packet["local_candidates"]["metric_catalog_hits"][0]["measure_field"],
            serde_json::json!("orders.amount")
        );
        assert_eq!(
            aggregate_packet["local_candidates"]["metric_catalog_hits"][0]["aggregate"],
            serde_json::json!("AVG")
        );

        let packet_with_samples = rejected_query_packet_payload(
            &graph,
            "show shipped orders",
            "needs_model",
            serde_json::json!({"source": "query_frame_error"}),
            true,
        )
        .unwrap();
        let orders = packet_with_samples["schema_card"]["entities"]
            .as_array()
            .unwrap()
            .iter()
            .find(|entity| entity["name"] == serde_json::json!("orders"))
            .unwrap();
        let status = orders["fields"]
            .as_array()
            .unwrap()
            .iter()
            .find(|field| field["name"] == serde_json::json!("status"))
            .unwrap();
        assert_eq!(status["samples"], serde_json::json!(["shipped", "draft"]));
    }

    #[test]
    fn rejection_packet_payload_surfaces_physical_table_family_ambiguity() {
        let dir = tempfile::tempdir().unwrap();
        let graph = dir.path().join("sharded.semsql");
        write_sharded_rejection_packet_test_graph(&graph);

        let packet = rejected_query_packet_payload(
            &graph,
            "how many mails have status sent",
            "not_routed_ambiguous_physical_table_family",
            serde_json::json!({"source": "query_frame_error"}),
            false,
        )
        .unwrap();

        assert_eq!(
            packet["schema_card"]["summary"]["ambiguous_physical_family_count"],
            serde_json::json!(1)
        );
        let family = &packet["schema_card"]["physical_table_families"][0];
        assert_eq!(family["base_table"], serde_json::json!("mails"));
        assert_eq!(family["anchor"], serde_json::json!("organizations"));
        assert_eq!(family["member_count"], serde_json::json!(3));
        assert_eq!(family["requires_clarification"], serde_json::json!(true));
        assert!(family["members"]
            .as_array()
            .unwrap()
            .iter()
            .any(|member| member["entity"] == serde_json::json!("mails")
                && member["role"] == serde_json::json!("base_table")));

        let mention = &packet["local_candidates"]["ambiguous_physical_families_mentioned"][0];
        assert_eq!(mention["base_table"], serde_json::json!("mails"));
        assert_eq!(mention["requires_clarification"], serde_json::json!(true));
        assert_eq!(
            mention["resolution_hint"],
            serde_json::json!(
                "do not pick a physical partition from the base word alone; use app metadata, a table catalog, or ask which partition/scope is intended"
            )
        );
    }

    #[test]
    fn query_rejection_packet_option_writes_only_on_fail_closed() {
        let dir = tempfile::tempdir().unwrap();
        let graph = dir.path().join("graph.semsql");
        let packet_path = dir.path().join("rejected.packet.json");
        write_rejection_packet_test_graph(&graph);

        let err = cmd_query(QueryArgs {
            graph: &graph,
            nl: "which customer is healthiest?",
            dialect: Some("sqlite"),
            cascade_manifest: None,
            intent_yaml: None,
            oracle_skeleton: None,
            oracle_schema_json: None,
            oracle_slots_json: None,
            query_frame_json: None,
            rejection_packet_json: Some(&packet_path),
            rejection_include_samples: false,
        })
        .unwrap_err();

        assert!(err.to_string().contains("cascade"));
        let packet: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&packet_path).unwrap()).unwrap();
        assert_eq!(packet["source"], "semsql_rejected_query_packet");
        assert_eq!(packet["question"], "which customer is healthiest?");
        assert_eq!(
            packet["query_frame"]["source"],
            serde_json::json!("query_frame_error")
        );
    }

    #[test]
    fn query_frame_diagnostics_cap_large_semantic_atlas_arrays() {
        let mut payload = serde_json::json!({
            "source": "query_frame_error",
            "semantic_atlas": {
                "entity_count": QUERY_FRAME_MAX_ATLAS_ENTITIES + 1,
                "field_count": QUERY_FRAME_MAX_ATLAS_FIELDS + 2,
                "relationship_count": QUERY_FRAME_MAX_ATLAS_RELATIONSHIPS + 3,
                "entities": (0..=QUERY_FRAME_MAX_ATLAS_ENTITIES)
                    .map(|idx| serde_json::json!({"entity": format!("e{idx}")}))
                    .collect::<Vec<_>>(),
                "fields": (0..(QUERY_FRAME_MAX_ATLAS_FIELDS + 2))
                    .map(|idx| serde_json::json!({"field": format!("e.f{idx}")}))
                    .collect::<Vec<_>>(),
                "relationships": (0..(QUERY_FRAME_MAX_ATLAS_RELATIONSHIPS + 3))
                    .map(|idx| serde_json::json!({"from": format!("e{idx}.id")}))
                    .collect::<Vec<_>>(),
                "value_aliases": (0..(QUERY_FRAME_MAX_ATLAS_VALUE_ALIASES + 4))
                    .map(|idx| serde_json::json!({"field": "e.f", "value": format!("v{idx}")}))
                    .collect::<Vec<_>>(),
                "metric_candidates": (0..(QUERY_FRAME_MAX_ATLAS_METRIC_CANDIDATES + 5))
                    .map(|idx| serde_json::json!({"name": format!("m{idx}")}))
                    .collect::<Vec<_>>(),
            },
        });

        cap_query_frame_diagnostic_payload(&mut payload);

        let atlas = &payload["semantic_atlas"];
        assert_eq!(atlas["diagnostic_truncated"], serde_json::json!(true));
        assert_eq!(
            atlas["entities"].as_array().unwrap().len(),
            QUERY_FRAME_MAX_ATLAS_ENTITIES
        );
        assert_eq!(
            atlas["fields"].as_array().unwrap().len(),
            QUERY_FRAME_MAX_ATLAS_FIELDS
        );
        assert_eq!(
            atlas["relationships"].as_array().unwrap().len(),
            QUERY_FRAME_MAX_ATLAS_RELATIONSHIPS
        );
        assert_eq!(
            atlas["value_aliases"].as_array().unwrap().len(),
            QUERY_FRAME_MAX_ATLAS_VALUE_ALIASES
        );
        assert_eq!(
            atlas["metric_candidates"].as_array().unwrap().len(),
            QUERY_FRAME_MAX_ATLAS_METRIC_CANDIDATES
        );
        assert_eq!(
            atlas["diagnostic_truncated_counts"]["entities"],
            serde_json::json!(1)
        );
        assert_eq!(
            atlas["diagnostic_truncated_counts"]["value_aliases"],
            serde_json::json!(4)
        );
        assert_eq!(
            atlas["diagnostic_truncated_counts"]["metric_candidates"],
            serde_json::json!(5)
        );
    }

    #[test]
    fn query_frame_payload_groups_stage3_slots_by_role() {
        let out = semsql_runtime::CascadeOutcome {
            sql_text: "SELECT users.name FROM users WHERE users.status = 'active'".into(),
            timings_us: semsql_runtime::PerStageTimings::default(),
            confidences: semsql_runtime::PerStageConfidence::default(),
            intent_hints: vec!["status".into()],
            stage_pinned: "stage_3".into(),
            repair_attempts: 0,
            query_frame: Some(semsql_runtime::QueryFrameTrace {
                schema_version: 3,
                source: "pre_stage3_slot_inputs".into(),
                question: "active users".into(),
                skeleton: "SELECT @field1 FROM @entity1 WHERE @field2 = @val1".into(),
                linked_entities: vec!["users".into()],
                linked_fields: vec!["users.name".into(), "users.status".into()],
                mentions: Vec::new(),
                bindings: Vec::new(),
                frame: semsql_runtime::QueryFrameParts {
                    projections: vec![semsql_runtime::QueryFrameRoleRef {
                        slot: "@field1".into(),
                        role: "projection_field".into(),
                        ..Default::default()
                    }],
                    ..Default::default()
                },
                slots: vec![semsql_runtime::QueryFrameSlot {
                    slot: "@field1".into(),
                    kind: "field".into(),
                    role: "projection_field".into(),
                    candidates: vec![
                        semsql_runtime::QueryFrameCandidate {
                            value: "users.name".into(),
                            source_fields: Vec::new(),
                        },
                        semsql_runtime::QueryFrameCandidate {
                            value: "users.email".into(),
                            source_fields: Vec::new(),
                        },
                    ],
                    ..Default::default()
                }],
            }),
            runtime_query_frame: None,
            semantic_atlas: None,
            intent_frame: None,
            bound_query_plan: None,
            slot_decisions: vec![
                semsql_runtime::stage_slotfiller::SlotDecision {
                    slot_name: "@field1".into(),
                    slot_kind: "field".into(),
                    slot_role: "projection_field".into(),
                    picked: Some("users.name".into()),
                    picked_index: Some(0),
                    original_candidate_count: 3,
                    candidates: vec![semsql_runtime::stage_slotfiller::SlotDecisionCandidate {
                        value: "users.name".into(),
                        score: 0.8,
                        biased_score: 0.8,
                        source_fields: Vec::new(),
                    }],
                    ..Default::default()
                },
                semsql_runtime::stage_slotfiller::SlotDecision {
                    slot_name: "@val1".into(),
                    slot_kind: "value".into(),
                    slot_role: "predicate_value".into(),
                    predicate_field_slot: Some("@field2".into()),
                    predicate_field: Some("users.status".into()),
                    predicate_operator: Some("=".into()),
                    picked: Some("'active'".into()),
                    picked_index: Some(1),
                    original_candidate_count: 4,
                    candidates: vec![semsql_runtime::stage_slotfiller::SlotDecisionCandidate {
                        value: "'active'".into(),
                        score: 0.7,
                        biased_score: 0.9,
                        source_fields: vec!["users.status".into()],
                    }],
                    ..Default::default()
                },
            ],
        };
        let payload = query_frame_payload("active users", &out, &out.sql_text);
        assert_eq!(payload["schema_version"], serde_json::json!(3));
        assert_eq!(payload["source"], "query_frame");
        assert_eq!(
            payload["pre_stage3"]["frame"]["projections"][0]["slot"],
            serde_json::json!("@field1")
        );
        assert_eq!(
            payload["stage3"]["frame"]["projections"][0]["field"],
            serde_json::json!("users.name")
        );
        assert_eq!(
            payload["stage3"]["frame"]["predicates"][0]["field"],
            serde_json::json!("users.status")
        );
        assert_eq!(
            payload["stage3"]["frame"]["predicates"][0]["value"],
            serde_json::json!("'active'")
        );
        assert_eq!(
            payload["stage3"]["slots"][1]["candidates"][0]["source_fields"][0],
            serde_json::json!("users.status")
        );
        assert_eq!(
            payload["stage3"]["slots"][0]["role"],
            serde_json::json!("projection_field")
        );
        assert_eq!(
            payload["stage3"]["slots"][0]["rejected_candidates"][0]["value"],
            serde_json::json!("users.email")
        );
        assert_eq!(
            payload["diagnostics"]["renderability"]["sql_surface_valid"],
            serde_json::json!(true)
        );
        assert_eq!(
            payload["result_shape"]["kind"],
            serde_json::json!("tabular")
        );
        assert_eq!(
            payload["result_shape"]["default_view"],
            serde_json::json!("table")
        );
    }

    #[test]
    fn result_shape_marks_single_aggregate_as_metric() {
        let shape = result_shape_hint(
            "SELECT COUNT(*) AS active_accounts FROM accounts WHERE status = 'active'",
        );
        assert_eq!(shape["kind"], serde_json::json!("scalar_metric"));
        assert_eq!(shape["default_view"], serde_json::json!("metric"));
        assert_eq!(
            shape["columns"][0]["name"],
            serde_json::json!("active_accounts")
        );
    }

    #[test]
    fn result_shape_maps_grouped_category_measure_to_bar_chart() {
        let shape = result_shape_hint(
            "SELECT regions.name, SUM(invoices.amount) AS total_amount \
             FROM invoices INNER JOIN accounts ON accounts.id = invoices.account_id \
             INNER JOIN regions ON regions.id = accounts.region_id \
             GROUP BY regions.name ORDER BY total_amount DESC",
        );
        assert_eq!(shape["kind"], serde_json::json!("categorical_chart"));
        assert_eq!(shape["chartjs"]["type"], serde_json::json!("bar"));
        assert_eq!(
            shape["chartjs"]["mapping"]["labels_from"],
            serde_json::json!("name")
        );
        assert_eq!(
            shape["chartjs"]["mapping"]["datasets"][0]["data_from"],
            serde_json::json!("total_amount")
        );
    }

    #[test]
    fn result_shape_maps_grouped_time_measure_to_line_chart() {
        let shape = result_shape_hint(
            "SELECT STRFTIME('%Y-%m', accounts.created_at) AS month, COUNT(*) AS account_count \
             FROM accounts GROUP BY STRFTIME('%Y-%m', accounts.created_at) ORDER BY month",
        );
        assert_eq!(shape["kind"], serde_json::json!("time_series_chart"));
        assert_eq!(shape["chartjs"]["type"], serde_json::json!("line"));
        assert_eq!(
            shape["chartjs"]["mapping"]["labels_from"],
            serde_json::json!("month")
        );
    }

    #[test]
    fn result_shape_maps_two_grouped_dimensions_to_multi_series_chart() {
        let shape = result_shape_hint(
            "SELECT DATE(accounts.created_at) AS day, regions.name AS region, \
             COUNT(*) AS account_count FROM accounts \
             INNER JOIN regions ON regions.id = accounts.region_id \
             GROUP BY DATE(accounts.created_at), regions.name ORDER BY day",
        );
        assert_eq!(shape["kind"], serde_json::json!("multi_series_chart"));
        assert_eq!(shape["default_view"], serde_json::json!("chart"));
        assert_eq!(shape["chartjs"]["type"], serde_json::json!("line"));
        assert_eq!(
            shape["chartjs"]["mapping"]["labels_from"],
            serde_json::json!("day")
        );
        assert_eq!(
            shape["chartjs"]["mapping"]["series_from"],
            serde_json::json!("region")
        );
        assert_eq!(
            shape["chartjs"]["mapping"]["datasets"][0]["data_from"],
            serde_json::json!("account_count")
        );
    }

    #[test]
    fn result_shape_keeps_multi_column_lists_as_table() {
        let shape = result_shape_hint("SELECT accounts.company_name, agents.full_name FROM accounts INNER JOIN agents ON agents.id = accounts.owner_id");
        assert_eq!(shape["kind"], serde_json::json!("tabular"));
        assert_eq!(shape["default_view"], serde_json::json!("table"));
        assert!(shape["chartjs"].is_null());
    }

    #[test]
    fn query_frame_payload_filters_selected_bindings_by_selected_predicate_field() {
        let make_binding =
            |kind: &str, slot: &str, value: &str| semsql_runtime::QueryFrameBinding {
                mention_kind: kind.into(),
                mention_text: if kind == "date" {
                    "2000/1/1".into()
                } else {
                    "50%".into()
                },
                mention_normalized: if kind == "date" {
                    "2000/1/1".into()
                } else {
                    "50".into()
                },
                mention_start: 0,
                mention_end: 1,
                slot: slot.into(),
                slot_role: "predicate_value".into(),
                predicate_operator: Some(">".into()),
                predicate_field_slot: Some(
                    if slot == "@val1" {
                        "@field1"
                    } else {
                        "@field2"
                    }
                    .into(),
                ),
                candidate_index: 0,
                candidate_value: value.into(),
                candidate_source_fields: Vec::new(),
                match_kind: if kind == "date" {
                    "normalized_date_literal".into()
                } else {
                    "percentage_literal".into()
                },
                confidence: 0.8,
            };
        let out = semsql_runtime::CascadeOutcome {
            sql_text: "SELECT * FROM schools WHERE schools.opendate > '2000-01-01' AND frpm.percent_eligible > 50".into(),
            timings_us: semsql_runtime::PerStageTimings::default(),
            confidences: semsql_runtime::PerStageConfidence::default(),
            intent_hints: Vec::new(),
            stage_pinned: "stage_3".into(),
            repair_attempts: 0,
            query_frame: Some(semsql_runtime::QueryFrameTrace {
                schema_version: 3,
                source: "pre_stage3_slot_inputs".into(),
                question: "opened after 2000/1/1 over 50%".into(),
                skeleton: "SELECT * FROM @entity1 WHERE @field1 > @val1 AND @field2 > @val2".into(),
                linked_entities: vec!["schools".into(), "frpm".into()],
                linked_fields: vec!["schools.opendate".into(), "frpm.percent_eligible".into()],
                mentions: Vec::new(),
                bindings: vec![
                    make_binding("date", "@val1", "'2000-01-01'"),
                    make_binding("percentage", "@val1", "50"),
                    make_binding("date", "@val2", "'2000-01-01'"),
                    make_binding("percentage", "@val2", "50"),
                ],
                frame: semsql_runtime::QueryFrameParts::default(),
                slots: Vec::new(),
            }),
            runtime_query_frame: None,
            semantic_atlas: None,
            intent_frame: None,
            bound_query_plan: None,
            slot_decisions: vec![
                semsql_runtime::stage_slotfiller::SlotDecision {
                    slot_name: "@field1".into(),
                    slot_kind: "field".into(),
                    slot_role: "predicate_field".into(),
                    picked: Some("schools.opendate".into()),
                    ..Default::default()
                },
                semsql_runtime::stage_slotfiller::SlotDecision {
                    slot_name: "@field2".into(),
                    slot_kind: "field".into(),
                    slot_role: "predicate_field".into(),
                    picked: Some("frpm.percent_eligible".into()),
                    ..Default::default()
                },
                semsql_runtime::stage_slotfiller::SlotDecision {
                    slot_name: "@val1".into(),
                    slot_kind: "value".into(),
                    slot_role: "predicate_value".into(),
                    predicate_field_slot: Some("@field1".into()),
                    predicate_field: Some("schools.opendate".into()),
                    predicate_operator: Some(">".into()),
                    picked: Some("'2000-01-01'".into()),
                    candidates: vec![
                        semsql_runtime::stage_slotfiller::SlotDecisionCandidate {
                            value: "'2000-01-01'".into(),
                            score: 0.2,
                            biased_score: 0.9,
                            source_fields: Vec::new(),
                        },
                        semsql_runtime::stage_slotfiller::SlotDecisionCandidate {
                            value: "50".into(),
                            score: 0.1,
                            biased_score: 0.1,
                            source_fields: Vec::new(),
                        },
                    ],
                    ..Default::default()
                },
                semsql_runtime::stage_slotfiller::SlotDecision {
                    slot_name: "@val2".into(),
                    slot_kind: "value".into(),
                    slot_role: "predicate_value".into(),
                    predicate_field_slot: Some("@field2".into()),
                    predicate_field: Some("frpm.percent_eligible".into()),
                    predicate_operator: Some(">".into()),
                    picked: Some("50".into()),
                    candidates: vec![
                        semsql_runtime::stage_slotfiller::SlotDecisionCandidate {
                            value: "'2000-01-01'".into(),
                            score: 0.2,
                            biased_score: 0.2,
                            source_fields: Vec::new(),
                        },
                        semsql_runtime::stage_slotfiller::SlotDecisionCandidate {
                            value: "50".into(),
                            score: 0.3,
                            biased_score: 0.9,
                            source_fields: Vec::new(),
                        },
                    ],
                    ..Default::default()
                },
            ],
        };

        let payload = query_frame_payload("opened after 2000/1/1 over 50%", &out, &out.sql_text);
        let selected = payload["stage3"]["selected_bindings"].as_array().unwrap();
        assert_eq!(selected.len(), 2, "{selected:?}");
        assert!(selected.iter().any(|binding| {
            binding["mention_kind"] == serde_json::json!("date")
                && binding["slot"] == serde_json::json!("@val1")
                && binding["selected_predicate_field"] == serde_json::json!("schools.opendate")
                && binding["picked"] == serde_json::json!(true)
        }));
        assert!(selected.iter().any(|binding| {
            binding["mention_kind"] == serde_json::json!("percentage")
                && binding["slot"] == serde_json::json!("@val2")
                && binding["selected_predicate_field"] == serde_json::json!("frpm.percent_eligible")
                && binding["picked"] == serde_json::json!(true)
        }));
    }

    #[test]
    fn query_frame_renderability_ignores_at_signs_inside_literals() {
        assert_eq!(
            unresolved_placeholders("SELECT '@field1', `@field2`, @field3"),
            vec!["@field3".to_string()]
        );
        let diagnostics = renderability_diagnostics("SELECT '@field1' AS email");
        assert_eq!(diagnostics["sql_surface_valid"], serde_json::json!(true));
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
#[allow(unused_variables)]
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
