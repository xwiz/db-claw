"""Framework-source extraction bridge probes.

The probe creates tiny app-source fixtures plus a real SQLite schema, then runs
the native ``semsql extract --framework <name>`` bridge. It verifies that source
vocabulary resolves to DB-grounded graph canonicals rather than dangling
framework/model names.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExpectedVocab:
    term: str
    canonical_kind: str
    canonical_value: str


@dataclass(frozen=True)
class FrameworkFixture:
    framework: str
    files: dict[str, str]
    expected_vocab: tuple[ExpectedVocab, ...]
    query: str | None = None
    expected_sql: str | None = None


def framework_bridge_fixtures() -> tuple[FrameworkFixture, ...]:
    return (
        FrameworkFixture(
            framework="laravel",
            files={
                "composer.json": json.dumps({"require": {"laravel/framework": "^11.0"}}),
                "app/Filament/Resources/StudentResource.php": """<?php
namespace App\\Filament\\Resources;
class StudentResource extends Resource {
    protected static ?string $model = User::class;
    protected static ?string $modelLabel = 'Student';
    protected static ?string $pluralModelLabel = 'Students';
    public static function form(Form $form): Form {
        return $form->schema([
            Forms\\Components\\TextInput::make('status')->label('Account Status'),
        ]);
    }
}
""",
            },
            expected_vocab=(
                ExpectedVocab("students", "entity", "users"),
                ExpectedVocab("account status", "field", "users.status"),
            ),
            query="how many students",
            expected_sql='SELECT COUNT(*) FROM "users"',
        ),
        FrameworkFixture(
            framework="django",
            files={
                "manage.py": "# django fixture\n",
                "users/models.py": """from django.db import models

class User(models.Model):
    status = models.CharField(
        verbose_name="Account Status",
        max_length=20,
        choices=[("active", "Active"), ("blocked", "Blocked")],
    )

    class Meta:
        verbose_name = "Student"
        verbose_name_plural = "Students"
""",
            },
            expected_vocab=(
                ExpectedVocab("students", "entity", "users"),
                ExpectedVocab("account status", "field", "users.status"),
                ExpectedVocab("active", "enum_value", "users.status:active"),
            ),
            query="how many students",
            expected_sql='SELECT COUNT(*) FROM "users"',
        ),
        FrameworkFixture(
            framework="rails",
            files={
                "Gemfile": "gem 'rails'\n",
                "config/locales/en.yml": """en:
  activerecord:
    models:
      user:
        one: "Student"
        other: "Students"
    attributes:
      user:
        status: "Account Status"
""",
            },
            expected_vocab=(
                ExpectedVocab("students", "entity", "users"),
                ExpectedVocab("account status", "field", "users.status"),
            ),
            query="how many students",
            expected_sql='SELECT COUNT(*) FROM "users"',
        ),
        FrameworkFixture(
            framework="nextjs",
            files={
                "package.json": json.dumps({"dependencies": {"next": "^14.0.0"}}),
                "schemas/user.ts": """import { z } from "zod";
export const userSchema = z.object({
    status: z.string().describe("Account Status"),
});
""",
            },
            expected_vocab=(ExpectedVocab("account status", "field", "users.status"),),
        ),
        FrameworkFixture(
            framework="vue",
            files={
                "package.json": json.dumps({"dependencies": {"vue": "^3.4.0"}}),
                "src/components/UserForm.vue": """<template>
  <label for="status">Account Status</label>
  <input id="status" v-model="user.status" />
