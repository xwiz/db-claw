//! Resolution surfaces over the shared rejected-query packet contract.

use anyhow::{Context, Result};
use semsql_runtime::context::{ResolutionMemory, ResolutionMemoryEntry};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

#[derive(Clone, Debug, Serialize)]
struct ResolutionSession {
    schema_version: u64,
    source: &'static str,
    question: String,
    decision: String,
    reason: String,
    next_action: String,
    atlas_strength: Value,
    tasks: Vec<ResolutionTask>,
    schema_evidence: Value,
    candidate_plan: Value,
    validated_sql_preview: Option<String>,
    memory_path: String,
}

#[derive(Clone, Debug, Serialize)]
struct ResolutionTask {
    slot: String,
    status: String,
    reason: String,
    candidates: Vec<String>,
    suggested_kind: Option<String>,
    suggested_term: Option<String>,
}

#[derive(Debug, Deserialize)]
struct SaveRequest {
    term: String,
    kind: String,
    target: String,
}

pub fn run(
    packet_path: &Path,
    mode: &str,
    memory_path: Option<&Path>,
    choice: Option<&str>,
    term: Option<&str>,
    kind: Option<&str>,
) -> Result<()> {
    let packet = read_packet(packet_path)?;
    let memory_path = memory_path
        .map(Path::to_path_buf)
        .unwrap_or_else(|| default_memory_path(packet_path));
    let session = build_session(&packet, &memory_path)?;
    if let Some(target) = choice {
        let term = term.context("--term is required when --choice is used")?;
        let kind = kind
            .map(str::to_owned)
            .or_else(|| infer_kind_for_target(&session, target))
            .context("--kind is required when the target kind cannot be inferred")?;
        save_memory_entry(&packet, packet_path, &memory_path, term, &kind, target)?;
        let normalized = normalized_target(&kind, target);
        print_saved(&memory_path, term, &kind, &normalized);
        return Ok(());
    }
    match mode {
        "json" => {
            println!("{}", serde_json::to_string_pretty(&session)?);
            Ok(())
        }
        "cli" => run_cli(&packet, packet_path, &memory_path, &session),
        "web" => run_web(&packet, packet_path, &memory_path, &session),
        _ => anyhow::bail!("unsupported resolve mode `{mode}`; expected web, cli, or json"),
    }
}

fn read_packet(path: &Path) -> Result<Value> {
    let bytes = std::fs::read(path)
        .with_context(|| format!("reading rejected packet `{}`", path.display()))?;
    let packet: Value = serde_json::from_slice(&bytes)
        .with_context(|| format!("parsing rejected packet `{}`", path.display()))?;
    if packet.get("source").and_then(Value::as_str) != Some("semsql_rejected_query_packet") {
        anyhow::bail!("`{}` is not a semsql rejected-query packet", path.display());
    }
    Ok(packet)
}

fn build_session(packet: &Value, memory_path: &Path) -> Result<ResolutionSession> {
    let decision = packet
        .get("resolution_decision")
        .and_then(Value::as_object)
        .context("packet is missing resolution_decision")?;
    let packet_suggested_term = packet
        .pointer("/resolution_task/unresolved_value_bindings/0/value")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let tasks = decision
        .get("unresolved_slots")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|slot| {
            let slot_name = slot
                .get("slot")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let candidates = slot
                .get("candidates")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect::<Vec<_>>();
            ResolutionTask {
                slot: slot_name.to_string(),
                status: slot
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("missing")
                    .to_string(),
                reason: slot
                    .get("reason")
                    .and_then(Value::as_str)
                    .unwrap_or("unresolved")
                    .to_string(),
                candidates: candidates.clone(),
                suggested_kind: suggested_kind(slot_name).map(str::to_owned),
                suggested_term: packet_suggested_term
                    .clone()
                    .or_else(|| suggested_term_from_candidates(slot_name, &candidates)),
            }
        })
        .collect();
    let mut candidate_plan = packet
        .pointer("/query_frame/bound_query_plan")
        .cloned()
        .unwrap_or(Value::Null);
    let plan_valid = candidate_plan
        .get("valid")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let validated_sql_preview =
        if decision.get("decision").and_then(Value::as_str) == Some("execute") && plan_valid {
            candidate_plan
                .get("sql")
                .and_then(Value::as_str)
                .map(str::to_owned)
        } else {
            None
        };
    if let Some(object) = candidate_plan.as_object_mut() {
        object.remove("sql");
    }
    let schema_evidence = serde_json::json!({
        "graph_context": packet.get("graph_context").cloned().unwrap_or(Value::Null),
        "local_candidates": packet.get("local_candidates").cloned().unwrap_or(Value::Null),
        "semantic_atlas": packet.pointer("/query_frame/semantic_atlas").cloned().unwrap_or(Value::Null),
    });
    Ok(ResolutionSession {
        schema_version: 2,
        source: "semsql_resolution_session",
        question: packet
            .get("question")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        decision: decision
            .get("decision")
            .and_then(Value::as_str)
            .unwrap_or("reject")
            .to_string(),
        reason: decision
            .get("reason")
            .and_then(Value::as_str)
            .unwrap_or("unknown")
            .to_string(),
        next_action: decision
            .get("next_action")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        atlas_strength: decision
            .get("atlas_strength")
            .cloned()
            .unwrap_or(Value::Null),
        tasks,
        schema_evidence,
        candidate_plan,
        validated_sql_preview,
        memory_path: memory_path.display().to_string(),
    })
}

