"""Laravel private-alpha extract/query/resolve/rerun acceptance probe."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

QUESTION = "show clients in segment enterprise"
APPROVED_TARGET = "packages.plan_level=enterprise"


def run_laravel_alpha_probe(
    *,
    out_dir: Path,
    semsql_bin: Path,
    build_extractor: bool = False,
    repo_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = repo_root or Path.cwd()
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

    app_dir = out_dir / "laravel-app"
    app_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "app.sqlite"
    graph_path = out_dir / "app.semsql"
    packet_path = out_dir / "rejected.packet.json"
    memory_path = out_dir / "resolution.memory.yaml"
    for path in (db_path, graph_path, packet_path, memory_path):
        path.unlink(missing_ok=True)
    _write_laravel_fixture(app_dir)
    _write_database(db_path)

    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    extract = _run(
        [
            semsql_bin,
            "extract",
            app_dir,
            "--framework",
            "laravel",
            "--db-url",
            f"sqlite:{db_path}",
            "--output",
            graph_path,
        ],
        env=run_env,
    )
    relationships = _read_relationships(graph_path) if extract.returncode == 0 else []

    first_query = _run(
        [
            semsql_bin,
            "query",
            "--graph",
            graph_path,
            "--dialect",
            "sqlite",
            "--format",
            "json",
            "--rejection-packet-json",
            packet_path,
            QUESTION,
        ],
        env=run_env,
    )
    first_payload = _json_stdout(first_query)
    first_decision = _decision(first_payload)
    packet = _read_json(packet_path)
    candidates = _slot_candidates(packet, "filter_value")

    approval = _run(
        [
            semsql_bin,
            "resolve",
            packet_path,
            "--mode",
            "json",
            "--memory",
            memory_path,
            "--choice",
            APPROVED_TARGET,
            "--term",
            "enterprise",
        ],
        env=run_env,
    )
    memory_created = approval.returncode == 0 and memory_path.exists()

    rerun = _run(
        [
            semsql_bin,
            "query",
            "--graph",
            graph_path,
            "--dialect",
            "sqlite",
            "--format",
            "json",
            "--resolution-memory",
            memory_path,
            QUESTION,
        ],
        env=run_env,
    )
    rerun_payload = _json_stdout(rerun)
    rerun_decision = _decision(rerun_payload)
    rerun_sql = _sql(rerun_payload)

    relationship_ok = any(
        row["from_entity"] == "clients"
        and row["from_field"] == "plan_ref"
        and row["to_entity"] == "packages"
        and row["to_field"] == "code"
        for row in relationships
    )
    ambiguity_ok = first_decision == "ask_user" and {
        "packages.plan_level=enterprise",
        "packages.service_level=enterprise",
    }.issubset(set(candidates))
    rerun_ok = (
        rerun.returncode == 0
        and rerun_decision == "execute"
        and rerun_sql is not None
        and "plan_level" in rerun_sql
        and "plan_ref" in rerun_sql
        and "packages" in rerun_sql
    )
    checks = {
        "extract": extract.returncode == 0,
        "source_relationship_grounded": relationship_ok,
        "first_query_asks_user": ambiguity_ok,
        "approval_saved": memory_created,
        "approved_rerun_executes": rerun_ok,
    }
    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "question": QUESTION,
        "approved_target": APPROVED_TARGET,
        "paths": {
            "app": str(app_dir),
            "database": str(db_path),
            "graph": str(graph_path),
            "packet": str(packet_path),
            "memory": str(memory_path),
        },
        "checks": checks,
        "relationships": relationships,
        "first_query": {
            "returncode": first_query.returncode,
            "decision": first_decision,
            "candidates": candidates,
            "stdout": first_query.stdout.strip(),
            "stderr": first_query.stderr.strip(),
        },
        "approval": {
            "returncode": approval.returncode,
            "stdout": approval.stdout.strip(),
            "stderr": approval.stderr.strip(),
        },
        "rerun": {
            "returncode": rerun.returncode,
            "decision": rerun_decision,
            "sql": rerun_sql,
            "stdout": rerun.stdout.strip(),
            "stderr": rerun.stderr.strip(),
        },
        "extract": {
            "returncode": extract.returncode,
            "stdout": extract.stdout.strip(),
            "stderr": extract.stderr.strip(),
        },
    }


def render_laravel_alpha_probe_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Laravel Private-Alpha Probe",
        "",
        f"- status: `{str(report['status']).upper()}`",
        f"- question: `{report['question']}`",
        f"- approved target: `{report['approved_target']}`",
        "",
        "| Check | Result |",
        "|---|---:|",
    ]
    for name, passed in report["checks"].items():
        lines.append(f"| `{name}` | `{bool(passed)}` |")
    lines.extend(
        [
            "",
            "## Decision Flow",
            "",
            f"- first decision: `{report['first_query']['decision']}`",
            f"- candidates: `{', '.join(report['first_query']['candidates'])}`",
            f"- rerun decision: `{report['rerun']['decision']}`",
            f"- rerun SQL: `{report['rerun']['sql']}`",
            "",
            "The fixture intentionally has no physical foreign key. The join edge",
            "must come from grounded Eloquent relationship metadata.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_laravel_fixture(root: Path) -> None:
    files = {
        "composer.json": json.dumps({"require": {"laravel/framework": "^11.0"}}),
        "app/Models/Client.php": """<?php
