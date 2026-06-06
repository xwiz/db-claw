from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "audit_static_query_shortcuts.py"
    spec = importlib.util.spec_from_file_location("audit_static_query_shortcuts", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_static_shortcut_audit_ignores_test_modules(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        """
fn runtime() {}

#[cfg(test)]
mod tests {
    const FIXTURE: &str = "Marvel Comics";
}
""",
        encoding="utf-8",
    )

    assert module.audit_static_query_shortcuts(tmp_path) == []


def test_static_shortcut_audit_flags_production_literals(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text('const BAD: &str = "Marvel Comics";\n', encoding="utf-8")

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert findings[0].pattern == "Marvel Comics"


def test_static_shortcut_audit_flags_student_enrollment_pattern(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        """
fn bad(tokens: &mut HashSet<String>) {
    if tokens.contains("students") {
        tokens.insert("enrollment".to_string());
    }
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "students" in findings[0].pattern


def test_static_shortcut_audit_flags_publisher_context_bonus(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        """
fn bad(lower_context: &str) -> f32 {
    if lower_context.contains("publisher") {
        return 5.0;
    }
    0.0
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "publisher" in findings[0].pattern


def test_static_shortcut_audit_flags_charter_projection_bonus(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        """
fn bad(lower_nl: &str, tokens: HashSet<String>) -> f32 {
    if lower_nl.contains("charter") && tokens.contains("charter") {
        return 6.0;
    }
    0.0
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "charter" in findings[0].pattern


def test_static_shortcut_audit_flags_attribute_concept_literals(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "lib.rs").write_text(
        'const BAD_TRIGGER: &str = "least intelligent";\n',
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert findings[0].pattern == "least intelligent"


def test_static_shortcut_audit_flags_cdscode_projection_bonus(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "stage_slotfiller.rs").write_text(
        """
fn bad(tail_lower: &str) -> bool {
    tail_lower.contains("cdscode")
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "cdscode" in findings[0].pattern


def test_static_shortcut_audit_flags_school_name_projection_bonus(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "stage_slotfiller.rs").write_text(
        """
fn bad(tail_lower: &str) -> bool {
    tail_lower == "school" || tail_lower == "sname"
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "school" in findings[0].pattern


def test_static_shortcut_audit_flags_school_student_stopword_bundle(tmp_path: Path) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-runtime" / "src"
    src.mkdir(parents=True)
    (src / "stage_slotfiller.rs").write_text(
        'const STOP: &[&str] = &["school", "schools", "student", "students"];\n',
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "student" in findings[0].pattern


def test_static_shortcut_audit_flags_cli_code_compatibility_domain_terms(
    tmp_path: Path,
) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-cli" / "src"
    src.mkdir(parents=True)
    (src / "main.rs").write_text(
        """
fn bad(kind: &str) -> bool {
    match kind {
        "code" => ["cds", "charter", "code"].iter().any(|needle| *needle == "x"),
        _ => false,
    }
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 2
    assert {finding.pattern for finding in findings} == {
        r'"code"\s*=>\s*\[[^\]]*"cds"',
        r'"code"\s*=>\s*\[[^\]]*"charter"',
    }


def test_static_shortcut_audit_flags_cli_enum_charter_compatibility(
    tmp_path: Path,
) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-cli" / "src"
    src.mkdir(parents=True)
    (src / "main.rs").write_text(
        """
fn bad(kind: &str) -> bool {
    match kind {
        "enum_keyword" => ["category", "charter"].iter().any(|needle| *needle == "x"),
        _ => false,
    }
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "enum_keyword" in findings[0].pattern


def test_static_shortcut_audit_flags_cli_phrase_school_compatibility(
    tmp_path: Path,
) -> None:
    module = _load_script()
    src = tmp_path / "crates" / "semsql-cli" / "src"
    src.mkdir(parents=True)
    (src / "main.rs").write_text(
        """
fn bad(kind: &str) -> bool {
    match kind {
        "phrase" | "quoted_string" => ["name", "school"].iter().any(|needle| *needle == "x"),
        _ => false,
    }
}
""",
        encoding="utf-8",
    )

    findings = module.audit_static_query_shortcuts(tmp_path)

    assert len(findings) == 1
    assert "quoted_string" in findings[0].pattern