fn suggested_term_from_candidates(slot: &str, candidates: &[String]) -> Option<String> {
    if slot != "filter_value" || candidates.is_empty() {
        return None;
    }
    let values = candidates
        .iter()
        .map(|candidate| candidate.rsplit_once('=').map(|(_, value)| value.trim()))
        .collect::<Option<Vec<_>>>()?;
    let first = values.first()?.trim();
    if first.is_empty()
        || values
            .iter()
            .any(|value| !value.eq_ignore_ascii_case(first))
    {
        return None;
    }
    Some(first.to_ascii_lowercase())
}

fn run_cli(
    packet: &Value,
    packet_path: &Path,
    memory_path: &Path,
    session: &ResolutionSession,
) -> Result<()> {
    println!("Question: {}", session.question);
    println!("Decision: {} ({})", session.decision, session.reason);
    let task = session
        .tasks
        .iter()
        .find(|task| !task.candidates.is_empty())
        .context("packet has no bounded candidates to approve")?;
    println!("Resolve {}:", task.slot);
    for (index, candidate) in task.candidates.iter().enumerate() {
        println!("  {}. {}", index + 1, candidate);
    }
    print!("Choice: ");
    std::io::stdout().flush()?;
    let mut selected = String::new();
    std::io::stdin().read_line(&mut selected)?;
    let index: usize = selected.trim().parse().context("choice must be a number")?;
    let target = task
        .candidates
        .get(index.saturating_sub(1))
        .context("choice is out of range")?;
    print!("Term or phrase to remember");
    if let Some(suggested) = &task.suggested_term {
        print!(" [{suggested}]");
    }
    print!(": ");
    std::io::stdout().flush()?;
    let mut entered_term = String::new();
    std::io::stdin().read_line(&mut entered_term)?;
    let term = if entered_term.trim().is_empty() {
        task.suggested_term
            .as_deref()
            .context("a term is required for approved memory")?
    } else {
        entered_term.trim()
    };
    let kind = task
        .suggested_kind
        .as_deref()
        .or_else(|| infer_kind_from_target(target))
        .context("cannot infer canonical kind for selected target")?;
    save_memory_entry(packet, packet_path, memory_path, term, kind, target)?;
    let normalized = normalized_target(kind, target);
    print_saved(memory_path, term, kind, &normalized);
    Ok(())
}

