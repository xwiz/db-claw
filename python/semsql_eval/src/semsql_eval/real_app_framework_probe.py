"""Real application framework extraction probe.

The fixture bridge proves small synthetic apps. This probe targets real app
directories and checks generic invariants: source vocab exists, source vocab is
DB-grounded after ingestion, extraction stores no sample values when disabled,
    optional count-query checks route through the generated graph, and authored
    metrics either produce bounded fallback packets or deterministic local SQL
    matching the metric definition.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .realdb_schema_probe import _database_from_url, redact_db_url


def run_real_app_framework_probe(
    *,
    app_path: Path,
    framework: str,
    db_url: str,
    out_dir: Path,
    semsql_bin: Path,
    build_extractor: bool = False,
    repo_root: Path | None = None,
    min_source_vocab: int = 1,
    query_check_limit: int = 3,
    min_query_checks: int = 1,
    extract_timeout_seconds: int = 180,
    raw_timeout_seconds: int = 120,
    sample_values: bool = False,
    metric_jsonl: Path | None = None,
    metric_packet_check_limit: int = 3,
    min_metric_packet_checks: int = 0,
) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    if build_extractor:
        pnpm = shutil.which("pnpm")
        if pnpm is None:
            raise RuntimeError("pnpm executable not found; cannot build extractor CLI")
        subprocess.run(
            [pnpm, "--filter", "@semsql/extractor-cli", "build"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    raw_jsonl = out_dir / "source-vocab.raw.jsonl"
    graph_path = out_dir / "app.framework.semsql"
    raw = _run_raw_extractor(
        app_path=app_path,
        framework=framework,
        output=raw_jsonl,
        root=root,
        timeout_seconds=raw_timeout_seconds,
    )
    extract = _run_native_extract(
        app_path=app_path,
        framework=framework,
        db_url=db_url,
        semsql_bin=semsql_bin,
        graph_path=graph_path,
        timeout_seconds=extract_timeout_seconds,
        sample_values=sample_values,
        metric_jsonl=metric_jsonl,
    )
    graph = _inspect_graph(graph_path) if extract["returncode"] == 0 else _empty_graph()
    query_checks = _run_query_checks(
        graph_path=graph_path,
        semsql_bin=semsql_bin,
        candidates=graph["source_entity_vocab"],
        limit=query_check_limit,
    )
    metric_packet_checks = _run_metric_packet_checks(
        graph_path=graph_path,
        semsql_bin=semsql_bin,
        metrics=graph["metric_definitions"],
        limit=metric_packet_check_limit,
    )

    summary = _summarize(
        raw=raw,
        graph=graph,
        query_checks=query_checks,
        metric_packet_checks=metric_packet_checks,
        min_source_vocab=min_source_vocab,
        min_query_checks=min_query_checks,
        min_metric_packet_checks=min_metric_packet_checks,
        sample_values=sample_values,
    )
    return {
        "schema_version": 1,
        "status": "pass" if summary["pass"] else "fail",
        "app_path": str(app_path),
        "framework": framework,
        "database": _database_from_url(db_url),
        "db_url": redact_db_url(db_url),
        "out_dir": str(out_dir),
        "graph": str(graph_path),
        "raw_jsonl": str(raw_jsonl),
        "metric_jsonl": str(metric_jsonl) if metric_jsonl is not None else None,
        "semsql_bin": str(semsql_bin),
        "sample_values": sample_values,
        "summary": summary,
        "raw_extractor": raw,
        "native_extract": extract,
        "graph_inspection": graph,
        "query_checks": query_checks,
        "metric_packet_checks": metric_packet_checks,
    }


def render_real_app_framework_probe_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    graph = report["graph_inspection"]
    lines = [
        "# Real App Framework Probe",
        "",
        f"- status: `{report['status'].upper()}`",
        f"- framework: `{report['framework']}`",
        f"- database: `{report['database']}`",
        f"- app path: `{report['app_path']}`",
        f"- graph: `{report['graph']}`",
        f"- raw source jsonl: `{report['raw_jsonl']}`",
        f"- source vocab grounded: `{summary['source_vocab_grounded']}/{summary['source_vocab']}`",
        f"- raw source fragments: `{summary['raw_fragments']}`",
        f"- entities/fields/relationships: `{graph['entity_count']}/{graph['field_count']}/{graph['relationship_count']}`",
        f"- metric definitions: `{graph['metric_definition_count']}`",
        f"- sample-value rows: `{graph['sample_value_count']}`",
        f"- query checks: `{summary['query_checks_ok']}/{summary['query_checks']}` required `{summary['min_query_checks']}`",
        f"- metric packet checks: `{summary['metric_packet_checks_ok']}/{summary['metric_packet_checks']}` required `{summary['min_metric_packet_checks']}`",
        "",
        "## Source Extractors",
        "",
        "| Extractor | Vocab |",
        "|---|---:|",
    ]
    for extractor, count in graph["source_vocab_by_extractor"].items():
        lines.append(f"| `{extractor}` | `{count}` |")
    if not graph["source_vocab_by_extractor"]:
        lines.append("| `none` | `0` |")
    lines.extend(["", "## Query Checks", "", "| Term | Expected Entity | OK | SQL |", "|---|---|---:|---|"])
    for check in report["query_checks"]:
        lines.append(
            "| `{term}` | `{entity}` | `{ok}` | `{sql}` |".format(
                term=check["term"],
                entity=check["expected_entity"],
                ok=check["ok"],
                sql=(check["sql"] or "").replace("|", "\\|"),
            )
        )
    if not report["query_checks"]:
        lines.append("| `none` | `n/a` | `n/a` | `no source entity vocab candidates` |")
    lines.extend(["", "## Metric Packet Checks", "", "| Question | Metric | OK | Reason |", "|---|---|---:|---|"])
    for check in report["metric_packet_checks"]:
        lines.append(
            "| `{question}` | `{metric}` | `{ok}` | `{reason}` |".format(
                question=check["question"],
                metric=check["metric_name"],
                ok=check["ok"],
                reason=check["reason"].replace("|", "\\|"),
            )
        )
    if not report["metric_packet_checks"]:
        lines.append("| `none` | `n/a` | `n/a` | `no metric definitions requested or ingested` |")
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "This is a metadata/source-vocabulary probe. It does not sample rows,",
            "execute business queries, or prove full app semantic coverage.",
        ]
    )
    return "\n".join(lines) + "\n"


def _run_raw_extractor(
    *,
    app_path: Path,
    framework: str,
    output: Path,
    root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    script = root / "packages" / "extractor-cli" / "dist" / "cli.js"
    if not script.is_file():
        raise RuntimeError(f"extractor CLI is not built at {script}; pass --build-extractor")
    cmd = [
        "node",
        str(script),
        str(app_path),
        "--framework",
        framework,
        "--output",
        str(output),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    records = _read_jsonl(output) if proc.returncode == 0 else []
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "record_count": len(records),
        "canonical_kinds": dict(Counter(_fragment_kind(record) for record in records)),
        "extractors": dict(Counter(_fragment_extractor(record) for record in records)),
    }


def _run_native_extract(
    *,
    app_path: Path,
    framework: str,
    db_url: str,
    semsql_bin: Path,
    graph_path: Path,
    timeout_seconds: int,
    sample_values: bool,
    metric_jsonl: Path | None,
) -> dict[str, Any]:
    if graph_path.exists():
        graph_path.unlink()
    cmd = [
        str(semsql_bin),
        "extract",
        str(app_path),
        "--framework",
        framework,
        "--db-url",
        db_url,
        "--output",
        str(graph_path),
    ]
    if not sample_values:
        cmd.append("--no-sample-values")
    if metric_jsonl is not None:
        cmd.extend(["--vocab-jsonl", str(metric_jsonl)])
    env = os.environ.copy()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        env=env,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
        "graph_exists": graph_path.exists(),
        "graph_bytes": graph_path.stat().st_size if graph_path.exists() else 0,
    }


def _inspect_graph(graph_path: Path) -> dict[str, Any]:
    with sqlite3.connect(graph_path) as conn:
        entities = {
            str(row[0])
            for row in conn.execute("SELECT canonical_name FROM entities").fetchall()
        }
        fields = {
            f"{row[0]}.{row[1]}"
            for row in conn.execute("SELECT entity, field FROM fields").fetchall()
        }
        relationships = int(
            conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        )
        sample_values = int(
            conn.execute("SELECT COUNT(*) FROM sample_values").fetchone()[0]
        )
        metric_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(metric_definitions)").fetchall()
        }
        metric_select = [
            "name",
            "display_label",
            "metric_kind",
            "subject_entity",
            "numerator_field",
            "numerator_operator",
            "numerator_value",
            "numerator_value_kind",
            "denominator_field",
            "scale",
            "required_entities_json",
            "aliases_json",
            "measure_field" if "measure_field" in metric_columns else "NULL AS measure_field",
            "aggregate" if "aggregate" in metric_columns else "NULL AS aggregate",
            (
                "distinct_measure"
                if "distinct_measure" in metric_columns
                else "0 AS distinct_measure"
            ),
        ]
        metric_rows = conn.execute(
            f"SELECT {', '.join(metric_select)} FROM metric_definitions ORDER BY name"
        ).fetchall()
        vocab_rows = conn.execute(
            "SELECT term, canonical_kind, canonical_value, source_layer, source_locator "
            "FROM vocabulary ORDER BY source_layer DESC, term, canonical_value"
        ).fetchall()
    source_vocab = [
        _vocab_record(row, entities=entities, fields=fields)
        for row in vocab_rows
        if row[4] is not None or int(row[3] or 0) > 1
    ]
    source_entity_vocab = [
        row
        for row in source_vocab
        if row["canonical_kind"] == "entity"
        and row["grounded"]
        and _safe_query_term(row["term"])
    ]
    extractor_counts = Counter(row["extractor"] for row in source_vocab)
    return {
        "entity_count": len(entities),
        "field_count": len(fields),
        "relationship_count": relationships,
        "sample_value_count": sample_values,
        "metric_definition_count": len(metric_rows),
        "metric_definitions": [_metric_record(row) for row in metric_rows],
        "vocabulary_count": len(vocab_rows),
        "source_vocab_count": len(source_vocab),
        "source_vocab_grounded": sum(1 for row in source_vocab if row["grounded"]),
        "source_vocab_dangling": [row for row in source_vocab if not row["grounded"]][:20],
        "source_vocab_by_extractor": dict(extractor_counts),
        "source_entity_vocab": source_entity_vocab[:25],
    }


def _empty_graph() -> dict[str, Any]:
    return {
        "entity_count": 0,
        "field_count": 0,
        "relationship_count": 0,
        "sample_value_count": 0,
        "metric_definition_count": 0,
        "metric_definitions": [],
        "vocabulary_count": 0,
        "source_vocab_count": 0,
        "source_vocab_grounded": 0,
        "source_vocab_dangling": [],
        "source_vocab_by_extractor": {},
        "source_entity_vocab": [],
    }


def _metric_record(row: tuple[Any, ...]) -> dict[str, Any]:
    aliases = _json_list(row[11])
    return {
        "name": str(row[0]),
        "display_label": str(row[1] or ""),
        "metric_kind": str(row[2]),
        "subject_entity": str(row[3]),
        "numerator_field": str(row[4]),
        "numerator_operator": str(row[5]),
        "numerator_value": str(row[6]),
        "numerator_value_kind": str(row[7]),
        "denominator_field": str(row[8]),
        "scale": float(row[9]),
        "required_entities": _json_list(row[10]),
        "aliases": aliases,
        "measure_field": str(row[12]) if row[12] is not None else None,
        "aggregate": str(row[13]).upper() if row[13] is not None else None,
        "distinct": bool(int(row[14] or 0)),
    }


def _json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)]


def _vocab_record(
    row: tuple[Any, ...],
    *,
    entities: set[str],
    fields: set[str],
) -> dict[str, Any]:
    term, kind, value, layer, locator_json = row
    locator = _parse_locator(locator_json)
    value = str(value)
    return {
        "term": str(term),
        "canonical_kind": str(kind),
        "canonical_value": value,
        "source_layer": int(layer or 0),
        "extractor": locator.get("extractor", "unknown"),
        "grounded": _canonical_is_grounded(str(kind), value, entities, fields),
    }


def _canonical_is_grounded(
    kind: str,
    value: str,
    entities: set[str],
    fields: set[str],
) -> bool:
    if kind == "entity":
        return value in entities
    if kind == "field":
        return value in fields
    if kind == "enum_value":
        field, _sep, _raw = value.partition(":")
        return field in fields
    if kind == "scope_predicate":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("field"), str):
            return str(parsed["field"]) in fields
        field, _sep, _raw = value.partition("=")
        return field in fields
    return False


def _run_query_checks(
    *,
    graph_path: Path,
    semsql_bin: Path,
    candidates: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate["term"], candidate["canonical_value"])
        if key in seen:
            continue
        seen.add(key)
        question = f"how many {candidate['term']}"
        proc = subprocess.run(
            [str(semsql_bin), "query", "--graph", str(graph_path), question],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        sql = proc.stdout.splitlines()[0] if proc.stdout.splitlines() else ""
        expected = str(candidate["canonical_value"])
        checks.append(
            {
                "term": candidate["term"],
                "question": question,
                "expected_entity": expected,
                "returncode": proc.returncode,
                "sql": sql,
                "stderr": proc.stderr.strip(),
                "ok": proc.returncode == 0 and expected in sql,
            }
        )
        if len(checks) >= limit:
            break
    return checks


def _run_metric_packet_checks(
    *,
    graph_path: Path,
    semsql_bin: Path,
    metrics: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for metric in metrics[: max(limit, 0)]:
        question = _metric_probe_question(metric)
        packet_path = graph_path.parent / f"metric-packet-{len(checks) + 1}.json"
        if packet_path.exists():
            packet_path.unlink()
        proc = subprocess.run(
            [
                str(semsql_bin),
                "query",
                "--graph",
                str(graph_path),
                "--rejection-packet-json",
                str(packet_path),
                question,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        packet = _read_json_object(packet_path) if packet_path.exists() else None
        hits = (
            packet.get("local_candidates", {}).get("metric_catalog_hits", [])
            if isinstance(packet, dict)
            else []
        )
        matched = any(
            isinstance(hit, dict) and hit.get("name") == metric["name"] for hit in hits
        )
        sql = proc.stdout.strip()
        local_metric_ok = (
            proc.returncode == 0
            and not packet_path.exists()
            and _metric_sql_matches(metric, sql)
        )
        if local_metric_ok:
            reason = "local route ok"
        elif not packet_path.exists():
            reason = "no rejection packet written"
        elif not matched:
            reason = "metric catalog hit missing"
        else:
            reason = "ok"
        checks.append(
            {
                "metric_name": metric["name"],
                "question": question,
                "returncode": proc.returncode,
                "sql": sql,
                "stderr": proc.stderr.strip(),
                "packet": str(packet_path) if packet_path.exists() else None,
                "packet_metric_hit_count": len(hits),
                "ok": matched or local_metric_ok,
                "reason": reason,
            }
        )
    return checks


def _metric_sql_matches(metric: dict[str, Any], sql: str) -> bool:
    if not sql.strip():
        return False
    metric_kind = str(metric.get("metric_kind") or "")
    if metric_kind == "aggregate":
        aggregate = str(metric.get("aggregate") or "").upper()
        measure_field = str(metric.get("measure_field") or "")
        if aggregate not in {"AVG", "COUNT", "MAX", "MIN", "SUM"}:
            return False
        if aggregate not in sql.upper():
            return False
        if not _sql_mentions_field(sql, measure_field):
            return False
        if metric.get("distinct") and "DISTINCT" not in sql.upper():
            return False
        return True
    if metric_kind == "conditional_rate":
        return _sql_mentions_field(
            sql,
            str(metric.get("numerator_field") or ""),
        ) and _sql_mentions_field(sql, str(metric.get("denominator_field") or ""))
    return False


def _sql_mentions_field(sql: str, field_ref: str) -> bool:
    if "." not in field_ref:
        return False
    entity, field = field_ref.split(".", 1)
    lowered = sql.lower()
    return entity.lower() in lowered and field.lower() in lowered


def _metric_probe_question(metric: dict[str, Any]) -> str:
    aliases = metric.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                return alias.strip()
    display_label = metric.get("display_label")
    if isinstance(display_label, str) and display_label.strip():
        return display_label.strip()
    return str(metric["name"]).replace("_", " ")


def _summarize(
    *,
    raw: dict[str, Any],
    graph: dict[str, Any],
    query_checks: list[dict[str, Any]],
    metric_packet_checks: list[dict[str, Any]],
    min_source_vocab: int,
    min_query_checks: int,
    min_metric_packet_checks: int,
    sample_values: bool,
) -> dict[str, Any]:
    source_vocab = graph["source_vocab_count"]
    source_grounded = graph["source_vocab_grounded"]
    query_ok = sum(1 for check in query_checks if check["ok"])
    required_query_ok = query_ok == len(query_checks)
    required_query_count_ok = len(query_checks) >= min_query_checks
    metric_packet_ok = sum(1 for check in metric_packet_checks if check["ok"])
    required_metric_packet_ok = metric_packet_ok == len(metric_packet_checks)
    required_metric_packet_count_ok = (
        len(metric_packet_checks) >= min_metric_packet_checks
    )
    sample_policy_ok = sample_values or graph["sample_value_count"] == 0
    passed = (
        raw["returncode"] == 0
        and raw["record_count"] >= min_source_vocab
        and graph["entity_count"] > 0
        and graph["field_count"] > 0
        and source_vocab >= min_source_vocab
        and source_vocab == source_grounded
        and sample_policy_ok
        and required_query_count_ok
        and required_query_ok
        and required_metric_packet_count_ok
        and required_metric_packet_ok
    )
    return {
        "pass": passed,
        "raw_fragments": raw["record_count"],
        "source_vocab": source_vocab,
        "source_vocab_grounded": source_grounded,
        "source_vocab_dangling": len(graph["source_vocab_dangling"]),
        "query_checks": len(query_checks),
        "query_checks_ok": query_ok,
        "min_query_checks": min_query_checks,
        "query_check_count_ok": required_query_count_ok,
        "metric_definitions": graph["metric_definition_count"],
        "metric_packet_checks": len(metric_packet_checks),
        "metric_packet_checks_ok": metric_packet_ok,
        "min_metric_packet_checks": min_metric_packet_checks,
        "metric_packet_check_count_ok": required_metric_packet_count_ok,
        "sample_policy_ok": sample_policy_ok,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        records.append(json.loads(line))
    return records


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _fragment_kind(record: dict[str, Any]) -> str:
    canonical = record.get("canonical")
    if isinstance(canonical, dict):
        return str(canonical.get("kind", "unknown"))
    return "unknown"


def _fragment_extractor(record: dict[str, Any]) -> str:
    locator = record.get("locator")
    if isinstance(locator, dict):
        return str(locator.get("extractor", "unknown"))
    return "unknown"


def _parse_locator(locator_json: Any) -> dict[str, Any]:
    if not locator_json:
        return {}
    try:
        parsed = json.loads(str(locator_json))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_query_term(term: str) -> bool:
    normalized = term.strip()
    return (
        2 <= len(normalized) <= 48
        and all(ch.isalnum() or ch in {" ", "_", "-"} for ch in normalized)
        and not any(ch.isdigit() for ch in normalized)
    )