</template>
""",
            },
            expected_vocab=(ExpectedVocab("account status", "field", "users.status"),),
        ),
    )


def run_framework_bridge_probe(
    *,
    out_dir: Path,
    semsql_bin: Path,
    build_extractor: bool = False,
    repo_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if build_extractor:
        root = repo_root or Path.cwd()
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

    records: list[dict[str, Any]] = []
    for fixture in framework_bridge_fixtures():
        records.append(_run_one_fixture(out_dir, semsql_bin, fixture, env=env))

    passed = sum(1 for record in records if record["ok"])
    return {
        "schema_version": 1,
        "status": "pass" if passed == len(records) else "fail",
        "out_dir": str(out_dir),
        "semsql_bin": str(semsql_bin),
        "summary": {
            "frameworks": len(records),
            "passed": passed,
            "failed": len(records) - passed,
            "expected_vocab": sum(len(record["expected_vocab"]) for record in records),
            "matched_vocab": sum(record["matched_vocab_count"] for record in records),
            "query_checks": sum(1 for record in records if record["query"] is not None),
            "query_ok": sum(1 for record in records if record.get("query_ok") is True),
        },
        "records": records,
    }


def _run_one_fixture(
    out_dir: Path,
    semsql_bin: Path,
    fixture: FrameworkFixture,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    case_dir = out_dir / fixture.framework
    case_dir.mkdir(parents=True, exist_ok=True)
    db_path = case_dir / "app.sqlite"
    graph_path = case_dir / "app.semsql"
    _write_fixture_files(case_dir, fixture.files)
    _write_sqlite_schema(db_path)

    extract_cmd = [
        str(semsql_bin),
        "extract",
        str(case_dir),
        "--framework",
        fixture.framework,
        "--db-url",
        f"sqlite:{db_path}",
        "--output",
        str(graph_path),
        "--no-sample-values",
    ]
    extract = subprocess.run(
        extract_cmd,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    vocab = _read_vocab(graph_path) if extract.returncode == 0 else []
    expected = [
        {
            "term": item.term,
            "canonical_kind": item.canonical_kind,
            "canonical_value": item.canonical_value,
            "matched": _vocab_has(vocab, item),
        }
        for item in fixture.expected_vocab
    ]
    matched_vocab_count = sum(1 for item in expected if item["matched"])

    query_stdout: str | None = None
    query_returncode: int | None = None
    query_ok: bool | None = None
    if fixture.query is not None:
        query = subprocess.run(
            [
                str(semsql_bin),
                "query",
                "--graph",
                str(graph_path),
                "--dialect",
                "sqlite",
                fixture.query,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        query_returncode = query.returncode
        query_stdout = query.stdout.strip()
        first_line = query.stdout.splitlines()[0] if query.stdout.splitlines() else ""
        query_ok = query.returncode == 0 and first_line == fixture.expected_sql

    ok = (
        extract.returncode == 0
        and matched_vocab_count == len(fixture.expected_vocab)
        and (query_ok is not False)
    )
    return {
        "framework": fixture.framework,
        "ok": ok,
        "case_dir": str(case_dir),
        "graph": str(graph_path),
        "extract_returncode": extract.returncode,
        "extract_stdout": extract.stdout.strip(),
        "extract_stderr": extract.stderr.strip(),
        "expected_vocab": expected,
        "matched_vocab_count": matched_vocab_count,
        "query": fixture.query,
        "expected_sql": fixture.expected_sql,
        "query_returncode": query_returncode,
        "query_stdout": query_stdout,
        "query_ok": query_ok,
    }


def render_framework_bridge_probe_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Framework Extract Bridge Probe",
        "",
        f"- status: `{report['status'].upper()}`",
        f"- frameworks: `{summary['passed']}/{summary['frameworks']}`",
        f"- expected vocab matched: `{summary['matched_vocab']}/{summary['expected_vocab']}`",
        f"- query checks: `{summary['query_ok']}/{summary['query_checks']}`",
        f"- out: `{report['out_dir']}`",
        "",
        "| Framework | OK | Vocab | Query | Graph |",
        "|---|---:|---:|---:|---|",
    ]
    for record in report["records"]:
        vocab_total = len(record["expected_vocab"])
        query_cell = "n/a" if record["query"] is None else str(bool(record["query_ok"]))
        lines.append(
            "| `{framework}` | `{ok}` | `{matched}/{total}` | `{query}` | `{graph}` |".format(
                framework=record["framework"],
                ok=record["ok"],
                matched=record["matched_vocab_count"],
                total=vocab_total,
                query=query_cell,
                graph=record["graph"],
            )
        )
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "This checks source-alias ingestion and DB-grounded canonicalization.",
            "It does not prove broad framework app readiness or package-install availability.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_fixture_files(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _write_sqlite_schema(path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT,
            status TEXT,
            is_active INTEGER
        );
        INSERT INTO users (id, email, status, is_active)
        VALUES (1, 'one@example.test', 'active', 1);
        """
    )
    conn.close()


def _read_vocab(graph_path: Path) -> list[dict[str, str]]:
    conn = sqlite3.connect(graph_path)
    try:
        rows = conn.execute(
            "SELECT term, canonical_kind, canonical_value FROM vocabulary"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"term": str(term), "canonical_kind": str(kind), "canonical_value": str(value)}
        for term, kind, value in rows
    ]


def _vocab_has(vocab: list[dict[str, str]], expected: ExpectedVocab) -> bool:
    return any(
        row["term"] == expected.term
        and row["canonical_kind"] == expected.canonical_kind
        and row["canonical_value"] == expected.canonical_value
        for row in vocab
    )