fn run_web(
    packet: &Value,
    packet_path: &Path,
    memory_path: &Path,
    session: &ResolutionSession,
) -> Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0").context("binding local resolver")?;
    listener.set_nonblocking(true)?;
    let url = format!("http://{}/", listener.local_addr()?);
    open_browser(&url)?;
    println!("resolution_editor={url}");
    println!("waiting for an approved mapping; Ctrl+C cancels");
    let deadline = Instant::now() + Duration::from_secs(15 * 60);
    while Instant::now() < deadline {
        match listener.accept() {
            Ok((mut stream, _)) => {
                if handle_request(&mut stream, packet, packet_path, memory_path, session)? {
                    return Ok(());
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(error) => return Err(error.into()),
        }
    }
    anyhow::bail!("local resolver timed out without an approved mapping")
}

fn handle_request(
    stream: &mut TcpStream,
    packet: &Value,
    packet_path: &Path,
    memory_path: &Path,
    session: &ResolutionSession,
) -> Result<bool> {
    let request = read_http_request(stream)?;
    let first_line = request.lines().next().unwrap_or_default();
    if first_line.starts_with("GET / ") {
        let body = editor_html(session)?;
        write_http(
            stream,
            "200 OK",
            "text/html; charset=utf-8",
            body.as_bytes(),
        )?;
        return Ok(false);
    }
    if first_line.starts_with("GET /session ") {
        let body = serde_json::to_vec_pretty(session)?;
        write_http(stream, "200 OK", "application/json", &body)?;
        return Ok(false);
    }
    if first_line.starts_with("POST /save ") {
        let body = request.split("\r\n\r\n").nth(1).unwrap_or_default();
        let save: SaveRequest = serde_json::from_str(body).context("parsing web save request")?;
        save_memory_entry(
            packet,
            packet_path,
            memory_path,
            &save.term,
            &save.kind,
            &save.target,
        )?;
        write_http(stream, "200 OK", "application/json", br#"{"saved":true}"#)?;
        let normalized = normalized_target(&save.kind, &save.target);
        print_saved(memory_path, &save.term, &save.kind, &normalized);
        return Ok(true);
    }
    write_http(stream, "404 Not Found", "text/plain", b"not found")?;
    Ok(false)
}

fn read_http_request(stream: &mut TcpStream) -> Result<String> {
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    let mut bytes = Vec::new();
    let mut chunk = [0_u8; 4096];
    loop {
        let read = stream.read(&mut chunk)?;
        if read == 0 {
            break;
        }
        bytes.extend_from_slice(&chunk[..read]);
        if let Some(header_end) = find_subsequence(&bytes, b"\r\n\r\n") {
            let headers = String::from_utf8_lossy(&bytes[..header_end]);
            let content_length = headers
                .lines()
                .find_map(|line| {
                    line.strip_prefix("Content-Length:")
                        .or_else(|| line.strip_prefix("content-length:"))
                })
                .and_then(|value| value.trim().parse::<usize>().ok())
                .unwrap_or(0);
            if bytes.len() >= header_end + 4 + content_length {
                break;
            }
        }
        if bytes.len() > 64 * 1024 {
            anyhow::bail!("resolver HTTP request is too large");
        }
    }
    String::from_utf8(bytes).context("resolver HTTP request was not UTF-8")
}

fn editor_html(session: &ResolutionSession) -> Result<String> {
    let session_json = serde_json::to_string(session)?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e");
    Ok(EDITOR_HTML.replace("__SESSION__", &session_json))
}

fn save_memory_entry(
    packet: &Value,
    packet_path: &Path,
    memory_path: &Path,
    term: &str,
    kind: &str,
    target: &str,
) -> Result<()> {
    let target = normalized_target(kind, target);
    validate_mapping(kind, &target)?;
    if term.trim().is_empty() {
        anyhow::bail!("memory term must not be empty");
    }
    let drift_key = packet
        .pointer("/graph_context/schema_hash")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let mut memory: ResolutionMemory = if memory_path.exists() {
        let text = std::fs::read_to_string(memory_path)
            .with_context(|| format!("reading memory `{}`", memory_path.display()))?;
        serde_yaml::from_str(&text)
            .with_context(|| format!("parsing memory `{}`", memory_path.display()))?
    } else {
        ResolutionMemory {
            drift_key: drift_key.clone(),
            ..Default::default()
        }
    };
    if memory.drift_key.is_none() {
        memory.drift_key = drift_key.clone();
    }
    let entry = ResolutionMemoryEntry {
        term: term.trim().to_ascii_lowercase(),
        kind: kind.to_string(),
        target: target.clone(),
        status: "confirmed".to_string(),
        confidence: 0.9,
        drift_key,
        provenance: Some(serde_json::json!({
            "source": "explicit_user_approval",
            "question": packet
                .get("question")
                .and_then(Value::as_str)
                .unwrap_or_default(),
            "packet": packet_path.display().to_string(),
        })),
    };
    memory.entries.retain(|existing| {
        !(existing.term == entry.term
            && existing.kind == entry.kind
            && existing.target == entry.target)
    });
    memory.entries.push(entry);
    memory.entries.sort_by(|left, right| {
        left.term
            .cmp(&right.term)
            .then_with(|| left.kind.cmp(&right.kind))
            .then_with(|| left.target.cmp(&right.target))
    });
    if let Some(parent) = memory_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(memory_path, serde_yaml::to_string(&memory)?)
        .with_context(|| format!("writing memory `{}`", memory_path.display()))?;
    Ok(())
}

fn validate_mapping(kind: &str, target: &str) -> Result<()> {
    if !matches!(
        kind,
        "entity" | "field" | "enum_value" | "relationship" | "scope_predicate"
    ) {
        anyhow::bail!("unsupported memory mapping kind `{kind}`");
    }
    if target.trim().is_empty() {
        anyhow::bail!("memory target must not be empty");
    }
    Ok(())
}

fn suggested_kind(slot: &str) -> Option<&'static str> {
    match slot {
        "subject" | "table_family" => Some("entity"),
        "projection" | "date_time" | "order_field" => Some("field"),
        "filter_value" => Some("enum_value"),
        "join_path" => Some("relationship"),
        _ => None,
    }
}

fn normalized_target(kind: &str, target: &str) -> String {
    if kind == "enum_value" {
        if let Some((field, value)) = target.split_once('=') {
            return format!("{}:{}", field.trim(), value.trim());
        }
    }
    if kind == "field" {
        if let Some((field, _)) = target.split_once('=') {
            return field.trim().to_string();
        }
    }
    if kind == "relationship" {
        if let Some((left, right)) = target.split_once("->") {
            return format!("{} -> {}", left.trim(), right.trim());
        }
    }
    target.trim().to_string()
}

fn infer_kind_for_target(session: &ResolutionSession, target: &str) -> Option<String> {
    session.tasks.iter().find_map(|task| {
        task.candidates
            .iter()
            .any(|candidate| candidate == target)
            .then(|| task.suggested_kind.clone())
            .flatten()
    })
}

fn infer_kind_from_target(target: &str) -> Option<&'static str> {
    if target.contains(" -> ") {
        Some("relationship")
    } else if target.contains('.') {
        Some("field")
    } else if !target.trim().is_empty() {
        Some("entity")
    } else {
        None
    }
}