namespace App\\Models;
class Client extends Model {
    protected $table = 'clients';
    protected $fillable = ['client_name', 'plan_ref'];
    public function plan(): BelongsTo {
        return $this->belongsTo(Package::class, 'plan_ref', 'code');
    }
}
""",
        "app/Models/Package.php": """<?php
namespace App\\Models;
class Package extends Model {
    protected $table = 'packages';
    protected $fillable = ['code', 'package_name', 'plan_level', 'service_level'];
    public function clients(): HasMany {
        return $this->hasMany(Client::class, 'plan_ref', 'code');
    }
}
""",
        "app/Filament/Resources/ClientResource.php": """<?php
namespace App\\Filament\\Resources;
class ClientResource extends Resource {
    protected static ?string $model = Client::class;
    protected static ?string $modelLabel = 'Client';
    protected static ?string $pluralModelLabel = 'Clients';
    public static function table(Table $table): Table {
        return $table->columns([
            Tables\\Columns\\TextColumn::make('client_name')->label('Client Name'),
        ]);
    }
}
""",
        "app/Filament/Resources/PackageResource.php": """<?php
namespace App\\Filament\\Resources;
class PackageResource extends Resource {
    protected static ?string $model = Package::class;
    public static function form(Form $form): Form {
        return $form->schema([
            Forms\\Components\\TextInput::make('plan_level')->label('Segment'),
            Forms\\Components\\TextInput::make('service_level')->label('Segment'),
        ]);
    }
}
""",
    }
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _write_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE packages (
                code TEXT PRIMARY KEY,
                package_name TEXT NOT NULL,
                plan_level TEXT NOT NULL,
                service_level TEXT NOT NULL
            );
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY,
                client_name TEXT NOT NULL,
                plan_ref TEXT NOT NULL
            );
            INSERT INTO packages (code, package_name, plan_level, service_level)
            VALUES ('ENT', 'Enterprise Plan', 'enterprise', 'enterprise');
            INSERT INTO clients (id, client_name, plan_ref)
            VALUES (1, 'Acme', 'ENT');
            """
        )
    finally:
        conn.close()


def _run(args: list[Path | str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )


def _read_relationships(graph: Path) -> list[dict[str, str]]:
    conn = sqlite3.connect(graph)
    try:
        rows = conn.execute(
            """
            SELECT from_entity, from_field, to_entity, to_field, kind
            FROM relationships
            ORDER BY from_entity, from_field, to_entity, to_field
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "from_entity": str(row[0]),
            "from_field": str(row[1]),
            "to_entity": str(row[2]),
            "to_field": str(row[3]),
            "kind": str(row[4]),
        }
        for row in rows
    ]


def _json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _decision(payload: dict[str, Any]) -> str | None:
    decision = payload.get("resolution_decision")
    if not isinstance(decision, dict):
        return None
    value = decision.get("decision")
    return value if isinstance(value, str) else None


def _sql(payload: dict[str, Any]) -> str | None:
    value = payload.get("sql")
    return value if isinstance(value, str) else None


def _slot_candidates(packet: dict[str, Any], slot_name: str) -> list[str]:
    decision = packet.get("resolution_decision")
    if not isinstance(decision, dict):
        return []
    slots = decision.get("unresolved_slots")
    if not isinstance(slots, list):
        return []
    for slot in slots:
        if not isinstance(slot, dict) or slot.get("slot") != slot_name:
            continue
        candidates = slot.get("candidates")
        if isinstance(candidates, list):
            return [value for value in candidates if isinstance(value, str)]
    return []
