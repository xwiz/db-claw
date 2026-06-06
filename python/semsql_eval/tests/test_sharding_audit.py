from __future__ import annotations

from semsql_eval.sharding_audit import (
    ColumnMeta,
    TableMeta,
    audit_sharding_metadata,
    inspect_laravel_sharding_source,
    render_mysql_sharding_audit_markdown,
)


def test_mysql_sharding_audit_detects_families_and_drift() -> None:
    tables = [
        TableMeta("organizations", 3),
        TableMeta("mails", 0),
        TableMeta("mails_organizations_1", 10),
        TableMeta("mails_organizations_2", 5),
        TableMeta("mails_organizations_", 0),
        TableMeta("employees", 0),
        TableMeta("employees_organizations_1", 4),
        TableMeta("employees_organizations_1_organizations_1", 0),
    ]
    columns = [
        ColumnMeta("mails", "id", "char(42)", "NO", ""),
        ColumnMeta("mails", "subject", "varchar(255)", "YES", ""),
        ColumnMeta("mails", "vvs_key_version", "int(10)", "YES", ""),
        ColumnMeta("mails_organizations_1", "id", "char(42)", "NO", "PRI"),
        ColumnMeta("mails_organizations_1", "subject", "varchar(255)", "YES", ""),
        ColumnMeta("mails_organizations_2", "id", "char(42)", "NO", "PRI"),
        ColumnMeta("mails_organizations_2", "subject", "varchar(255)", "YES", ""),
        ColumnMeta("mails_organizations_2", "extra", "varchar(255)", "YES", ""),
        ColumnMeta("employees", "id", "char(42)", "NO", ""),
        ColumnMeta("employees", "quota", "bigint(20)", "YES", ""),
        ColumnMeta("employees_organizations_1", "id", "char(42)", "NO", "PRI"),
        ColumnMeta("employees_organizations_1", "quota", "varchar(255)", "YES", ""),
        ColumnMeta(
            "employees_organizations_1_organizations_1",
            "id",
            "char(42)",
            "NO",
            "PRI",
        ),
    ]

    report = audit_sharding_metadata(
        database="app",
        tables=tables,
        columns=columns,
        source={
            "inspected": True,
            "configured_models": ["Mail", "Employee", "Task"],
            "expected_base_tables": ["employees", "mails", "tasks"],
        },
    )

    assert report["status"] == "review"
    assert report["summary"]["shard_family_count"] == 3
    assert report["summary"]["active_shard_table_count"] == 3
    assert report["summary"]["active_ambiguous_family_count"] == 1
    assert report["summary"]["malformed_shard_table_count"] == 1
    assert report["summary"]["nested_shard_table_count"] == 1
    assert "tasks" in report["source_missing_families"]
    mails = next(family for family in report["families"] if family["base_table"] == "mails")
    assert mails["missing_columns_sample"] == ["vvs_key_version"]
    assert mails["extra_columns_sample"] == ["extra"]
    assert mails["active_physical_tables"] == [
        "mails_organizations_1",
        "mails_organizations_2",
    ]
    assert "active_table_ambiguity" in mails["review_reasons"]
    employees = next(
        family for family in report["families"] if family["base_table"] == "employees"
    )
    assert employees["type_drift_count"] == 1


def test_render_mysql_sharding_audit_markdown_summarizes_review() -> None:
    report = {
        "status": "review",
        "database": "app",
        "safety_note": "information_schema/source-only audit; no table data sampled",
        "summary": {
            "table_count": 3,
            "shard_family_count": 1,
            "shard_table_count": 2,
            "active_shard_table_count": 1,
            "active_ambiguous_family_count": 0,
            "malformed_shard_table_count": 0,
            "nested_shard_table_count": 0,
            "source_expected_family_count": 1,
            "needs_review": 1,
        },
        "source": {
            "inspected": True,
            "configured_models": ["Mail"],
            "expected_base_tables": ["mails"],
        },
        "families": [
            {
                "base_table": "mails",
                "anchor_table": "organizations",
                "shards": [{"table": "mails_organizations_1"}],
                "active_physical_tables": ["mails_organizations_1"],
                "approx_rows_total": 10,
                "type_drift_count": 2,
                "missing_columns_sample": ["vvs_key_version"],
                "extra_columns_sample": [],
                "review_reasons": ["missing_columns", "type_drift"],
            }
        ],
        "malformed_shard_tables": [],
        "nested_shard_tables": [],
        "source_missing_families": [],
        "source_extra_families": [],
    }

    rendered = render_mysql_sharding_audit_markdown(report)

    assert "status: `REVIEW`" in rendered
    assert "shard families: `1`" in rendered
    assert "`mails`" in rendered
    assert "`missing_columns, type_drift`" in rendered


def test_inspect_laravel_sharding_source_reads_config(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "app" / "Models").mkdir(parents=True)
    (tmp_path / "config" / "sharding.php").write_text(
        "<?php return ['models' => [App\\Models\\Mail::class, "
        "App\\Models\\MailAttachment::class, App\\Models\\MailAlias::class]];",
        encoding="utf-8",
    )
    (tmp_path / "app" / "Models" / "Mail.php").write_text(
        "<?php class Mail { use Shardable; }",
        encoding="utf-8",
    )

    source = inspect_laravel_sharding_source(tmp_path)

    assert source["inspected"] is True
    assert source["configured_models"] == ["Mail", "MailAlias", "MailAttachment"]
    assert source["expected_base_tables"] == [
        "mail_aliases",
        "mail_attachments",
        "mails",
    ]
    assert source["shardable_model_files"] == ["Mail"]