fn default_memory_path(packet_path: &Path) -> PathBuf {
    packet_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("semsql.memory.yaml")
}

fn open_browser(url: &str) -> Result<()> {
    #[cfg(target_os = "windows")]
    Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn()
        .context("opening local resolver in the default browser")?;
    #[cfg(target_os = "macos")]
    Command::new("open").arg(url).spawn()?;
    #[cfg(all(unix, not(target_os = "macos")))]
    Command::new("xdg-open").arg(url).spawn()?;
    Ok(())
}

fn write_http(stream: &mut TcpStream, status: &str, content_type: &str, body: &[u8]) -> Result<()> {
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )?;
    stream.write_all(body)?;
    stream.flush()?;
    Ok(())
}

fn find_subsequence(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn print_saved(path: &Path, term: &str, kind: &str, target: &str) {
    println!("resolution_memory={}", path.display());
    println!("saved_mapping={term} -> {kind}:{target}");
}

const EDITOR_HTML: &str = r#"<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DB Claw Resolution</title>
<style>
body{font:14px system-ui;margin:0;background:#f6f7f9;color:#17202a}
header{background:#fff;border-bottom:1px solid #d9dee5;padding:16px 24px}
main{display:grid;grid-template-columns:minmax(280px,420px) 1fr;min-height:calc(100vh - 70px)}
aside,section{padding:20px 24px}aside{background:#fff;border-right:1px solid #d9dee5}
h1{font-size:18px;margin:0 0 6px}h2{font-size:15px}.muted{color:#637083}
.task{border-top:1px solid #e4e7eb;padding:14px 0}label{display:block;font-weight:600;margin:12px 0 5px}
input,select{width:100%;box-sizing:border-box;padding:9px;border:1px solid #aeb7c2;border-radius:4px;background:#fff}
button{margin-top:16px;padding:9px 14px;border:0;border-radius:4px;background:#1267d6;color:#fff;font-weight:600;cursor:pointer}
pre{white-space:pre-wrap;background:#fff;border:1px solid #d9dee5;padding:14px;max-height:65vh;overflow:auto}
@media(max-width:760px){main{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #d9dee5}}
</style></head><body>
<header><h1>Resolve query</h1><div class="muted" id="decision"></div></header>
<main><aside><div id="question"></div><div id="tasks"></div></aside>
<section><h2>Candidate plan</h2><pre id="plan"></pre>
<h2>Schema evidence</h2><pre id="evidence"></pre>
<div id="preview" hidden><h2>Validated SQL</h2><pre id="sql"></pre></div></section></main>
<script>
const s=__SESSION__;
document.querySelector('#decision').textContent=`${s.decision}: ${s.reason}`;
document.querySelector('#question').innerHTML=`<strong>${s.question}</strong><p class="muted">${s.next_action}</p>`;
document.querySelector('#plan').textContent=JSON.stringify(s.candidate_plan,null,2);
document.querySelector('#evidence').textContent=JSON.stringify({atlas_strength:s.atlas_strength,...s.schema_evidence},null,2);
if(s.validated_sql_preview){document.querySelector('#preview').hidden=false;document.querySelector('#sql').textContent=s.validated_sql_preview}
const root=document.querySelector('#tasks');
s.tasks.forEach((t,i)=>{
 const div=document.createElement('div');div.className='task';
 const options=t.candidates.map(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;return o.outerHTML}).join('');
 div.innerHTML=`<h2>${t.slot}</h2><div class="muted">${t.status}: ${t.reason}</div>
 <label>Candidate</label><select>${options}</select>
 <label>Term to remember</label><input class="term">
 <label>Mapping kind</label><input class="kind">
 <button type="button">Approve mapping</button>`;
 div.querySelector('.term').value=t.suggested_term||'';
 div.querySelector('.kind').value=t.suggested_kind||'';
 div.querySelector('button').onclick=async()=>{
  const payload={target:div.querySelector('select').value,term:div.querySelector('.term').value,kind:div.querySelector('.kind').value};
  if(!payload.term||!payload.kind){alert('Term and kind are required');return}
  const response=await fetch('/save',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});
  if(response.ok)document.body.innerHTML='<main><section><h1>Mapping saved</h1><p>You may close this tab.</p></section></main>';
  else alert(await response.text());
 };
 root.appendChild(div);
});
</script></body></html>"#;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn session_exposes_decision_tasks() {
        let packet = serde_json::json!({
            "source": "semsql_rejected_query_packet",
            "question": "show fast orders",
            "resolution_decision": {
                "decision": "ask_user",
                "reason": "missing_value_evidence",
                "next_action": "choose a field",
                "atlas_strength": {"overall": 0.35},
                "unresolved_slots": [{
                    "slot": "filter_value",
                    "status": "weak",
                    "reason": "missing_value_evidence",
                    "candidates": ["orders.speed_score"]
                }]
            },
            "resolution_task": {
                "unresolved_value_bindings": [{"value": "fast"}]
            },
            "query_frame": {
                "bound_query_plan": {
                    "valid": false,
                    "sql": "SELECT * FROM orders",
                    "predicates": []
                }
            }
        });
        let session = build_session(&packet, Path::new("memory.yaml")).unwrap();
        assert_eq!(session.schema_version, 2);
        assert_eq!(session.decision, "ask_user");
        assert_eq!(
            session.tasks[0].suggested_kind.as_deref(),
            Some("enum_value")
        );
        assert_eq!(session.tasks[0].suggested_term.as_deref(), Some("fast"));
        assert!(session.validated_sql_preview.is_none());
        assert!(session.candidate_plan.get("sql").is_none());
    }

    #[test]
    fn session_derives_common_filter_term_from_candidates() {
        let packet = serde_json::json!({
            "source": "semsql_rejected_query_packet",
            "question": "show clients in segment enterprise",
            "resolution_decision": {
                "decision": "ask_user",
                "reason": "ambiguous_unscoped_value_field",
                "next_action": "choose a field",
                "atlas_strength": {"overall": 0.25},
                "unresolved_slots": [{
                    "slot": "filter_value",
                    "status": "ambiguous",
                    "reason": "ambiguous_unscoped_value_field",
                    "candidates": [
                        "packages.package_name=Enterprise",
                        "packages.package_tier=enterprise"
                    ]
                }]
            }
        });
        let session = build_session(&packet, Path::new("memory.yaml")).unwrap();
        assert_eq!(
            session.tasks[0].suggested_term.as_deref(),
            Some("enterprise")
        );
    }
}
