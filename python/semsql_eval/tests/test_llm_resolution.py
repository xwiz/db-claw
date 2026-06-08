from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from semsql_eval.llm_resolution import (
    DEFAULT_GROQ_BASE_URL,
    DEFAULT_OPENAI_MODEL,
    build_openai_chat_resolution_request,
    build_openai_resolution_request,
    build_openai_resolution_request_batch,
    build_pathway_rejected_query_packets,
    build_rejected_query_packet,
    build_runtime_frame_resolution_proposal,
    build_schema_card,
    call_openai_chat_compatible_resolution,
    compact_resolution_packet_for_provider,
    evaluate_resolution_safety_expectations,
    render_openai_request_batch_markdown,
    render_resolution_batch_markdown,
    render_resolution_proposal,
    render_resolution_proposal_batch,
    render_resolution_provider_batch_markdown,
    render_resolution_safety_expectations_markdown,
    render_schema_card_markdown,
    resolution_json_schema,
    resolve_resolution_proposal_batch,
    validate_resolution_proposal,
)


def _make_graph(
    path: Path,
    *,
    include_vocabulary: bool = True,
    include_shards: bool = False,
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO semsql_metadata VALUES ('schema_version', '1');

        CREATE TABLE entities (
            canonical_name TEXT PRIMARY KEY,
            db_table TEXT NOT NULL,
            db_schema TEXT,
            singular_label TEXT,
            plural_label TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE fields (
            entity TEXT NOT NULL,
            field TEXT NOT NULL,
            db_column TEXT NOT NULL,
            type TEXT NOT NULL,
            display_label TEXT,
            enum_canonical TEXT,
            unit_canonical TEXT,
            proto_blob BLOB NOT NULL DEFAULT X'',
            PRIMARY KEY (entity, field)
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT NOT NULL,
            from_field TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            to_field TEXT NOT NULL,
            kind TEXT NOT NULL,
            relation_name TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE sample_values (
            field_canonical TEXT PRIMARY KEY,
            examples TEXT NOT NULL,
            pii_redacted INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    if include_vocabulary:
        conn.executescript(
            """
            CREATE TABLE vocabulary (
                term TEXT NOT NULL,
                canonical_kind TEXT NOT NULL,
                canonical_value TEXT NOT NULL,
                confidence REAL NOT NULL,
                source_layer INTEGER NOT NULL,
                source_locator TEXT,
                PRIMARY KEY (term, canonical_kind, canonical_value)
            );
            """
        )
    entities = ["organizations", "mails"]
    if include_shards:
        entities.extend(["mails_organizations_1", "mails_organizations_2"])
    for entity in entities:
        conn.execute(
            "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label) "
            "VALUES (?, ?, ?, ?)",
            (entity, entity, entity.rstrip("s"), entity),
        )
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) VALUES (?, ?, ?, ?, ?)",
        [
            ("organizations", "id", "id", "integer", "ID"),
            ("organizations", "name", "name", "text", "Name"),
            ("mails", "id", "id", "integer", "ID"),
            ("mails", "subject", "subject", "text", "Subject"),
            ("mails", "status", "status", "text", "Status"),
            ("mails", "ordered_on", "ordered_on", "text", "Ordered On"),
            ("mails", "joined_on", "joined_on", "text", "Joined On"),
            ("mails", "organization_id", "organization_id", "integer", "Organization ID"),
            *(
                [
                    ("mails_organizations_1", "id", "id", "integer", "ID"),
                    ("mails_organizations_2", "id", "id", "integer", "ID"),
                ]
                if include_shards
                else []
            ),
        ],
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('mails', 'organization_id', 'organizations', 'id', 'many_to_one')"
    )
    conn.execute(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES ('mails.status', '[\"sent\", \"draft\"]', 0)"
    )
    if include_vocabulary:
        conn.executemany(
            "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
            "VALUES (?, 'scope_predicate', ?, ?, 2)",
            [
                (
                    "sent",
                    json.dumps(
                        {
                            "scope": "mails.status.sent",
                            "field": "mails.status",
                            "operator": "=",
                            "rawValue": "sent",
                        }
                    ),
                    0.88,
                ),
                (
                    "draft",
                    json.dumps(
                        {
                            "scope": "mails.status.draft",
                            "field": "mails.status",
                            "operator": "=",
                            "rawValue": "draft",
                        }
                    ),
                    0.88,
                ),
            ],
        )
    conn.commit()
    conn.close()


def _make_ambiguous_value_graph(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO semsql_metadata VALUES ('schema_version', '1');

        CREATE TABLE entities (
            canonical_name TEXT PRIMARY KEY,
            db_table TEXT NOT NULL,
            db_schema TEXT,
            singular_label TEXT,
            plural_label TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE fields (
            entity TEXT NOT NULL,
            field TEXT NOT NULL,
            db_column TEXT NOT NULL,
            type TEXT NOT NULL,
            display_label TEXT,
            enum_canonical TEXT,
            unit_canonical TEXT,
            proto_blob BLOB NOT NULL DEFAULT X'',
            PRIMARY KEY (entity, field)
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT NOT NULL,
            from_field TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            to_field TEXT NOT NULL,
            kind TEXT NOT NULL,
            relation_name TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE sample_values (
            field_canonical TEXT PRIMARY KEY,
            examples TEXT NOT NULL,
            pii_redacted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE vocabulary (
            term TEXT NOT NULL,
            canonical_kind TEXT NOT NULL,
            canonical_value TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_layer INTEGER NOT NULL,
            source_locator TEXT,
            PRIMARY KEY (term, canonical_kind, canonical_value)
        );
        """
    )
    conn.executemany(
        "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label) "
        "VALUES (?, ?, ?, ?)",
        [
            ("customers", "customers", "customer", "customers"),
            ("orders", "orders", "order", "orders"),
            ("regions", "regions", "region", "regions"),
        ],
    )
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("customers", "id", "id", "integer", "ID"),
            ("customers", "name", "name", "text", "Name"),
            ("customers", "region", "region", "text", "Region"),
            ("customers", "segment", "segment", "text", "Segment"),
            ("orders", "id", "id", "integer", "ID"),
            ("orders", "customer_id", "customer_id", "integer", "Customer ID"),
            ("orders", "region", "region", "text", "Region"),
            ("orders", "segment", "segment", "text", "Segment"),
            ("orders", "amount", "amount", "decimal", "Amount"),
            ("regions", "id", "id", "integer", "ID"),
            ("regions", "name", "name", "text", "Name"),
        ],
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('orders', 'customer_id', 'customers', 'id', 'many_to_one')"
    )
    conn.executemany(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES (?, ?, 0)",
        [
            ("customers.region", '["North", "South"]'),
            ("customers.segment", '["Priority", "Standard"]'),
            ("orders.region", '["North", "East"]'),
            ("orders.segment", '["Priority", "Backlog"]'),
            ("regions.name", '["North", "West"]'),
        ],
    )
    conn.executemany(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES (?, 'scope_predicate', ?, ?, 2)",
        [
            (
                "north",
                json.dumps(
                    {
                        "scope": "orders.region.north",
                        "field": "orders.region",
                        "operator": "=",
                        "rawValue": "North",
                    }
                ),
                0.9,
            ),
            (
                "north",
                json.dumps(
                    {
                        "scope": "customers.region.north",
                        "field": "customers.region",
                        "operator": "=",
                        "rawValue": "North",
                    }
                ),
                0.9,
            ),
        ],
    )
    conn.commit()
    conn.close()


def _make_sensitive_graph(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO semsql_metadata VALUES ('schema_version', '1');

        CREATE TABLE entities (
            canonical_name TEXT PRIMARY KEY,
            db_table TEXT NOT NULL,
            db_schema TEXT,
            singular_label TEXT,
            plural_label TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE fields (
            entity TEXT NOT NULL,
            field TEXT NOT NULL,
            db_column TEXT NOT NULL,
            type TEXT NOT NULL,
            display_label TEXT,
            enum_canonical TEXT,
            unit_canonical TEXT,
            proto_blob BLOB NOT NULL DEFAULT X'',
            PRIMARY KEY (entity, field)
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT NOT NULL,
            from_field TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            to_field TEXT NOT NULL,
            kind TEXT NOT NULL,
            relation_name TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE sample_values (
            field_canonical TEXT PRIMARY KEY,
            examples TEXT NOT NULL,
            pii_redacted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE vocabulary (
            term TEXT NOT NULL,
            canonical_kind TEXT NOT NULL,
            canonical_value TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_layer INTEGER NOT NULL,
            source_locator TEXT,
            PRIMARY KEY (term, canonical_kind, canonical_value)
        );
        """
    )
    conn.executemany(
        "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label) "
        "VALUES (?, ?, ?, ?)",
        [
            ("users", "users", "user", "users"),
            ("api_tokens", "api_tokens", "api token", "api tokens"),
        ],
    )
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) VALUES (?, ?, ?, ?, ?)",
        [
            ("users", "id", "id", "integer", "ID"),
            ("users", "name", "name", "text", "Name"),
            ("api_tokens", "id", "id", "integer", "ID"),
            ("api_tokens", "user_id", "user_id", "integer", "User ID"),
            ("api_tokens", "secret_token", "secret_token", "text", "Token"),
        ],
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('api_tokens', 'user_id', 'users', 'id', 'many_to_one')"
    )
    conn.commit()
    conn.close()


def _make_leads_graph(path: Path, *, extra_lead_date: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO semsql_metadata VALUES ('schema_version', '1');

        CREATE TABLE entities (
            canonical_name TEXT PRIMARY KEY,
            db_table TEXT NOT NULL,
            db_schema TEXT,
            singular_label TEXT,
            plural_label TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE fields (
            entity TEXT NOT NULL,
            field TEXT NOT NULL,
            db_column TEXT NOT NULL,
            type TEXT NOT NULL,
            display_label TEXT,
            enum_canonical TEXT,
            unit_canonical TEXT,
            proto_blob BLOB NOT NULL DEFAULT X'',
            PRIMARY KEY (entity, field)
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT NOT NULL,
            from_field TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            to_field TEXT NOT NULL,
            kind TEXT NOT NULL,
            relation_name TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE sample_values (
            field_canonical TEXT PRIMARY KEY,
            examples TEXT NOT NULL,
            pii_redacted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE vocabulary (
            term TEXT NOT NULL,
            canonical_kind TEXT NOT NULL,
            canonical_value TEXT NOT NULL,
            confidence REAL NOT NULL,
            source_layer INTEGER NOT NULL,
            source_locator TEXT,
            PRIMARY KEY (term, canonical_kind, canonical_value)
        );
        CREATE TABLE metric_definitions (
            name TEXT PRIMARY KEY,
            display_label TEXT,
            metric_kind TEXT NOT NULL,
            subject_entity TEXT NOT NULL,
            numerator_field TEXT NOT NULL,
            numerator_operator TEXT NOT NULL,
            numerator_value TEXT NOT NULL,
            numerator_value_kind TEXT NOT NULL,
            denominator_field TEXT NOT NULL,
            scale REAL NOT NULL,
            required_entities_json TEXT NOT NULL,
            aliases_json TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label) "
        "VALUES (?, ?, ?, ?)",
        [
            ("leads", "leads", "lead", "leads"),
            ("campaigns", "campaigns", "campaign", "campaigns"),
            ("organizations", "organizations", "organization", "organizations"),
        ],
    )
    lead_fields = [
        ("leads", "id", "id", "integer", "ID"),
        ("leads", "status", "status", "text", "Status"),
        ("leads", "acquisition_channel", "acquisition_channel", "text", "Source Channel"),
        ("leads", "captured_on", "captured_on", "text", "Created On"),
        ("leads", "program_id", "program_id", "integer", "Campaign ID"),
        (
            "leads",
            "converted_organization_id",
            "converted_organization_id",
            "integer",
            "Converted Account ID",
        ),
        ("campaigns", "id", "id", "integer", "ID"),
        ("campaigns", "program_name", "program_name", "text", "Campaign Name"),
        ("campaigns", "launch_date", "launch_date", "text", "Launch Date"),
        ("organizations", "id", "id", "integer", "ID"),
        ("organizations", "lifecycle_state", "lifecycle_state", "text", "Lifecycle State"),
    ]
    if extra_lead_date:
        lead_fields.append(("leads", "qualified_date", "qualified_date", "text", "Qualified Date"))
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) VALUES (?, ?, ?, ?, ?)",
        lead_fields,
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('leads', 'program_id', 'campaigns', 'id', 'many_to_one')"
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('leads', 'converted_organization_id', 'organizations', 'id', 'many_to_one')"
    )
    conn.executemany(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES (?, 'scope_predicate', ?, 0.88, 2)",
        [
            (
                "new",
                json.dumps(
                    {
                        "scope": "leads.status.new",
                        "field": "leads.status",
                        "operator": "=",
                        "rawValue": "new",
                    }
                ),
            ),
            (
                "converted",
                json.dumps(
                    {
                        "scope": "leads.status.converted",
                        "field": "leads.status",
                        "operator": "=",
                        "rawValue": "converted",
                    }
                ),
            ),
            (
                "paid search",
                json.dumps(
                    {
                        "scope": "leads.acquisition_channel.paid_search",
                        "field": "leads.acquisition_channel",
                        "operator": "=",
                        "rawValue": "paid_search",
                    }
                ),
            ),
            (
                "customer",
                json.dumps(
                    {
                        "scope": "organizations.lifecycle_state.customer",
                        "field": "organizations.lifecycle_state",
                        "operator": "=",
                        "rawValue": "customer",
                    }
                ),
            ),
        ],
    )
    conn.execute(
        "INSERT INTO metric_definitions("
        "name, display_label, metric_kind, subject_entity, numerator_field, "
        "numerator_operator, numerator_value, numerator_value_kind, "
        "denominator_field, scale, required_entities_json, aliases_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "lead_to_customer_conversion_rate",
            "Lead to customer conversion rate",
            "conditional_rate",
            "leads",
            "leads.status",
            "=",
            "converted",
            "value_dictionary",
            "leads.id",
            100.0,
            json.dumps(["leads"]),
            json.dumps(
                [
                    "lead to customer conversion rate",
                    "lead conversion rate",
                    "prospect conversion rate",
                ]
            ),
        ),
    )
    conn.commit()
    conn.close()


def test_schema_card_summarizes_graph_and_shard_family(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph, include_shards=True)

    card = build_schema_card(graph)

    assert card["summary"]["entity_count"] == 4
    assert card["summary"]["physical_table_family_count"] == 1
    assert card["summary"]["ambiguous_physical_family_count"] == 1
    assert card["summary"]["table_activity_hint_count"] == 4
    assert card["summary"]["value_dictionary_count"] == 2
    mails = next(entity for entity in card["entities"] if entity["name"] == "mails")
    assert "subject" in mails["display_fields"]
    assert "status" in mails["status_fields"]
    assert "ordered_on" in mails["date_fields"]
    assert "joined_on" in mails["date_fields"]
    assert mails["fields"][0]["samples"] == []
    assert mails["table_activity_hint"]["evidence_source"] == "graph_metadata_only"
    assert mails["table_activity_hint"]["evidence_level"] == "strong"
    assert mails["table_activity_hint"]["sample_value_field_count"] == 1
    assert mails["table_activity_hint"]["value_dictionary_term_count"] == 2
    assert mails["table_activity_hint"]["relationship_count"] == 1
    shard = next(entity for entity in card["entities"] if entity["name"] == "mails_organizations_1")
    assert (
        mails["table_activity_hint"]["evidence_score"]
        > shard["table_activity_hint"]["evidence_score"]
    )
    assert card["physical_table_families"][0]["base_table"] == "mails"
    assert card["physical_table_families"][0]["requires_clarification"] is True
    assert card["physical_table_families"][0]["members"][0]["entity"] == "mails"
    assert card["physical_table_families"][0]["members"][0]["role"] == "base_table"
    assert (
        card["physical_table_families"][0]["members"][0]["table_activity_hint"][
            "evidence_level"
        ]
        == "strong"
    )
    assert card["shard_families"][0]["base"] == "mails"
    assert card["shard_families"][0]["ambiguous_without_anchor"] is True

    rendered = render_schema_card_markdown(card)
    assert "ambiguous physical families: `1`" in rendered
    assert "table activity hints: `4`" in rendered
    assert "value dictionary terms: `2`" in rendered
    assert "`mails` via `organizations`" in rendered


def test_rejected_query_packet_marks_ambiguous_family(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph, include_shards=True)

    packet = build_rejected_query_packet(
        graph,
        "show mails sent today",
        route_reason="not_routed_ambiguous_physical_table_family",
    )

    assert packet["question"] == "show mails sent today"
    assert packet["allowed_resolution_contract"]["must_not_emit_final_sql"] is True
    assert (
        packet["allowed_resolution_contract"][
            "must_clarify_ambiguous_physical_table_families"
        ]
        is True
    )
    assert (
        packet["allowed_resolution_contract"][
            "must_clarify_ambiguous_physical_shard_families"
        ]
        is True
    )
    assert packet["allowed_resolution_contract"]["must_not_route_sensitive_schema"] is True
    assert packet["allowed_resolution_contract"]["row_list_routes_are_locally_capped"] is True
    families = packet["local_candidates"]["ambiguous_physical_families_mentioned"]
    assert len(families) == 1
    assert families[0]["base_table"] == "mails"
    assert families[0]["base"] == "mails"
    assert families[0]["requires_clarification"] is True
    value_hit = next(
        hit
        for hit in packet["local_candidates"]["value_dictionary_hits"]
        if hit["field"] == "mails.status"
    )
    assert value_hit["term"] == "sent"
    assert value_hit["operator"] == "="
    assert value_hit["raw_value"] == "sent"


def test_rejected_query_packet_exposes_scope_path_candidates(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)

    packet = build_rejected_query_packet(
        graph,
        "count sent mails by organization",
    )

    assert packet["local_candidates"]["scope_path_ambiguous"] is False
    assert packet["local_candidates"]["scope_path_candidates"] == [
        {
            "subject_entity": "mails",
            "scope_entity": "organizations",
            "matched_scope_terms": ["organization"],
            "relationships": [
                {
                    "from": "mails.organization_id",
                    "to": "organizations.id",
                    "kind": "many_to_one",
                }
            ],
            "path_length": 1,
        }
    ]


def test_rejected_query_packet_exposes_ambiguous_scope_paths(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'owner_organization_id', 'owner_organization_id', "
        "'integer', 'Owner Organization ID')"
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('mails', 'owner_organization_id', 'organizations', 'id', 'many_to_one')"
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(
        graph,
        "count sent mails by organization",
    )

    assert packet["local_candidates"]["scope_path_ambiguous"] is True
    relationships = [
        candidate["relationships"][0]
        for candidate in packet["local_candidates"]["scope_path_candidates"]
    ]
    assert relationships == [
        {
            "from": "mails.organization_id",
            "to": "organizations.id",
            "kind": "many_to_one",
        },
        {
            "from": "mails.owner_organization_id",
            "to": "organizations.id",
            "kind": "many_to_one",
        },
    ]
    mails = next(entity for entity in packet["schema_card"]["entities"] if entity["name"] == "mails")
    assert {"organization_id", "owner_organization_id"} <= {
        field["name"] for field in mails["fields"]
    }


def test_resolution_proposal_rejects_ambiguous_shard_family_route(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph, include_shards=True)
    packet = build_rejected_query_packet(graph, "show mails sent today")

    validation = validate_resolution_proposal(packet, _valid_sent_mail_proposal())
    result = render_resolution_proposal(packet, _valid_sent_mail_proposal())

    assert validation["valid"] is False
    assert "ambiguous_shard_family_route" in {
        issue["code"] for issue in validation["issues"]
    }
    assert result["valid"] is False
    assert result["sql"] is None


def test_resolution_proposal_rejects_cli_style_physical_family_packet(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph, include_shards=True)
    packet = build_rejected_query_packet(graph, "show mails sent today")
    packet["schema_card"].pop("shard_families", None)
    family = packet["schema_card"]["physical_table_families"][0]
    packet["local_candidates"]["ambiguous_physical_families_mentioned"] = [
        {
            "base_table": family["base_table"],
            "anchor": family["anchor"],
            "member_count": family["member_count"],
            "matched_tokens": ["mail", "mails"],
            "requires_clarification": True,
        }
    ]

    validation = validate_resolution_proposal(packet, _valid_sent_mail_proposal())

    assert validation["valid"] is False
    assert "ambiguous_shard_family_route" in {
        issue["code"] for issue in validation["issues"]
    }


def test_pathway_rejected_packet_batch_uses_fail_closed_route_rows(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    graph = bench / "graphs" / "mail_suite.semsql"
    graph.parent.mkdir(parents=True)
    _make_graph(graph)
    frame = bench / "frames" / "mail" / "001-mail.json"
    frame.parent.mkdir(parents=True)
    frame.write_text(
        json.dumps({"runtime_query_frame": {"route_reason": "not_routed_complex_shape"}}),
        encoding="utf-8",
    )
    report_json = tmp_path / "report.json"
    report_json.write_text(
        json.dumps(
            {
                "benchmark": "pathway-decision-v1",
                "out_dir": str(bench),
                "suites": [{"suite": "mail", "db_id": "mail_suite"}],
                "cases": [
                    {
                        "suite": "mail",
                        "id": "mail001",
                        "question": "count sent mails by organization",
                        "disposition": "route",
                        "family": "grouped_count",
                        "difficulty": "medium",
                        "query_frame_path": str(frame),
                        "features": {
                            "bound_plan_reject_reason": "not_routed_complex_shape"
                        },
                        "policies": {"bound_plan": {"bucket": "fail_closed"}},
                    },
                    {
                        "suite": "mail",
                        "id": "mail002",
                        "question": "show sent mails",
                        "disposition": "route",
                        "family": "lookup",
                        "difficulty": "easy",
                        "policies": {"bound_plan": {"bucket": "correct"}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = build_pathway_rejected_query_packets(
        report_json,
        tmp_path / "packets",
        policy="bound_plan",
    )

    assert summary["packet_count"] == 1
    packet_path = Path(summary["packets"][0]["packet_path"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["question"] == "count sent mails by organization"
    assert packet["route_reason"] == "not_routed_complex_shape"
    assert packet["query_frame"]["runtime_query_frame"]["route_reason"] == (
        "not_routed_complex_shape"
    )
    assert packet["pathway_case"]["case_id"] == "mail001"
    assert (tmp_path / "packets" / "index.json").exists()


def test_resolution_proposal_batch_renders_matching_packets(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    (packet_dir / "mail001.packet.json").write_text(
        json.dumps(packet),
        encoding="utf-8",
    )
    (packet_dir / "mail001.proposal.json").write_text(
        json.dumps(proposal),
        encoding="utf-8",
    )

    summary = render_resolution_proposal_batch(packet_dir)
    rendered = render_resolution_batch_markdown(summary)

    assert summary["packet_count"] == 1
    assert summary["valid_count"] == 1
    assert summary["invalid_count"] == 0
    assert summary["missing_proposal_count"] == 0
    assert summary["result_shape_counts"] == {"table": 1}
    assert summary["shape_missing_declared_count"] == 1
    assert summary["shape_mismatch_count"] == 0
    assert summary["cases"][0]["shape_contract"] == "missing_declared"
    assert (packet_dir / "mail001.render.json").exists()
    assert "`mail001`" in rendered
    assert "missing declared shape: `1`" in rendered


def test_resolution_proposal_batch_counts_shape_mismatches(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["result_shape"] = "multi_series_chart"
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    (packet_dir / "mail001.packet.json").write_text(
        json.dumps(packet),
        encoding="utf-8",
    )
    (packet_dir / "mail001.proposal.json").write_text(
        json.dumps(proposal),
        encoding="utf-8",
    )

    summary = render_resolution_proposal_batch(packet_dir)

    assert summary["valid_count"] == 0
    assert summary["invalid_count"] == 1
    assert summary["result_shape_counts"] == {"table": 1}
    assert summary["shape_mismatch_count"] == 1
    assert summary["shape_contract_counts"] == {"mismatch": 1}
    assert summary["cases"][0]["declared_result_shape"] == "multi_series_chart"
    assert summary["cases"][0]["result_shape_kind"] == "table"
    assert "result_shape_mismatch" in summary["cases"][0]["issue_codes"]


def test_resolution_proposal_batch_tracks_bi_shape_contracts(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "leads.semsql"
    _make_leads_graph(graph)
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()

    cases = {
        "leads-by-channel": (
            "How many leads came from each acquisition channel?",
            {
                "schema_version": 1,
                "action": "route",
                "confidence": 0.9,
                "intent": "count leads by acquisition channel",
                "result_shape": "categorical_chart",
                "target_entities": ["leads"],
                "projections": [
                    {
                        "kind": "count",
                        "field": "",
                        "aggregate": "COUNT",
                        "alias": "lead_count",
                        "rationale": "count leads in each channel",
                    }
                ],
                "filters": [],
                "joins": [],
                "group_by": ["leads.acquisition_channel"],
                "order_by": [],
                "limit": 0,
                "ambiguity_questions": [],
                "evidence": [],
                "safety_notes": [],
            },
        ),
        "leads-over-time": (
            "How many leads were captured over time?",
            {
                "schema_version": 1,
                "action": "route",
                "confidence": 0.9,
                "intent": "count leads over captured date",
                "result_shape": "time_series_chart",
                "target_entities": ["leads"],
                "projections": [
                    {
                        "kind": "count",
                        "field": "",
                        "aggregate": "COUNT",
                        "alias": "lead_count",
                        "rationale": "count leads on each captured date",
                    }
                ],
                "filters": [],
                "joins": [],
                "group_by": ["leads.captured_on"],
                "order_by": [],
                "limit": 0,
                "ambiguity_questions": [],
                "evidence": [],
                "safety_notes": [],
            },
        ),
        "leads-channel-over-time": (
            "How many leads by channel over time?",
            {
                "schema_version": 1,
                "action": "route",
                "confidence": 0.9,
                "intent": "count leads by acquisition channel over captured date",
                "result_shape": "multi_series_chart",
                "target_entities": ["leads"],
                "projections": [
                    {
                        "kind": "count",
                        "field": "",
                        "aggregate": "COUNT",
                        "alias": "lead_count",
                        "rationale": "count leads by channel per captured date",
                    }
                ],
                "filters": [],
                "joins": [],
                "group_by": ["leads.captured_on", "leads.acquisition_channel"],
                "order_by": [],
                "limit": 0,
                "ambiguity_questions": [],
                "evidence": [],
                "safety_notes": [],
            },
        ),
        "bad-multiseries": (
            "How many leads by channel over time?",
            {
                "schema_version": 1,
                "action": "route",
                "confidence": 0.9,
                "intent": "incorrectly shaped lead count by channel",
                "result_shape": "multi_series_chart",
                "target_entities": ["leads"],
                "projections": [
                    {
                        "kind": "count",
                        "field": "",
                        "aggregate": "COUNT",
                        "alias": "lead_count",
                        "rationale": "missing the time group required for multi-series",
                    }
                ],
                "filters": [],
                "joins": [],
                "group_by": ["leads.acquisition_channel"],
                "order_by": [],
                "limit": 0,
                "ambiguity_questions": [],
                "evidence": [],
                "safety_notes": [],
            },
        ),
    }
    for stem, (question, proposal) in cases.items():
        packet = build_rejected_query_packet(graph, question)
        (packet_dir / f"{stem}.packet.json").write_text(
            json.dumps(packet),
            encoding="utf-8",
        )
        (packet_dir / f"{stem}.proposal.json").write_text(
            json.dumps(proposal),
            encoding="utf-8",
        )

    summary = render_resolution_proposal_batch(packet_dir)

    assert summary["packet_count"] == 4
    assert summary["valid_count"] == 3
    assert summary["invalid_count"] == 1
    assert summary["shape_match_count"] == 3
    assert summary["shape_mismatch_count"] == 1
    assert summary["shape_missing_declared_count"] == 0
    assert summary["result_shape_counts"] == {
        "categorical_chart": 2,
        "multi_series_chart": 1,
        "time_series_chart": 1,
    }
    by_stem = {case["stem"]: case for case in summary["cases"]}
    assert by_stem["bad-multiseries"]["shape_contract"] == "mismatch"
    assert by_stem["bad-multiseries"]["result_shape_kind"] == "categorical_chart"


def test_resolution_provider_batch_writes_proposal_and_render(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    (packet_dir / "mail001.packet.json").write_text(
        json.dumps(packet),
        encoding="utf-8",
    )

    def fake_provider(packet_payload: dict[str, object]) -> dict[str, object]:
        assert packet_payload["question"] == "show sent mail subjects"
        proposal = _valid_sent_mail_proposal()
        proposal["result_shape"] = "table"
        return {
            "schema_version": 1,
            "source": "fake_provider",
            "proposal": proposal,
        }

    summary = resolve_resolution_proposal_batch(
        packet_dir,
        provider=fake_provider,
        provider_name="fake",
    )
    rendered = render_resolution_provider_batch_markdown(summary)

    assert summary["packet_count"] == 1
    assert summary["provider_call_count"] == 1
    assert summary["used_existing_proposal_count"] == 0
    assert summary["valid_count"] == 1
    assert summary["invalid_count"] == 0
    assert summary["provider_error_count"] == 0
    assert summary["shape_match_count"] == 1
    assert summary["shape_mismatch_count"] == 0
    assert summary["shape_missing_declared_count"] == 0
    assert summary["cases"][0]["shape_contract"] == "matched"
    assert (packet_dir / "mail001.fake.json").exists()
    assert (packet_dir / "mail001.proposal.json").exists()
    assert (packet_dir / "mail001.render.json").exists()
    assert "provider calls: `1`" in rendered
    assert "shape matches: `1`" in rendered


def test_resolution_provider_batch_reuses_existing_proposal(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    (packet_dir / "mail001.packet.json").write_text(
        json.dumps(packet),
        encoding="utf-8",
    )
    (packet_dir / "mail001.proposal.json").write_text(
        json.dumps(_valid_sent_mail_proposal()),
        encoding="utf-8",
    )

    def provider_should_not_run(_: dict[str, object]) -> dict[str, object]:
        raise AssertionError("provider should not run when proposal exists")

    summary = resolve_resolution_proposal_batch(
        packet_dir,
        provider=provider_should_not_run,
        provider_name="fake",
    )

    assert summary["provider_call_count"] == 0
    assert summary["used_existing_proposal_count"] == 1
    assert summary["valid_count"] == 1
    assert summary["provider_error_count"] == 0


def test_resolution_safety_gate_accepts_mixed_expected_outcomes(
    tmp_path: Path,
) -> None:
    route_render = tmp_path / "route.render.json"
    clarify_render = tmp_path / "clarify.render.json"
    invalid_render = tmp_path / "invalid.render.json"
    route_render.write_text(
        json.dumps(
            {
                "valid": True,
                "sql": 'SELECT "mails"."subject" FROM "mails"',
                "proposal_action": "route",
                "effective_action": "route",
                "result_shape": {"kind": "table"},
                "validation": {"valid": True},
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    clarify_render.write_text(
        json.dumps(
            {
                "valid": False,
                "sql": None,
                "proposal_action": "clarify",
                "effective_action": "clarify",
                "validation": {"valid": True},
                "issues": [{"code": "proposal_not_route"}],
            }
        ),
        encoding="utf-8",
    )
    invalid_render.write_text(
        json.dumps(
            {
                "valid": False,
                "sql": None,
                "proposal_action": "clarify",
                "effective_action": "clarify",
                "validation": {"valid": False},
                "issues": [{"code": "proposal_validation_failed"}],
            }
        ),
        encoding="utf-8",
    )
    summary = {
        "source": "semsql_resolution_provider_batch",
        "cases": [
            {
                "stem": "route",
                "valid": True,
                "render_path": str(route_render),
                "result_shape_kind": "table",
                "selected_source": "typed_fallback",
            },
            {
                "stem": "clarify",
                "valid": False,
                "render_path": str(clarify_render),
            },
            {
                "stem": "invalid",
                "valid": False,
                "render_path": str(invalid_render),
            },
            {
                "stem": "bad-shape",
                "valid": False,
                "issue_codes": ["result_shape_mismatch"],
                "result_shape_kind": "categorical_chart",
            },
        ],
    }
    expectations = {
        "cases": {
            "route": {
                "outcome": "route",
                "result_shape": "table",
                "selected_source": "typed_fallback",
            },
            "clarify": {
                "outcome": "clarify",
                "required_issue_codes": ["proposal_not_route"],
            },
            "invalid": {
                "outcome": "block",
                "required_issue_codes": ["proposal_validation_failed"],
            },
            "bad-shape": {
                "outcome": "block",
                "required_issue_codes": ["result_shape_mismatch"],
            },
        }
    }

    report = evaluate_resolution_safety_expectations(summary, expectations)
    rendered = render_resolution_safety_expectations_markdown(report)

    assert report["pass"] is True
    assert report["outcome_counts"] == {"block": 2, "clarify": 1, "route": 1}
    assert "bad-shape" in rendered


def test_resolution_safety_gate_fails_direct_llm_sql() -> None:
    summary = {
        "source": "llm_resolution_fallback_batch",
        "cases": [
            {
                "stem": "unsafe",
                "status": "selected",
                "selected_sql_present": True,
                "used_direct_llm_sql": True,
            }
        ],
    }
    expectations = {"cases": {"unsafe": {"outcome": "route"}}}

    report = evaluate_resolution_safety_expectations(summary, expectations)

    assert report["pass"] is False
    assert report["cases"][0]["failures"] == ["direct_llm_sql"]


def test_openai_request_batch_writes_strict_request_previews(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    (packet_dir / "mail001.packet.json").write_text(
        json.dumps(packet),
        encoding="utf-8",
    )

    summary = build_openai_resolution_request_batch(
        packet_dir,
        tmp_path / "requests",
        model="gpt-test",
    )
    rendered = render_openai_request_batch_markdown(summary)

    assert summary["packet_count"] == 1
    assert summary["provider_call_count"] == 0
    request_path = Path(summary["cases"][0]["request_path"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["model"] == "gpt-test"
    assert request["text"]["format"]["strict"] is True
    assert "`mail001`" in rendered


def test_rejected_query_packet_does_not_emit_misleading_status_stem(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)

    packet = build_rejected_query_packet(graph, "show status")

    field_hit = next(
        hit
        for hit in packet["local_candidates"]["field_hits"]
        if hit["field"] == "mails.status"
    )
    assert field_hit["matched_tokens"] == ["status"]


def test_value_dictionary_hits_require_whole_phrase(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES (?, 'scope_predicate', ?, ?, 2)",
        (
            "not sent",
            json.dumps(
                {
                    "scope": "mails.status.not_sent",
                    "field": "mails.status",
                    "operator": "=",
                    "rawValue": "draft",
                }
            ),
            0.88,
        ),
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(graph, "show sent mails")

    terms = {
        hit["term"]
        for hit in packet["local_candidates"]["value_dictionary_hits"]
        if hit["field"] == "mails.status"
    }
    assert terms == {"sent"}


def test_schema_card_can_include_non_redacted_samples(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)

    card = build_schema_card(graph, include_samples=True)

    mails = next(entity for entity in card["entities"] if entity["name"] == "mails")
    status = next(field for field in mails["fields"] if field["name"] == "status")
    assert status["samples"] == ["sent", "draft"]
    assert card["summary"]["sample_values_included"] is True


def test_schema_card_without_vocabulary_table_stays_compatible(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph, include_vocabulary=False)

    card = build_schema_card(graph)
    packet = build_rejected_query_packet(graph, "show sent mails")

    assert card["summary"]["value_dictionary_count"] == 0
    assert packet["local_candidates"]["value_dictionary_hits"] == []


def test_schema_card_exposes_field_scoped_value_dictionary(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)

    card = build_schema_card(graph)

    mails = next(entity for entity in card["entities"] if entity["name"] == "mails")
    status = next(field for field in mails["fields"] if field["name"] == "status")
    assert status["value_dictionary"] == [
        {
            "term": "draft",
            "operator": "=",
            "raw_value": "draft",
            "scope": "mails.status.draft",
            "confidence": 0.88,
            "source_layer": 2,
        },
        {
            "term": "sent",
            "operator": "=",
            "raw_value": "sent",
            "scope": "mails.status.sent",
            "confidence": 0.88,
            "source_layer": 2,
        },
    ]


def test_rejected_query_packet_candidates_use_full_graph_beyond_card_truncation(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, 'text', ?)",
        [(f"dummy_{idx:02d}", f"dummy_{idx:02d}", f"Dummy {idx}") for idx in range(30)],
    )
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'zz_priority', 'zz_priority', 'text', 'Priority')"
    )
    conn.execute(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES ('urgent', 'scope_predicate', ?, 0.88, 2)",
        (
            json.dumps(
                {
                    "scope": "mails.zz_priority.urgent",
                    "field": "mails.zz_priority",
                    "operator": "=",
                    "rawValue": "urgent",
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES ('mails.zz_priority', '[\"urgent\"]', 0)"
    )
    conn.commit()
    conn.close()

    card = build_schema_card(graph)
    packet = build_rejected_query_packet(
        graph,
        "show urgent mails by priority",
        include_samples=True,
    )

    mails = next(entity for entity in card["entities"] if entity["name"] == "mails")
    assert mails["truncated_fields"] > 0
    assert "zz_priority" not in {field["name"] for field in mails["fields"]}
    assert {
        "field": "mails.zz_priority",
        "role": "attribute",
        "matched_tokens": ["priority"],
    } in packet["local_candidates"]["field_hits"]
    assert any(
        hit["field"] == "mails.zz_priority" and hit["term"] == "urgent"
        for hit in packet["local_candidates"]["value_dictionary_hits"]
    )
    packet_mails = next(
        entity for entity in packet["schema_card"]["entities"] if entity["name"] == "mails"
    )
    packet_priority = next(
        field for field in packet_mails["fields"] if field["name"] == "zz_priority"
    )
    assert packet_priority["samples"] == ["urgent"]
    assert packet_priority["value_dictionary"] == [
        {
            "term": "urgent",
            "operator": "=",
            "raw_value": "urgent",
            "scope": "mails.zz_priority.urgent",
            "confidence": 0.88,
            "source_layer": 2,
        }
    ]
    assert packet["schema_card"]["summary"]["seed_field_enrichment_count"] == 1


def test_rejected_query_packet_exposes_sample_value_hits_beyond_card_truncation(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, 'text', ?)",
        [(f"dummy_{idx:02d}", f"dummy_{idx:02d}", f"Dummy {idx}") for idx in range(30)],
    )
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'zz_priority', 'zz_priority', 'text', 'Priority')"
    )
    conn.execute(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES ('mails.zz_priority', '[\"urgent\"]', 0)"
    )
    conn.commit()
    conn.close()

    card = build_schema_card(graph)
    packet = build_rejected_query_packet(
        graph,
        "show urgent mails by priority",
        include_samples=True,
    )

    mails = next(entity for entity in card["entities"] if entity["name"] == "mails")
    assert mails["truncated_fields"] > 0
    assert "zz_priority" not in {field["name"] for field in mails["fields"]}
    assert {
        "field": "mails.zz_priority",
        "operator": "=",
        "raw_value": "urgent",
        "value_kind": "sample_value",
        "match_type": "exact",
        "matched_tokens": ["urgent"],
    } in packet["local_candidates"]["sample_value_hits"]
    packet_mails = next(
        entity for entity in packet["schema_card"]["entities"] if entity["name"] == "mails"
    )
    packet_priority = next(
        field for field in packet_mails["fields"] if field["name"] == "zz_priority"
    )
    assert packet_priority["samples"] == ["urgent"]

    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "mails.zz_priority",
            "aggregate": "",
            "rationale": "priority is present in the packet",
        }
    ]
    proposal["filters"] = [
        {
            "field": "mails.zz_priority",
            "operator": "=",
            "value": "urgent",
            "value_kind": "sample_value",
            "rationale": "urgent was present in field-scoped samples",
        }
    ]
    proposal["joins"] = []
    proposal["evidence"] = [
        {"claim": "urgent is backed by a sample", "graph_refs": ["mails.zz_priority"]}
    ]

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert validation["valid"] is True
    assert result["valid"] is True
    assert '"mails"."zz_priority" = ' in str(result["sql"])


def test_sample_value_hits_are_omitted_without_samples_or_for_pii_like_values(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'recipient_email', 'recipient_email', 'text', 'Recipient Email')"
    )
    conn.execute(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES ('mails.recipient_email', '[\"alex@example.com\"]', 0)"
    )
    conn.commit()
    conn.close()

    packet_without_samples = build_rejected_query_packet(
        graph,
        "show urgent mails",
        include_samples=False,
    )
    packet_with_pii_like_sample = build_rejected_query_packet(
        graph,
        "show mails for alex@example.com",
        include_samples=True,
    )

    assert packet_without_samples["local_candidates"]["sample_value_hits"] == []
    assert not any(
        hit["field"] == "mails.recipient_email"
        for hit in packet_with_pii_like_sample["local_candidates"]["sample_value_hits"]
    )


def test_sample_value_component_aliases_validate_only_when_unique(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'rule_basis', 'rule_basis', 'text', 'Rule Basis')"
    )
    conn.execute(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES ('mails.rule_basis', '[\"speed_check\"]', 0)"
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(
        graph,
        "show mails based on speed",
        include_samples=True,
    )

    assert {
        "field": "mails.rule_basis",
        "operator": "=",
        "raw_value": "speed_check",
        "value_kind": "sample_value",
        "match_type": "unique_component",
        "matched_tokens": ["speed"],
    } in packet["local_candidates"]["sample_value_hits"]

    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "mails.rule_basis",
            "aggregate": "",
            "rationale": "rule basis is present in the packet",
        }
    ]
    proposal["filters"] = [
        {
            "field": "mails.rule_basis",
            "operator": "=",
            "value": "speed_check",
            "value_kind": "sample_value",
            "rationale": "speed uniquely matched the speed_check sample",
        }
    ]
    proposal["joins"] = []
    proposal["evidence"] = [
        {"claim": "speed_check sample was uniquely matched", "graph_refs": ["mails.rule_basis"]}
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is True


def test_ambiguous_sample_value_component_alias_requires_clarification(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, 'text', ?)",
        [
            ("rule_basis", "rule_basis", "Rule Basis"),
            ("risk_basis", "risk_basis", "Risk Basis"),
        ],
    )
    conn.executemany(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES (?, ?, 0)",
        [
            ("mails.rule_basis", '["speed_check"]'),
            ("mails.risk_basis", '["speed_risk"]'),
        ],
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(
        graph,
        "show mails based on speed",
        include_samples=True,
    )
    sample_hits = packet["local_candidates"]["sample_value_hits"]

    assert {
        hit["field"]: (hit["match_type"], hit.get("requires_clarification"))
        for hit in sample_hits
        if hit["raw_value"] in {"speed_check", "speed_risk"}
    } == {
        "mails.rule_basis": ("ambiguous_component", True),
        "mails.risk_basis": ("ambiguous_component", True),
    }

    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "mails.rule_basis",
            "aggregate": "",
            "rationale": "rule basis is present in the packet",
        }
    ]
    proposal["filters"] = [
        {
            "field": "mails.rule_basis",
            "operator": "=",
            "value": "speed_check",
            "value_kind": "sample_value",
            "rationale": "speed was ambiguous",
        }
    ]
    proposal["joins"] = []

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert any(issue["code"] == "unbacked_value_filter" for issue in validation["issues"])


def test_candidate_tokens_match_simple_verb_forms(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'score', 'score', 'integer', 'Score')"
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(graph, "show mails that scored 20")

    assert {
        "field": "mails.score",
        "role": "numeric",
        "matched_tokens": ["score"],
    } in packet["local_candidates"]["field_hits"]


def test_source_vocabulary_field_alias_seeds_capped_field(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, 'text', ?)",
        [(f"dummy_{idx:02d}", f"dummy_{idx:02d}", f"Dummy {idx}") for idx in range(30)],
    )
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'zz_final_approval', 'zz_final_approval', 'text', '')"
    )
    conn.execute(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES ('final approval', 'field', 'mails.zz_final_approval', 0.93, 6)"
    )
    conn.commit()
    conn.close()

    card = build_schema_card(graph)
    packet = build_rejected_query_packet(graph, "show final approval for mails")

    mails = next(entity for entity in card["entities"] if entity["name"] == "mails")
    assert "zz_final_approval" not in {field["name"] for field in mails["fields"]}
    assert {
        "term": "final approval",
        "canonical_kind": "field",
        "canonical_value": "mails.zz_final_approval",
        "matched_tokens": ["approval", "final"],
        "confidence": 0.93,
        "source_layer": 6,
    } in packet["local_candidates"]["source_vocabulary_hits"]
    packet_mails = next(
        entity for entity in packet["schema_card"]["entities"] if entity["name"] == "mails"
    )
    assert "zz_final_approval" in {field["name"] for field in packet_mails["fields"]}

    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "mails.zz_final_approval",
            "aggregate": "",
            "rationale": "final approval is source vocabulary for this field",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["evidence"] = [
        {
            "claim": "final approval maps to zz_final_approval",
            "graph_refs": ["mails.zz_final_approval"],
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is True


def test_enum_value_source_vocabulary_backs_field_scoped_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'rule_basis', 'rule_basis', 'text', 'Rule Basis')"
    )
    conn.execute(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES ('speed', 'enum_value', 'mails.rule_basis:speed_check', 0.94, 3)"
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(graph, "show mails based on speed")

    assert {
        "term": "speed",
        "field": "mails.rule_basis",
        "operator": "=",
        "raw_value": "speed_check",
        "value_kind": "enum_value",
        "matched_tokens": ["speed"],
        "confidence": 0.94,
        "source_layer": 3,
    } in packet["local_candidates"]["enum_value_hits"]

    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.rule_basis",
            "operator": "=",
            "value": "speed_check",
            "value_kind": "enum_value",
            "rationale": "speed is source vocabulary for this enum value",
        }
    ]
    proposal["joins"] = []
    proposal["evidence"] = [
        {"claim": "speed maps to rule_basis", "graph_refs": ["mails.rule_basis"]}
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is True


def test_ambiguous_enum_value_source_vocabulary_fails_closed(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, 'text', ?)",
        [
            ("rule_basis", "rule_basis", "Rule Basis"),
            ("risk_basis", "risk_basis", "Risk Basis"),
        ],
    )
    conn.executemany(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES ('speed', 'enum_value', ?, 0.94, 3)",
        [("mails.rule_basis:speed_check",), ("mails.risk_basis:speed_risk",)],
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(graph, "show mails based on speed")
    enum_hits = packet["local_candidates"]["enum_value_hits"]

    assert {
        hit["field"]: hit.get("requires_clarification")
        for hit in enum_hits
        if hit["term"] == "speed"
    } == {
        "mails.rule_basis": True,
        "mails.risk_basis": True,
    }

    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.rule_basis",
            "operator": "=",
            "value": "speed_check",
            "value_kind": "enum_value",
            "rationale": "speed was ambiguous",
        }
    ]
    proposal["joins"] = []

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert any(issue["code"] == "unbacked_value_filter" for issue in validation["issues"])


def test_rejected_query_packet_enriches_question_entities_beyond_card_truncation(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label) "
        "VALUES (?, ?, ?, ?)",
        [
            (
                f"aaa_dummy_{idx:02d}",
                f"aaa_dummy_{idx:02d}",
                f"dummy {idx}",
                f"dummies {idx}",
            )
            for idx in range(90)
        ],
    )
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES (?, 'id', 'id', 'integer', 'ID')",
        [(f"aaa_dummy_{idx:02d}",) for idx in range(90)],
    )
    conn.execute(
        "INSERT INTO entities(canonical_name, db_table, singular_label, plural_label) "
        "VALUES ('zz_priority_reports', 'zz_priority_reports', "
        "'priority report', 'priority reports')"
    )
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('zz_priority_reports', ?, ?, ?, ?)",
        [
            ("id", "id", "integer", "ID"),
            ("organization_id", "organization_id", "integer", "Organization ID"),
            ("amount_paid", "amount_paid", "decimal", "Amount Paid"),
            ("status", "status", "text", "Status"),
        ],
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('zz_priority_reports', 'organization_id', "
        "'organizations', 'id', 'many_to_one')"
    )
    conn.commit()
    conn.close()

    card = build_schema_card(graph)
    assert "zz_priority_reports" not in {
        entity["name"] for entity in card["entities"]
    }

    packet = build_rejected_query_packet(
        graph,
        "which status has the highest average amount paid for priority reports by organization",
    )

    packet_entities = {
        entity["name"]: entity for entity in packet["schema_card"]["entities"]
    }
    assert "zz_priority_reports" in packet_entities
    assert "organizations" in packet_entities
    report_fields = {
        field["name"] for field in packet_entities["zz_priority_reports"]["fields"]
    }
    assert {"amount_paid", "organization_id", "status"} <= report_fields
    assert {
        "from": "zz_priority_reports.organization_id",
        "to": "organizations.id",
        "kind": "many_to_one",
    } in packet["schema_card"]["relationships"]
    assert packet["schema_card"]["summary"]["seed_entity_enrichment_count"] >= 1
    assert packet["schema_card"]["summary"]["seed_relationship_enrichment_count"] >= 1

    compact = compact_resolution_packet_for_provider(
        packet,
        max_entities=4,
        max_fields_per_entity=4,
        max_relationships=4,
    )
    compact_entities = {
        entity["name"]: entity for entity in compact["schema_card"]["entities"]
    }
    assert "zz_priority_reports" in compact_entities
    compact_report_fields = {
        field["name"] for field in compact_entities["zz_priority_reports"]["fields"]
    }
    assert {"amount_paid", "organization_id", "status"} <= compact_report_fields
    assert {
        "from": "zz_priority_reports.organization_id",
        "to": "organizations.id",
        "kind": "many_to_one",
    } in compact["schema_card"]["relationships"]


def _valid_sent_mail_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.91,
        "intent": "list sent mail subjects",
        "target_entities": ["mails"],
        "projections": [
            {
                "kind": "field",
                "field": "mails.subject",
                "aggregate": "",
                "rationale": "subject is the display text for mail",
            }
        ],
        "filters": [
            {
                "field": "mails.status",
                "operator": "=",
                "value": "sent",
                "value_kind": "value_dictionary",
                "rationale": "the packet maps sent to mails.status",
            }
        ],
        "joins": [
            {
                "from_entity": "mails",
                "from_field": "organization_id",
                "to_entity": "organizations",
                "to_field": "id",
                "rationale": "relationship exists in the packet",
            }
        ],
        "group_by": [],
        "order_by": [],
        "limit": 100,
        "ambiguity_questions": [],
        "evidence": [
            {"claim": "sent maps to status", "graph_refs": ["mails.status"]}
        ],
        "safety_notes": [],
    }


def _business_schema_packet(question: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "test_packet",
        "question": question,
        "schema_card": {
            "schema_version": 1,
            "source": "test_schema_card",
            "graph": "",
            "entities": [
                {
                    "name": "organizations",
                    "db_table": "organizations",
                    "fields": [
                        {
                            "name": "id",
                            "db_column": "id",
                            "type": "integer",
                            "display_label": "ID",
                            "role": "id",
                            "value_dictionary": [],
                        },
                        {
                            "name": "organization_name",
                            "db_column": "organization_name",
                            "type": "text",
                            "display_label": "Account",
                            "role": "display",
                            "value_dictionary": [],
                        },
                        {
                            "name": "account_state",
                            "db_column": "account_state",
                            "type": "text",
                            "display_label": "Status",
                            "role": "status",
                            "value_dictionary": [
                                {
                                    "term": "churned",
                                    "operator": "=",
                                    "raw_value": "churned",
                                },
                                {
                                    "term": "active",
                                    "operator": "=",
                                    "raw_value": "active",
                                },
                            ],
                        },
                        {
                            "name": "market_segment",
                            "db_column": "market_segment",
                            "type": "text",
                            "display_label": "Segment",
                            "role": "attribute",
                            "value_dictionary": [],
                        },
                        {
                            "name": "annual_recurring_revenue",
                            "db_column": "annual_recurring_revenue",
                            "type": "float",
                            "display_label": "ARR",
                            "role": "numeric",
                            "value_dictionary": [],
                        },
                        {
                            "name": "created_date",
                            "db_column": "created_date",
                            "type": "date",
                            "display_label": "Created On",
                            "role": "date",
                            "value_dictionary": [],
                        },
                    ],
                },
                {
                    "name": "recurring_contracts",
                    "db_table": "recurring_contracts",
                    "fields": [
                        {
                            "name": "organization_id",
                            "db_column": "organization_id",
                            "type": "integer",
                            "display_label": "Account ID",
                            "role": "id",
                            "value_dictionary": [],
                        },
                        {
                            "name": "subscription_state",
                            "db_column": "subscription_state",
                            "type": "text",
                            "display_label": "Status",
                            "role": "status",
                            "value_dictionary": [
                                {
                                    "term": "cancelled",
                                    "operator": "=",
                                    "raw_value": "cancelled",
                                },
                                {
                                    "term": "active",
                                    "operator": "=",
                                    "raw_value": "active",
                                },
                            ],
                        },
                        {
                            "name": "ended_date",
                            "db_column": "ended_date",
                            "type": "date",
                            "display_label": "Ended On",
                            "role": "date",
                            "value_dictionary": [],
                        },
                    ],
                },
                {
                    "name": "deals",
                    "db_table": "deals",
                    "fields": [
                        {
                            "name": "deal_amount",
                            "db_column": "deal_amount",
                            "type": "float",
                            "display_label": "Amount",
                            "role": "numeric",
                            "value_dictionary": [],
                        },
                        {
                            "name": "deal_stage",
                            "db_column": "deal_stage",
                            "type": "text",
                            "display_label": "Stage",
                            "role": "attribute",
                            "value_dictionary": [
                                {
                                    "term": "closed lost",
                                    "operator": "=",
                                    "raw_value": "closed_lost",
                                },
                                {
                                    "term": "closed won",
                                    "operator": "=",
                                    "raw_value": "closed_won",
                                },
                                {
                                    "term": "proposal",
                                    "operator": "=",
                                    "raw_value": "proposal",
                                },
                                {
                                    "term": "negotiation",
                                    "operator": "=",
                                    "raw_value": "negotiation",
                                },
                            ],
                        },
                        {
                            "name": "expected_close_date",
                            "db_column": "expected_close_date",
                            "type": "date",
                            "display_label": "Close Date",
                            "role": "date",
                            "value_dictionary": [],
                        },
                        {
                            "name": "owner_member_id",
                            "db_column": "owner_member_id",
                            "type": "integer",
                            "display_label": "Owner",
                            "role": "id",
                            "value_dictionary": [],
                        },
                    ],
                },
                {
                    "name": "team_members",
                    "db_table": "team_members",
                    "fields": [
                        {
                            "name": "id",
                            "db_column": "id",
                            "type": "integer",
                            "display_label": "ID",
                            "role": "id",
                            "value_dictionary": [],
                        },
                        {
                            "name": "person_name",
                            "db_column": "person_name",
                            "type": "text",
                            "display_label": "Rep",
                            "role": "display",
                            "value_dictionary": [],
                        },
                    ],
                },
            ],
            "relationships": [
                {
                    "from": "recurring_contracts.organization_id",
                    "to": "organizations.id",
                    "kind": "many_to_one",
                },
                {
                    "from": "deals.owner_member_id",
                    "to": "team_members.id",
                    "kind": "many_to_one",
                },
            ],
        },
        "local_candidates": {},
    }


def test_resolution_proposal_validator_accepts_packet_backed_plan(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")

    validation = validate_resolution_proposal(packet, _valid_sent_mail_proposal())

    assert validation["valid"] is True
    assert validation["issue_count"] == 0


def test_resolution_proposal_validator_rejects_sensitive_schema_route(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "sensitive.semsql"
    _make_sensitive_graph(graph)
    packet = build_rejected_query_packet(graph, "show api token secrets")
    proposal = {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.9,
        "intent": "list api token secrets",
        "distinct": False,
        "target_entities": ["api_tokens"],
        "projections": [
            {
                "kind": "field",
                "field": "api_tokens.secret_token",
                "aggregate": "",
                "alias": "",
                "numerator_field": "",
                "numerator_operator": "",
                "numerator_value": None,
                "numerator_value_kind": "",
                "denominator_field": "",
                "scale": None,
                "rationale": "token secret is the requested field",
            }
        ],
        "filters": [],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 10,
        "ambiguity_questions": [],
        "evidence": [{"claim": "field exists", "graph_refs": ["api_tokens.secret_token"]}],
        "safety_notes": [],
    }

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert {issue["code"] for issue in validation["issues"]} >= {
        "sensitive_entity_forbidden",
        "sensitive_field_forbidden",
    }
    assert result["valid"] is False
    assert result["sql"] is None


def test_resolution_proposal_accepts_redundantly_qualified_join_fields(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["joins"] = [
        {
            "from_entity": "mails",
            "from_field": "mails.organization_id",
            "to_entity": "organizations",
            "to_field": "organizations.id",
            "rationale": "model redundantly qualified fields",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert validation["valid"] is True
    assert result["valid"] is True
    assert '"mails"."organization_id" = "organizations"."id"' in result["sql"]
    assert "mails.mails.organization_id" not in result["sql"]


def test_resolution_proposal_validator_rejects_unknown_field(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "payments.status",
            "operator": "=",
            "value": "sent",
            "value_kind": "value_dictionary",
            "rationale": "not in graph",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert {issue["code"] for issue in validation["issues"]} >= {
        "unknown_field",
        "unbacked_value_filter",
    }


def test_resolution_proposal_validator_rejects_unbacked_value_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.status",
            "operator": "=",
            "value": "archived",
            "value_kind": "value_dictionary",
            "rationale": "not in packet",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert validation["issues"][0]["code"] == "unbacked_value_filter"


def test_resolution_proposal_validator_rejects_unbacked_text_literal_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show archived mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.status",
            "operator": "=",
            "value": "archived",
            "value_kind": "literal",
            "rationale": "provider called an unbacked category a literal",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert validation["issues"][0]["code"] == "unbacked_value_filter"


def test_resolution_proposal_validator_accepts_backed_text_literal_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.status",
            "operator": "=",
            "value": "sent",
            "value_kind": "literal",
            "rationale": "provider mislabeled a backed value-dictionary hit",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is True


def test_resolution_proposal_validator_accepts_sample_backed_text_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "show sent mail subjects",
        include_samples=True,
    )
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.status",
            "operator": "=",
            "value": "sent",
            "value_kind": "sample",
            "rationale": "status value was present in field samples",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert validation["valid"] is True
    assert result["valid"] is True
    assert '"mails"."status" = ' in str(result["sql"])


def test_render_resolution_proposal_applies_default_row_list_limit(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert str(result["sql"]).endswith("LIMIT 100")
    assert "default_row_limit_applied" in {
        issue["code"] for issue in result["issues"]
    }


def test_resolution_proposal_canonicalizes_unambiguous_bare_rate_fields(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "what percentage of mails are sent",
        include_samples=True,
    )
    proposal = {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.9,
        "intent": "percentage of sent mails",
        "target_entities": ["mails"],
        "projections": [
            {
                "kind": "conditional_rate",
                "field": "",
                "aggregate": "",
                "alias": "pct_sent_mails",
                "numerator_field": "status",
                "numerator_operator": "=",
                "numerator_value": "sent",
                "numerator_value_kind": "sample",
                "denominator_field": "id",
                "scale": 100,
                "rationale": "sent is a status sample on mails",
            }
        ],
        "filters": [],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": None,
        "ambiguity_questions": [],
        "evidence": [{"claim": "sent is backed", "graph_refs": ["mails.status"]}],
        "safety_notes": [],
    }

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert validation["valid"] is True
    assert result["valid"] is True
    assert '"mails"."status" = ' in str(result["sql"])
    assert 'COUNT("mails"."id")' in str(result["sql"])


def test_resolution_proposal_validator_rejects_type_incompatible_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show mails with id above abc")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.id",
            "operator": ">",
            "value": "abc",
            "value_kind": "literal",
            "rationale": "invalid numeric comparison",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert validation["issues"][0]["code"] == "invalid_filter_value"


def test_resolution_proposal_validator_rejects_like_on_numeric_field(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show mails with id like 1")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.id",
            "operator": "LIKE",
            "value": "1",
            "value_kind": "literal",
            "rationale": "LIKE should not apply to numeric fields",
        }
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert validation["issues"][0]["code"] == "operator_type_mismatch"


def test_resolution_proposal_validator_accepts_group_by_field(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "count sent mails by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "rationale": "count mails",
        }
    ]
    proposal["group_by"] = ["mails.organization_id"]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is True


def test_resolution_proposal_validator_rejects_sql_fragments(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")
    proposal = _valid_sent_mail_proposal()
    proposal["intent"] = "SELECT * FROM mails"

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert validation["issues"][0]["code"] == "sql_fragment_forbidden"


def test_render_resolution_proposal_outputs_valid_local_sql(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mail subjects")

    result = render_resolution_proposal(
        packet,
        _valid_sent_mail_proposal(),
        dialect="sqlite",
    )

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."subject" FROM "mails" '
        'JOIN "organizations" ON "mails"."organization_id" = "organizations"."id" '
        'WHERE "mails"."status" = \'sent\' LIMIT 100'
    )
    assert result["query_frame_candidate"]["route_reason"] == "llm_resolution_validated"


def test_render_resolution_proposal_promotes_hidden_status_clarification(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "average mail id")
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["ambiguity_questions"] = [
        "Should the average include only sent mails or all mails with ids populated?"
    ]
    proposal["projections"] = [
        {
            "kind": "aggregate",
            "field": "mails.id",
            "aggregate": "AVG",
            "rationale": "average numeric mail id",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert result["issues"][0]["code"] == "clarify_auto_promoted_hidden_filter"
    assert result["sql"] == 'SELECT AVG("mails"."id") AS "avg" FROM "mails"'
    assert (
        result["query_frame_candidate"]["route_reason"]
        == "llm_resolution_validated_clarify_auto_promoted"
    )


def test_render_resolution_proposal_keeps_explicit_status_clarification_closed(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "average sent mail id")
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["ambiguity_questions"] = [
        "Should the average include only sent mails or all mails with ids populated?"
    ]
    proposal["projections"] = [
        {
            "kind": "aggregate",
            "field": "mails.id",
            "aggregate": "AVG",
            "rationale": "average numeric mail id",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    issue_codes = [issue["code"] for issue in result["issues"]]
    assert "clarify_auto_promoted_metric_formula" not in issue_codes
    assert "clarify_auto_promoted_metric_catalog" not in issue_codes
    assert not result.get("sql")


def test_render_resolution_proposal_promotes_alternative_filter_clarification(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, ?, ?)",
        [
            ("score", "score", "integer", "Score"),
            ("priority", "priority", "text", "Priority"),
        ],
    )
    conn.execute(
        "INSERT INTO sample_values(field_canonical, examples, pii_redacted) "
        "VALUES ('mails.priority', '[\"high\", \"medium\"]', 0)"
    )
    conn.commit()
    conn.close()
    packet = build_rejected_query_packet(
        graph,
        "which status has the highest average score for mails where priority is medium",
        include_samples=True,
    )
    proposal = {
        "schema_version": 1,
        "action": "clarify",
        "confidence": 0.86,
        "intent": "rank statuses by average score for medium priority mails",
        "target_entities": ["mails"],
        "projections": [
            {
                "kind": "field",
                "field": "mails.status",
                "aggregate": "",
                "alias": "status",
                "rationale": "requested grouping dimension",
            },
            {
                "kind": "aggregate",
                "field": "mails.score",
                "aggregate": "AVG",
                "alias": "avg_score",
                "rationale": "requested metric",
            },
        ],
        "filters": [
            {
                "field": "mails.priority",
                "operator": "=",
                "value": "medium",
                "value_kind": "sample_value",
                "rationale": "explicit priority filter from the question",
            }
        ],
        "joins": [],
        "group_by": ["mails.status"],
        "order_by": [
            {
                "field": "mails.score",
                "aggregate": "AVG",
                "alias": "avg_score",
                "direction": "DESC",
                "rationale": "highest average score",
            }
        ],
        "limit": 1,
        "ambiguity_questions": [
            "Do you mean mails.status = 'draft', or did you intend the priority filter only?"
        ],
        "evidence": [],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert {
        issue["code"] for issue in result["issues"]
    } == {"clarify_auto_promoted_unrequested_extra_filter"}
    assert '"mails"."priority" = \'medium\'' in result["sql"]
    assert '"mails"."status" = \'draft\'' not in result["sql"]


def _lead_count_clarify_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "clarify",
        "confidence": 0.74,
        "intent": "count new paid-search leads in a named month",
        "target_entities": ["leads"],
        "projections": [
            {
                "kind": "count",
                "field": "",
                "aggregate": "COUNT",
                "alias": "new_paid_search_leads_count",
                "rationale": "count matching leads",
            }
        ],
        "filters": [
            {
                "field": "leads.status",
                "operator": "=",
                "value": "new",
                "value_kind": "value_dictionary",
                "rationale": "new maps to lead status",
            },
            {
                "field": "leads.acquisition_channel",
                "operator": "=",
                "value": "paid_search",
                "value_kind": "value_dictionary",
                "rationale": "paid search maps to acquisition channel",
            },
        ],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 0,
        "ambiguity_questions": [
            "For 'in February 2024', should we filter by the lead capture date "
            "being in February 2024, or by the associated campaign launch date?"
        ],
        "evidence": [],
        "safety_notes": [],
    }


def test_render_resolution_proposal_promotes_single_subject_date_anchor(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "How many new leads came from paid search in February 2024?",
    )

    result = render_resolution_proposal(
        packet,
        _lead_count_clarify_proposal(),
        dialect="sqlite",
    )

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert result["issues"][0]["code"] == "clarify_auto_promoted_subject_date_anchor"
    assert result["sql"] == (
        'SELECT COUNT(*) AS "new_paid_search_leads_count" FROM "leads" '
        "WHERE \"leads\".\"status\" = 'new' "
        "AND \"leads\".\"acquisition_channel\" = 'paid_search' "
        "AND \"leads\".\"captured_on\" BETWEEN '2024-02-01' AND '2024-03-01'"
    )
    assert result["query_frame_candidate"]["predicates"][-1] == {
        "field": "leads.captured_on",
        "value": "['2024-02-01', '2024-03-01']",
        "operator": "BETWEEN",
    }


def test_rejected_query_packet_seeds_sole_date_window_anchor_on_wide_entity(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('leads', ?, ?, 'text', ?)",
        [
            (f"aaa_extra_{index:02}", f"aaa_extra_{index:02}", f"Extra {index:02}")
            for index in range(40)
        ],
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(
        graph,
        "How many new leads came from paid search in February 2024?",
    )

    date_window = packet["local_candidates"]["date_window"]
    assert date_window == {
        "mention": "2024-02",
        "start": "2024-02-01",
        "end": "2024-03-01",
        "anchor_candidates": [
            {
                "entity": "leads",
                "field": "leads.captured_on",
                "reason": "sole_date_field_on_mentioned_entity",
            }
        ],
        "ambiguous": False,
    }
    leads = next(entity for entity in packet["schema_card"]["entities"] if entity["name"] == "leads")
    assert any(field["name"] == "captured_on" for field in leads["fields"])


def test_render_resolution_proposal_keeps_multi_date_anchor_closed(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph, extra_lead_date=True)
    packet = build_rejected_query_packet(
        graph,
        "How many new leads came from paid search in February 2024?",
    )

    result = render_resolution_proposal(
        packet,
        _lead_count_clarify_proposal(),
        dialect="sqlite",
    )

    assert result["valid"] is False
    assert result["issues"][0]["code"] == "proposal_not_route"


def test_rejected_query_packet_marks_multi_date_window_anchor_ambiguous(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph, extra_lead_date=True)

    packet = build_rejected_query_packet(
        graph,
        "How many new leads came from paid search in February 2024?",
    )

    date_window = packet["local_candidates"]["date_window"]
    assert date_window["mention"] == "2024-02"
    assert date_window["ambiguous"] is True
    assert date_window["anchor_candidates"] == [
        {
            "entity": "leads",
            "field": "leads.captured_on",
            "reason": "one_of_multiple_date_fields_on_mentioned_entity",
        },
        {
            "entity": "leads",
            "field": "leads.qualified_date",
            "reason": "one_of_multiple_date_fields_on_mentioned_entity",
        },
    ]


def test_rejected_query_packet_prefers_date_role_anchor_when_unambiguous(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph, extra_lead_date=True)

    packet = build_rejected_query_packet(
        graph,
        "How many leads were qualified in February 2024?",
    )

    date_window = packet["local_candidates"]["date_window"]
    assert date_window["mention"] == "2024-02"
    assert date_window["ambiguous"] is False
    assert date_window["preferred_anchor"] == {
        "entity": "leads",
        "field": "leads.qualified_date",
        "reason": "date_role_match_on_mentioned_entity",
        "matched_tokens": ["qualified"],
        "score": 10,
    }
    assert date_window["anchor_candidates"] == [
        {
            "entity": "leads",
            "field": "leads.captured_on",
            "reason": "one_of_multiple_date_fields_on_mentioned_entity",
        },
        {
            "entity": "leads",
            "field": "leads.qualified_date",
            "reason": "date_role_match_on_mentioned_entity",
            "matched_tokens": ["qualified"],
            "score": 10,
        },
    ]


def test_render_resolution_proposal_promotes_date_role_anchor(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph, extra_lead_date=True)
    packet = build_rejected_query_packet(
        graph,
        "How many leads were qualified from paid search in February 2024?",
    )
    proposal = _lead_count_clarify_proposal()
    proposal["filters"] = [
        {
            "field": "leads.acquisition_channel",
            "operator": "=",
            "value": "paid_search",
            "value_kind": "value_dictionary",
            "rationale": "paid search maps to acquisition channel",
        }
    ]
    proposal["ambiguity_questions"] = [
        "For in February 2024, should the date filter apply to created date or qualified date?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["issues"][0]["code"] == "clarify_auto_promoted_subject_date_anchor"
    assert (
        '"leads"."qualified_date" BETWEEN \'2024-02-01\' AND \'2024-03-01\''
        in result["sql"]
    )
    assert '"leads"."captured_on" BETWEEN' not in result["sql"]


def _lead_conversion_rate_clarify_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "clarify",
        "confidence": 0.62,
        "intent": "lead-to-customer conversion rate by campaign",
        "target_entities": ["leads", "campaigns", "organizations"],
        "projections": [
            {
                "kind": "field",
                "field": "campaigns.program_name",
                "aggregate": "",
                "alias": "campaign",
                "rationale": "campaign display field",
            },
            {
                "kind": "conditional_rate",
                "field": "",
                "aggregate": "",
                "alias": "lead_to_customer_conversion_rate",
                "numerator_field": "organizations.lifecycle_state",
                "numerator_operator": "=",
                "numerator_value": "customer",
                "numerator_value_kind": "value_dictionary",
                "denominator_field": "leads.id",
                "scale": None,
                "rationale": "provider selected customer lifecycle numerator",
            },
        ],
        "filters": [],
        "joins": [
            {
                "from_entity": "leads",
                "from_field": "program_id",
                "to_entity": "campaigns",
                "to_field": "id",
                "rationale": "campaign dimension",
            },
            {
                "from_entity": "leads",
                "from_field": "converted_organization_id",
                "to_entity": "organizations",
                "to_field": "id",
                "rationale": "ambiguous customer lifecycle option",
            },
        ],
        "group_by": ["campaigns.program_name"],
        "order_by": [],
        "limit": 0,
        "ambiguity_questions": [
            "Should the numerator count leads with leads.status = converted, "
            "or leads whose converted organization has lifecycle_state = customer?"
        ],
        "evidence": [],
        "safety_notes": [],
    }


def test_render_resolution_proposal_promotes_lead_conversion_metric_catalog(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_leads_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "Lead-to-customer conversion rate by campaign",
    )
    assert packet["schema_card"]["summary"]["metric_definition_count"] == 1
    assert packet["local_candidates"]["metric_catalog_ambiguous"] is False
    assert packet["local_candidates"]["metric_catalog_hits"] == [
        {
            "metric_kind": "conditional_rate",
            "name": "lead_to_customer_conversion_rate",
            "display_label": "Lead to customer conversion rate",
            "alias": "lead_to_customer_conversion_rate",
            "subject_entity": "leads",
            "numerator_field": "leads.status",
            "numerator_operator": "=",
            "numerator_value": "converted",
            "numerator_value_kind": "value_dictionary",
            "denominator_field": "leads.id",
            "scale": 100.0,
            "required_entities": ["leads"],
            "matched_tokens": ["conversion", "customer", "lead", "rate", "to"],
            "source": "metric_definition",
        }
    ]

    result = render_resolution_proposal(
        packet,
        _lead_conversion_rate_clarify_proposal(),
        dialect="sqlite",
    )

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert result["issues"][0]["code"] == "clarify_auto_promoted_metric_catalog"
    assert "organizations" not in result["sql"]
    assert result["sql"] == (
        'SELECT "campaigns"."program_name", '
        'CAST(SUM(CASE WHEN "leads"."status" = \'converted\' THEN 1 ELSE 0 END) '
        'AS REAL) * 100.0 / NULLIF(COUNT("leads"."id"), 0) '
        'AS "lead_to_customer_conversion_rate" '
        'FROM "leads" JOIN "campaigns" ON "leads"."program_id" = "campaigns"."id" '
        'GROUP BY "campaigns"."program_name" '
        'ORDER BY "lead_to_customer_conversion_rate" DESC'
    )


def test_render_resolution_proposal_promotes_aggregate_metric_catalog(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'delivery_ms', 'delivery_ms', 'real', 'Delivery Time')"
    )
    conn.execute(
        """
        CREATE TABLE metric_definitions (
            name TEXT PRIMARY KEY,
            display_label TEXT,
            metric_kind TEXT NOT NULL,
            subject_entity TEXT NOT NULL,
            numerator_field TEXT NOT NULL,
            numerator_operator TEXT NOT NULL,
            numerator_value TEXT NOT NULL,
            numerator_value_kind TEXT NOT NULL,
            denominator_field TEXT NOT NULL,
            scale REAL NOT NULL,
            required_entities_json TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            measure_field TEXT,
            aggregate TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO metric_definitions("
        "name, display_label, metric_kind, subject_entity, numerator_field, "
        "numerator_operator, numerator_value, numerator_value_kind, "
        "denominator_field, scale, required_entities_json, aliases_json, "
        "measure_field, aggregate"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "average_delivery_time",
            "Average delivery time",
            "aggregate",
            "mails",
            "mails.delivery_ms",
            "=",
            "",
            "metric_definition",
            "mails.id",
            1.0,
            json.dumps(["mails"]),
            json.dumps(["average delivery time"]),
            "mails.delivery_ms",
            "AVG",
        ),
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(
        graph,
        "average delivery time by organization",
    )
    assert packet["local_candidates"]["metric_catalog_hits"][0]["metric_kind"] == "aggregate"
    assert packet["local_candidates"]["metric_catalog_hits"][0]["measure_field"] == "mails.delivery_ms"
    proposal = {
        "schema_version": 1,
        "action": "clarify",
        "target_entities": ["mails", "organizations"],
        "projections": [
            {
                "kind": "aggregate",
                "field": "",
                "aggregate": "",
                "alias": "average_delivery_time",
                "rationale": "metric definition needed",
            }
        ],
        "filters": [],
        "joins": [
            {
                "from_entity": "mails",
                "from_field": "organization_id",
                "to_entity": "organizations",
                "to_field": "id",
                "rationale": "group by organization",
            }
        ],
        "group_by": ["organizations.name"],
        "order_by": [],
        "limit": 0,
        "ambiguity_questions": ["Which metric definition should define this metric?"],
        "evidence": [],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert result["issues"][0]["code"] == "clarify_auto_promoted_metric_catalog"
    assert result["sql"] == (
        'SELECT "organizations"."name", AVG("mails"."delivery_ms") '
        'AS "average_delivery_time" '
        'FROM "mails" JOIN "organizations" '
        'ON "mails"."organization_id" = "organizations"."id" '
        'GROUP BY "organizations"."name" '
        'ORDER BY "average_delivery_time" DESC'
    )


def test_render_resolution_proposal_promotes_distinct_count_metric_catalog(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        """
        CREATE TABLE metric_definitions (
            name TEXT PRIMARY KEY,
            display_label TEXT,
            metric_kind TEXT NOT NULL,
            subject_entity TEXT NOT NULL,
            numerator_field TEXT NOT NULL,
            numerator_operator TEXT NOT NULL,
            numerator_value TEXT NOT NULL,
            numerator_value_kind TEXT NOT NULL,
            denominator_field TEXT NOT NULL,
            scale REAL NOT NULL,
            required_entities_json TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            measure_field TEXT,
            aggregate TEXT,
            distinct_measure INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO metric_definitions("
        "name, display_label, metric_kind, subject_entity, numerator_field, "
        "numerator_operator, numerator_value, numerator_value_kind, "
        "denominator_field, scale, required_entities_json, aliases_json, "
        "measure_field, aggregate, distinct_measure"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "unique_organizations",
            "Unique organizations",
            "aggregate",
            "mails",
            "mails.organization_id",
            "=",
            "",
            "metric_definition",
            "mails.id",
            1.0,
            json.dumps(["mails"]),
            json.dumps(["unique organizations"]),
            "mails.organization_id",
            "COUNT",
            1,
        ),
    )
    conn.commit()
    conn.close()

    packet = build_rejected_query_packet(graph, "unique organizations by status")
    assert packet["local_candidates"]["metric_catalog_hits"][0]["distinct"] is True
    assert (
        packet["local_candidates"]["metric_catalog_hits"][0]["measure_field"]
        == "mails.organization_id"
    )
    proposal = {
        "schema_version": 1,
        "action": "clarify",
        "target_entities": ["mails"],
        "projections": [
            {
                "kind": "aggregate",
                "field": "",
                "aggregate": "",
                "distinct": False,
                "alias": "unique_organizations",
                "rationale": "metric definition needed",
            }
        ],
        "filters": [],
        "joins": [],
        "group_by": ["mails.status"],
        "order_by": [],
        "limit": 0,
        "ambiguity_questions": ["Which metric definition should define this metric?"],
        "evidence": [],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert result["issues"][0]["code"] == "clarify_auto_promoted_metric_catalog"
    assert result["sql"] == (
        'SELECT "mails"."status", COUNT(DISTINCT "mails"."organization_id") '
        'AS "unique_organizations" FROM "mails" GROUP BY "mails"."status" '
        'ORDER BY "unique_organizations" DESC'
    )


def test_render_resolution_proposal_supports_grouped_count(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "count sent mails by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "rationale": "count mails",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."organization_id", COUNT(*) AS "count" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' GROUP BY "mails"."organization_id"'
    )
    assert result["query_frame_candidate"]["group_by"] == ["mails.organization_id"]


def test_render_resolution_proposal_supports_aggregate_order_by_count(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "top organizations by sent mail count")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "rationale": "count sent mail rows",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["order_by"] = [
        {
            "field": "",
            "aggregate": "COUNT",
            "direction": "DESC",
            "rationale": "top groups sort by row count",
        }
    ]
    proposal["limit"] = 1

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."organization_id", COUNT(*) AS "count" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' GROUP BY "mails"."organization_id" '
        "ORDER BY COUNT(*) DESC LIMIT 1"
    )


def test_render_resolution_proposal_honors_count_projection_alias(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "top organizations by sent mail count")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "sent_mail_count",
            "rationale": "count sent mail rows",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["order_by"] = [
        {
            "field": "",
            "aggregate": "COUNT",
            "alias": "sent_mail_count",
            "direction": "DESC",
            "rationale": "top groups sort by row count",
        }
    ]
    proposal["limit"] = 1

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."organization_id", COUNT(*) AS "sent_mail_count" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' GROUP BY "mails"."organization_id" '
        'ORDER BY "sent_mail_count" DESC LIMIT 1'
    )


def test_render_resolution_proposal_supports_aggregate_order_by_avg(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "statuses by average mail id")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "aggregate",
            "field": "mails.id",
            "aggregate": "AVG",
            "rationale": "average numeric mail id",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.status"]
    proposal["order_by"] = [
        {
            "field": "mails.id",
            "aggregate": "AVG",
            "direction": "ASC",
            "rationale": "sort by the average aggregate",
        }
    ]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."status", AVG("mails"."id") AS "avg" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' GROUP BY "mails"."status" '
        'ORDER BY AVG("mails"."id") ASC'
    )


def test_render_resolution_proposal_supports_declared_multi_series_shape(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent mail counts by status over ordered on")
    proposal = _valid_sent_mail_proposal()
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "sent_mail_count",
            "rationale": "count sent mails",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.ordered_on", "mails.status"]
    proposal["order_by"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["result_shape"]["kind"] == "multi_series_chart"
    assert result["query_frame_candidate"]["result_shape"]["kind"] == "multi_series_chart"
    assert result["sql"] == (
        'SELECT "mails"."ordered_on", "mails"."status", '
        'COUNT(*) AS "sent_mail_count" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' '
        'GROUP BY "mails"."ordered_on", "mails"."status"'
    )


def test_render_resolution_proposal_treats_timestamp_group_as_time_series(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'closed_at', 'closed_at', 'timestamp', 'Closed At')"
    )
    conn.commit()
    conn.close()
    packet = build_rejected_query_packet(graph, "mail counts by status over closed at")
    proposal = _valid_sent_mail_proposal()
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "mail_count",
            "rationale": "count mails",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.closed_at", "mails.status"]
    proposal["order_by"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["result_shape"]["kind"] == "multi_series_chart"
    assert result["result_shape"]["group_roles"] == ["date", "status"]


def test_render_resolution_proposal_promotes_explicit_time_grain_clarification(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent mail counts by status over ordered on")
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "sent_mail_count",
            "rationale": "count sent mails",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.ordered_on", "mails.status"]
    proposal["order_by"] = []
    proposal["limit"] = 0
    proposal["ambiguity_questions"] = [
        "What time grain should I use for mails.ordered_on, day, week, or month?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["query_frame_candidate"]["route_reason"] == (
        "llm_resolution_validated_clarify_auto_promoted"
    )
    assert {issue["code"] for issue in result["issues"]} == {
        "clarify_auto_promoted_explicit_time_grain"
    }
    assert result["result_shape"]["kind"] == "multi_series_chart"
    assert result["sql"] == (
        'SELECT "mails"."ordered_on", "mails"."status", '
        'COUNT(*) AS "sent_mail_count" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' '
        'GROUP BY "mails"."ordered_on", "mails"."status"'
    )


def test_render_resolution_proposal_keeps_vague_time_grain_clarification_closed(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent mail counts by status over time")
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "sent_mail_count",
            "rationale": "count sent mails",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.ordered_on", "mails.status"]
    proposal["limit"] = 0
    proposal["ambiguity_questions"] = [
        "What time grain should I use for mails.ordered_on, day, week, or month?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    assert result["sql"] is None
    assert {issue["code"] for issue in result["issues"]} == {"proposal_not_route"}


def test_render_resolution_proposal_promotes_lifecycle_existence_when_no_status_value(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'paid_at', 'paid_at', 'timestamp', 'Paid At')"
    )
    conn.commit()
    conn.close()
    packet = build_rejected_query_packet(
        graph,
        "show average mail id by status for paid mails over ordered on",
    )
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "mails.ordered_on",
            "aggregate": "",
            "alias": "ordered_on",
            "rationale": "time axis",
        },
        {
            "kind": "field",
            "field": "mails.status",
            "aggregate": "",
            "alias": "status",
            "rationale": "series dimension",
        },
        {
            "kind": "aggregate",
            "field": "mails.id",
            "aggregate": "AVG",
            "alias": "avg_mail_id",
            "rationale": "requested average",
        },
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.ordered_on", "mails.status"]
    proposal["order_by"] = []
    proposal["limit"] = 0
    proposal["ambiguity_questions"] = [
        "How should we identify paid mails: by mails.status = 'paid' or mails.paid_at IS NOT NULL?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert {
        issue["code"] for issue in result["issues"]
    } == {"clarify_auto_promoted_lifecycle_existence"}
    assert '"mails"."paid_at" IS NOT NULL' in result["sql"]
    assert result["result_shape"]["kind"] == "multi_series_chart"


def test_render_resolution_proposal_keeps_lifecycle_existence_ambiguous_with_status_value(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'paid_at', 'paid_at', 'timestamp', 'Paid At')"
    )
    conn.execute(
        "UPDATE sample_values SET examples = '[\"sent\", \"paid\"]' "
        "WHERE field_canonical = 'mails.status'"
    )
    conn.execute(
        "INSERT INTO vocabulary(term, canonical_kind, canonical_value, confidence, source_layer) "
        "VALUES (?, 'scope_predicate', ?, ?, 2)",
        (
            "paid",
            json.dumps(
                {
                    "scope": "mails.status.paid",
                    "field": "mails.status",
                    "operator": "=",
                    "rawValue": "paid",
                }
            ),
            0.9,
        ),
    )
    conn.commit()
    conn.close()
    packet = build_rejected_query_packet(
        graph,
        "show average mail id by status for paid mails over ordered on",
    )
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "aggregate",
            "field": "mails.id",
            "aggregate": "AVG",
            "alias": "avg_mail_id",
            "rationale": "requested average",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.ordered_on", "mails.status"]
    proposal["limit"] = 0
    proposal["ambiguity_questions"] = [
        "How should we identify paid mails: by mails.status = 'paid' or mails.paid_at IS NOT NULL?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    assert result["sql"] is None
    assert {issue["code"] for issue in result["issues"]} == {"proposal_not_route"}


def test_render_resolution_proposal_classifies_schema_path_clarification(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    conn = sqlite3.connect(graph)
    conn.execute(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', 'owner_organization_id', 'owner_organization_id', 'integer', 'Owner Organization ID')"
    )
    conn.execute(
        "INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind) "
        "VALUES ('mails', 'owner_organization_id', 'organizations', 'id', 'many_to_one')"
    )
    conn.commit()
    conn.close()
    packet = build_rejected_query_packet(graph, "count mails by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["result_shape"] = "categorical_chart"
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "mail_count",
            "rationale": "count mails",
        },
        {
            "kind": "field",
            "field": "organizations.name",
            "aggregate": "",
            "alias": "organization",
            "rationale": "display organization",
        },
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["organizations.name"]
    proposal["limit"] = 0
    proposal["ambiguity_questions"] = [
        "Which organization path should be used: mails.organization_id or mails.owner_organization_id?"
    ]
    proposal["evidence"] = [
        {
            "claim": "Two organization paths exist.",
            "graph_refs": [
                "relationships.mails.organization_id->organizations.id",
                "relationships.mails.owner_organization_id->organizations.id",
            ],
        }
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    assert result["sql"] is None
    assert {issue["code"] for issue in result["issues"]} == {
        "clarification_required_schema_path"
    }
    issue = result["issues"][0]
    assert "mails.organization_id" in issue["candidate_fields"]
    assert "mails.owner_organization_id" in issue["candidate_fields"]
    assert {"from": "mails.organization_id", "to": "organizations.id"} in issue[
        "candidate_relationships"
    ]
    assert {"from": "mails.owner_organization_id", "to": "organizations.id"} in issue[
        "candidate_relationships"
    ]
    assert [option["id"] for option in issue["clarification_options"]] == [
        "schema_path_1",
        "schema_path_2",
    ]
    assert issue["clarification_options"][0]["relationships"] == [
        {"from": "mails.organization_id", "to": "organizations.id"}
    ]
    assert issue["clarification_options"][1]["relationships"] == [
        {"from": "mails.owner_organization_id", "to": "organizations.id"}
    ]

    direct = render_resolution_proposal(
        packet,
        proposal,
        dialect="sqlite",
        clarification_choice="schema_path_1",
    )
    owner = render_resolution_proposal(
        packet,
        proposal,
        dialect="sqlite",
        clarification_choice="schema_path_2",
    )

    assert direct["valid"] is True
    assert direct["query_frame_candidate"]["route_reason"] == (
        "llm_resolution_validated_clarification_choice"
    )
    assert '"mails"."organization_id" = "organizations"."id"' in direct["sql"]
    assert owner["valid"] is True
    assert '"mails"."owner_organization_id" = "organizations"."id"' in owner["sql"]
    assert {
        issue["code"] for issue in owner["issues"]
    } == {"clarification_choice_applied_schema_path"}


def test_render_resolution_proposal_rejects_declared_multi_series_without_time_group(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent mail counts by status over ordered on")
    proposal = _valid_sent_mail_proposal()
    proposal["result_shape"] = "multi_series_chart"
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "alias": "sent_mail_count",
            "rationale": "count sent mails",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.status"]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    assert result["sql"] is None
    assert result["result_shape"]["kind"] == "categorical_chart"
    assert any(issue["code"] == "result_shape_mismatch" for issue in result["issues"])


def test_resolution_proposal_validator_rejects_unknown_result_shape(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent mail counts by status")
    proposal = _valid_sent_mail_proposal()
    proposal["result_shape"] = "sparkly_chart"

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert any(issue["code"] == "invalid_result_shape" for issue in validation["issues"])


def test_render_resolution_proposal_dedupes_grouped_projection_field(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "count sent mails by status")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "mails.status",
            "aggregate": "",
            "rationale": "status is the grouped display field",
        },
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "rationale": "count rows in each status group",
        },
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.status"]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."status", COUNT(*) AS "count" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' GROUP BY "mails"."status"'
    )


def test_render_resolution_proposal_supports_distinct_entity_lists(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "unique organizations with sent mails")
    proposal = _valid_sent_mail_proposal()
    proposal["distinct"] = True
    proposal["target_entities"] = ["mails"]
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "organizations.name",
            "aggregate": "",
            "rationale": "organization name is the requested display field",
        }
    ]
    proposal["joins"] = [
        {
            "from_entity": "mails",
            "from_field": "organization_id",
            "to_entity": "organizations",
            "to_field": "id",
            "rationale": "mail rows connect to organizations",
        }
    ]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT DISTINCT "organizations"."name" FROM "mails" '
        'JOIN "organizations" ON "mails"."organization_id" = "organizations"."id" '
        'WHERE "mails"."status" = \'sent\' LIMIT 100'
    )


def test_render_resolution_proposal_ignores_global_distinct_for_grouped_aggregate(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "unique organizations by ordered on")
    proposal = _valid_sent_mail_proposal()
    proposal["distinct"] = True
    proposal["projections"] = [
        {
            "kind": "aggregate",
            "field": "mails.organization_id",
            "aggregate": "COUNT",
            "distinct": True,
            "alias": "unique_organizations",
            "rationale": "count distinct organizations per date",
        }
    ]
    proposal["joins"] = []
    proposal["group_by"] = ["mails.ordered_on"]
    proposal["filters"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."ordered_on", COUNT(DISTINCT "mails"."organization_id") '
        'AS "unique_organizations" FROM "mails" GROUP BY "mails"."ordered_on"'
    )


def test_render_resolution_proposal_reorients_explicit_joins_from_base_entity(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "unique organizations with sent mails")
    proposal = _valid_sent_mail_proposal()
    proposal["distinct"] = True
    proposal["target_entities"] = ["organizations", "mails"]
    proposal["projections"] = [
        {
            "kind": "field",
            "field": "organizations.name",
            "aggregate": "",
            "rationale": "organization name is the requested display field",
        }
    ]
    proposal["joins"] = [
        {
            "from_entity": "mails",
            "from_field": "organization_id",
            "to_entity": "organizations",
            "to_field": "id",
            "rationale": "packet relationship is oriented from child to parent",
        }
    ]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT DISTINCT "organizations"."name" FROM "organizations" '
        'JOIN "mails" ON "organizations"."id" = "mails"."organization_id" '
        'WHERE "mails"."status" = \'sent\' LIMIT 100'
    )
    assert 'JOIN "organizations"' not in result["sql"]


def test_render_resolution_proposal_promotes_open_pipeline_stage_policy() -> None:
    packet = _business_schema_packet("Top 3 reps by open pipeline closing in Q2 2024")
    proposal: dict[str, object] = {
        "schema_version": 1,
        "action": "clarify",
        "confidence": 0.62,
        "intent": "rank reps by open pipeline value closing in Q2 2024",
        "target_entities": ["deals", "team_members"],
        "projections": [
            {
                "kind": "field",
                "field": "team_members.person_name",
                "aggregate": "",
                "alias": "rep_name",
                "rationale": "rep display field",
            },
            {
                "kind": "aggregate",
                "field": "deals.deal_amount",
                "aggregate": "SUM",
                "alias": "open_pipeline_amount",
                "rationale": "pipeline amount",
            },
        ],
        "filters": [
            {
                "field": "deals.expected_close_date",
                "operator": "range",
                "value": "2024-04-01..2024-07-01",
                "value_kind": "literal",
                "rationale": "Q2 close window",
            }
        ],
        "joins": [
            {
                "from_entity": "deals",
                "from_field": "owner_member_id",
                "to_entity": "team_members",
                "to_field": "id",
                "rationale": "owner rep",
            }
        ],
        "group_by": ["team_members.person_name"],
        "order_by": [
            {
                "field": "deals.deal_amount",
                "aggregate": "SUM",
                "alias": "open_pipeline_amount",
                "direction": "DESC",
                "rationale": "top reps",
            }
        ],
        "limit": 3,
        "ambiguity_questions": [
            "What does open pipeline mean: deals not yet closed or open invoices?"
        ],
        "evidence": [],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["query_frame_candidate"]["route_reason"] == (
        "llm_resolution_validated_clarify_auto_promoted"
    )
    assert {
        issue["code"] for issue in result["issues"]
    } == {"clarify_auto_promoted_open_stage_metric"}
    assert (
        '"deals"."deal_stage" NOT IN (\'closed_lost\', \'closed_won\')'
        in result["sql"]
    )
    assert (
        '"deals"."expected_close_date" BETWEEN \'2024-04-01\' AND \'2024-07-01\''
        in result["sql"]
    )


def test_render_resolution_proposal_promotes_lifecycle_event_date_policy() -> None:
    packet = _business_schema_packet("List churned accounts in Q1 2024 with segment and ARR")
    proposal: dict[str, object] = {
        "schema_version": 1,
        "action": "clarify",
        "confidence": 0.74,
        "intent": "list churned accounts in Q1 with segment and ARR",
        "target_entities": ["organizations"],
        "projections": [
            {
                "kind": "field",
                "field": "organizations.organization_name",
                "aggregate": "",
                "alias": "organization_name",
                "rationale": "account display",
            },
            {
                "kind": "field",
                "field": "organizations.market_segment",
                "aggregate": "",
                "alias": "market_segment",
                "rationale": "requested segment",
            },
            {
                "kind": "field",
                "field": "organizations.annual_recurring_revenue",
                "aggregate": "",
                "alias": "annual_recurring_revenue",
                "rationale": "requested ARR",
            },
        ],
        "filters": [
            {
                "field": "organizations.account_state",
                "operator": "=",
                "value": "churned",
                "value_kind": "value_dictionary",
                "rationale": "churned account status",
            },
            {
                "field": "organizations.created_date",
                "operator": "range",
                "value": "2024-01-01..2024-04-01",
                "value_kind": "literal",
                "rationale": "provider guessed account-created date",
            },
        ],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 0,
        "ambiguity_questions": [
            "For in Q1 2024, should the quarter filter apply to account created date or renewal date?"
        ],
        "evidence": [],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert {
        issue["code"] for issue in result["issues"]
    } == {
        "clarify_auto_promoted_lifecycle_event_date",
        "default_row_limit_applied",
    }
    assert 'JOIN "recurring_contracts"' in result["sql"]
    assert '"organizations"."created_date"' not in result["sql"]
    assert '"recurring_contracts"."subscription_state" = \'cancelled\'' in result["sql"]
    assert (
        '"recurring_contracts"."ended_date" BETWEEN \'2024-01-01\' AND \'2024-04-01\''
        in result["sql"]
    )


def test_render_resolution_proposal_normalizes_range_operator(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "count mails in March 2024")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "count",
            "field": "",
            "aggregate": "COUNT",
            "rationale": "count rows",
        }
    ]
    proposal["filters"] = [
        {
            "field": "mails.ordered_on",
            "operator": "range",
            "value": "2024-03-01..2024-04-01",
            "value_kind": "literal",
            "rationale": "provider date range shorthand",
        }
    ]
    proposal["joins"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT COUNT(*) AS "count" FROM "mails" '
        "WHERE \"mails\".\"ordered_on\" BETWEEN '2024-03-01' AND '2024-04-01'"
    )


def test_render_resolution_proposal_supports_conditional_rate(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent rate by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "conditional_rate",
            "field": "",
            "aggregate": "",
            "alias": "sent_rate",
            "numerator_field": "mails.status",
            "numerator_operator": "=",
            "numerator_value": "sent",
            "numerator_value_kind": "value_dictionary",
            "denominator_field": "mails.id",
            "scale": 100.0,
            "rationale": "percentage of mails whose status is sent",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["order_by"] = [
        {
            "field": "",
            "aggregate": "",
            "alias": "sent_rate",
            "direction": "DESC",
            "rationale": "sort by the rendered rate",
        }
    ]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."organization_id", '
        'CAST(SUM(CASE WHEN "mails"."status" = \'sent\' THEN 1 ELSE 0 END) '
        'AS REAL) * 100.0 / NULLIF(COUNT("mails"."id"), 0) AS "sent_rate" '
        'FROM "mails" GROUP BY "mails"."organization_id" '
        'ORDER BY "sent_rate" DESC'
    )


def test_rejected_query_packet_exposes_metric_formula_hit_for_rate(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)

    packet = build_rejected_query_packet(graph, "sent rate by organization")

    assert packet["local_candidates"]["metric_formula_ambiguous"] is False
    assert packet["local_candidates"]["metric_formula_hits"] == [
        {
            "metric_kind": "conditional_rate",
            "alias": "sent_rate",
            "numerator_field": "mails.status",
            "numerator_operator": "=",
            "numerator_value": "sent",
            "numerator_value_kind": "value_dictionary",
            "denominator_field": "mails.id",
            "scale": 100.0,
            "matched_tokens": ["rate", "sent"],
            "source": "packet_value_formula",
        }
    ]


def test_render_resolution_proposal_promotes_packet_metric_formula(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent rate by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["projections"] = [
        {
            "kind": "conditional_rate",
            "field": "",
            "aggregate": "",
            "alias": "sent_rate",
            "numerator_field": "",
            "numerator_operator": "",
            "numerator_value": None,
            "numerator_value_kind": "",
            "denominator_field": "",
            "scale": None,
            "rationale": "provider asks for metric definition",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["order_by"] = []
    proposal["limit"] = 0
    proposal["ambiguity_questions"] = [
        "Should sent rate mean count mails.status = sent over all mails?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["proposal_action"] == "clarify"
    assert result["effective_action"] == "route"
    assert result["issues"][0]["code"] == "clarify_auto_promoted_metric_formula"
    assert result["sql"] == (
        'SELECT "mails"."organization_id", '
        'CAST(SUM(CASE WHEN "mails"."status" = \'sent\' THEN 1 ELSE 0 END) '
        'AS REAL) * 100.0 / NULLIF(COUNT("mails"."id"), 0) AS "sent_rate" '
        'FROM "mails" GROUP BY "mails"."organization_id" '
        'ORDER BY "sent_rate" DESC'
    )


def test_ambiguous_packet_metric_formula_stays_closed(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent or draft rate by organization")

    assert packet["local_candidates"]["metric_formula_ambiguous"] is True
    assert {
        hit["numerator_value"] for hit in packet["local_candidates"]["metric_formula_hits"]
    } == {"sent", "draft"}

    proposal = _valid_sent_mail_proposal()
    proposal["action"] = "clarify"
    proposal["projections"] = [
        {
            "kind": "conditional_rate",
            "field": "",
            "aggregate": "",
            "alias": "status_rate",
            "numerator_field": "",
            "numerator_operator": "",
            "numerator_value": None,
            "numerator_value_kind": "",
            "denominator_field": "",
            "scale": None,
            "rationale": "provider asks for metric definition",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["ambiguity_questions"] = [
        "Should status rate count sent or draft mails?"
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    issue_codes = [issue["code"] for issue in result["issues"]]
    assert "clarify_auto_promoted_metric_formula" not in issue_codes
    assert "clarify_auto_promoted_metric_catalog" not in issue_codes
    assert not result.get("sql")


def test_render_resolution_proposal_uses_mysql_rate_cast(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent rate by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "conditional_rate",
            "field": "",
            "aggregate": "",
            "alias": "sent_rate",
            "numerator_field": "mails.status",
            "numerator_operator": "=",
            "numerator_value": "sent",
            "numerator_value_kind": "value_dictionary",
            "denominator_field": "mails.id",
            "scale": 100.0,
            "rationale": "percentage of mails whose status is sent",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["order_by"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="mysql")

    assert result["valid"] is True
    assert "AS DOUBLE) * 100.0" in result["sql"]
    assert "AS REAL)" not in result["sql"]


def test_render_resolution_proposal_defaults_null_rate_scale(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent rate by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "conditional_rate",
            "field": "",
            "aggregate": "",
            "alias": "sent_rate",
            "numerator_field": "mails.status",
            "numerator_operator": "=",
            "numerator_value": "sent",
            "numerator_value_kind": "value_dictionary",
            "denominator_field": "mails.id",
            "scale": None,
            "rationale": "fraction of mails whose status is sent",
        }
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = ["mails.organization_id"]
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert "* 1.0 / NULLIF" in result["sql"]


def test_resolution_proposal_validator_rejects_bad_order_alias(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "sent rate by organization")
    proposal = _valid_sent_mail_proposal()
    proposal["order_by"] = [
        {
            "field": "",
            "aggregate": "",
            "alias": "missing_rate",
            "direction": "DESC",
            "rationale": "not a projection alias",
        },
        {
            "field": "",
            "aggregate": "",
            "alias": "bad alias",
            "direction": "DESC",
            "rationale": "invalid identifier",
        },
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert {issue["code"] for issue in validation["issues"]} >= {
        "unknown_order_alias",
        "invalid_alias",
    }


def test_resolution_proposal_validator_rejects_invalid_aggregate_order(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "statuses by median mail id")
    proposal = _valid_sent_mail_proposal()
    proposal["order_by"] = [
        {
            "field": "mails.id",
            "aggregate": "MEDIAN",
            "direction": "DESC",
            "rationale": "unsupported aggregate",
        },
        {
            "field": "",
            "aggregate": "AVG",
            "direction": "ASC",
            "rationale": "missing field for avg",
        },
    ]

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert {issue["code"] for issue in validation["issues"]} >= {
        "unsupported_aggregate_order",
        "missing_aggregate_field",
    }


def test_resolution_proposal_validator_rejects_bad_distinct_aggregate(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "average distinct mail ids")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "aggregate",
            "field": "mails.id",
            "aggregate": "AVG",
            "distinct": True,
            "alias": "average_distinct_ids",
            "rationale": "invalid distinct aggregate",
        },
        {
            "kind": "field",
            "field": "mails.status",
            "aggregate": "",
            "distinct": True,
            "alias": "status",
            "rationale": "invalid non-aggregate distinct",
        },
    ]
    proposal["filters"] = []
    proposal["joins"] = []
    proposal["group_by"] = []
    proposal["order_by"] = []
    proposal["limit"] = 0

    validation = validate_resolution_proposal(packet, proposal)

    assert validation["valid"] is False
    assert {issue["code"] for issue in validation["issues"]} >= {
        "unsupported_distinct_aggregate",
        "distinct_requires_aggregate",
    }


def test_render_resolution_proposal_supports_in_and_between_filters(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent or draft mails from id 1 to 10")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.status",
            "operator": "IN",
            "value": ["sent", "draft"],
            "value_kind": "literal",
            "rationale": "status list",
        },
        {
            "field": "mails.id",
            "operator": "BETWEEN",
            "value": [1, 10],
            "value_kind": "literal",
            "rationale": "numeric range",
        },
    ]
    proposal["joins"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."subject" FROM "mails" '
        'WHERE "mails"."status" IN (\'sent\', \'draft\') '
        'AND "mails"."id" BETWEEN 1 AND 10 LIMIT 100'
    )


def test_render_resolution_proposal_supports_null_filters(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails with an id")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.status",
            "operator": "=",
            "value": "sent",
            "value_kind": "value_dictionary",
            "rationale": "status evidence",
        },
        {
            "field": "mails.id",
            "operator": "IS NOT NULL",
            "value": None,
            "value_kind": "null_check",
            "rationale": "id must be present",
        },
    ]
    proposal["joins"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."subject" FROM "mails" '
        'WHERE "mails"."status" = \'sent\' '
        'AND "mails"."id" IS NOT NULL LIMIT 100'
    )


def test_render_resolution_proposal_normalizes_provider_null_filter_syntax(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails with an id")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.id",
            "operator": "IS NOT",
            "value": None,
            "value_kind": "null_check",
            "rationale": "provider used split null operator syntax",
        }
    ]
    proposal["joins"] = []
    proposal["limit"] = 0

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert validation["valid"] is True
    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."subject" FROM "mails" '
        'WHERE "mails"."id" IS NOT NULL LIMIT 100'
    )


def test_resolution_proposal_validator_accepts_text_backed_date_role_filters(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails after 2024-02-01")
    proposal = _valid_sent_mail_proposal()
    proposal["filters"] = [
        {
            "field": "mails.ordered_on",
            "operator": ">=",
            "value": "2024-02-01",
            "value_kind": "literal",
            "rationale": "ordered_on is a date-role text column",
        }
    ]
    proposal["joins"] = []
    proposal["limit"] = 0

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "mails"."subject" FROM "mails" '
        'WHERE "mails"."ordered_on" >= \'2024-02-01\' LIMIT 100'
    )


def test_render_resolution_proposal_rejects_all_column_projection(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show all sent mails")
    proposal = _valid_sent_mail_proposal()
    proposal["projections"] = [
        {
            "kind": "all",
            "field": "",
            "aggregate": "",
            "rationale": "row dump",
        }
    ]

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    assert result["issues"][0]["code"] == "all_projection_forbidden"
    assert result["sql"] is None


def test_openai_request_uses_strict_structured_resolution_schema(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show mails")

    request = build_openai_resolution_request(packet, model="gpt-test")
    schema = resolution_json_schema()

    assert request["model"] == "gpt-test"
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == schema
    assert "Do not generate final SQL" in request["instructions"]
    assert "field-scoped value_dictionary" in request["instructions"]
    assert "bare field names" in request["instructions"]
    assert "prefer display fields" in request["instructions"]
    assert "by <entity>" in request["instructions"]
    assert "category/status/type/tier/segment field" in request["instructions"]
    assert "value_kind=value_dictionary" in request["instructions"]
    assert "packet.resolution_task.kind is resolve_value_binding" in request["instructions"]
    assert "Metric catalog hits and metric formula hits" in request["instructions"]
    assert "Do not infer a hidden lifecycle/status filter" in request["instructions"]
    assert "Omit order_by unless" in request["instructions"]
    assert "Do not route ambiguous physical shard-family members" in request["instructions"]
    assert "Do not route sensitive tables or fields" in request["instructions"]
    assert "positive limit for row-list results" in request["instructions"]


def test_provider_packet_compaction_strips_runtime_atlas_and_keeps_candidate_schema(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails by organization")
    packet["query_frame"] = {
        "runtime_query_frame": {
            "required_entities": ["mails"],
            "projection": {"fields": ["mails.subject"]},
            "predicates": [{"field": "mails.status"}],
        },
        "semantic_atlas": {"huge": ["x" * 2000 for _ in range(50)]},
    }
    original_size = len(json.dumps(packet))

    compact = compact_resolution_packet_for_provider(
        packet,
        max_entities=4,
        max_fields_per_entity=4,
        max_relationships=4,
    )

    assert "semantic_atlas" not in compact["query_frame"]
    assert len(json.dumps(compact)) < original_size
    assert compact["schema_card"]["summary"]["provider_compacted"] is True
    mails = next(
        entity for entity in compact["schema_card"]["entities"] if entity["name"] == "mails"
    )
    mail_fields = {field["name"] for field in mails["fields"]}
    assert {"subject", "status"}.issubset(mail_fields)
    assert compact["schema_card"]["relationships"] == [
        {
            "from": "mails.organization_id",
            "to": "organizations.id",
            "kind": "many_to_one",
        }
    ]


def test_ambiguous_value_reject_builds_typed_resolution_task(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_ambiguous_value_graph(graph)
    query_frame = {
        "bound_query_plan": {
            "base_entity": "orders",
            "reject_reason": "ambiguous_unscoped_value_field",
            "joins": [
                {
                    "from_entity": "orders",
                    "from_field": "orders.customer_id",
                    "to_entity": "customers",
                    "to_field": "customers.id",
                }
            ],
            "predicates": [
                {"field": "orders.region", "operator": "=", "value": "North"}
            ],
            "projections": [
                {
                    "expression": "SUM(orders.amount)",
                    "field": "orders.amount",
                }
            ],
        },
        "runtime_query_frame": {
            "required_entities": ["orders", "customers"],
            "predicates": [
                {"field": "orders.region", "operator": "=", "value": "North"}
            ],
            "projection": {"fields": ["orders.amount"]},
        },
        "semantic_atlas": {"large": ["x" * 1000]},
    }

    packet = build_rejected_query_packet(
        graph,
        "total order amount for north customers",
        query_frame=query_frame,
    )

    task = packet["resolution_task"]
    assert task["kind"] == "resolve_value_binding"
    assert task["reason"] == "ambiguous_unscoped_value_field"
    binding = task["unresolved_value_bindings"][0]
    assert binding["value"] == "North"
    assert binding["selected_field"] == "orders.region"
    fields = {candidate["field"] for candidate in binding["candidate_fields"]}
    assert {"orders.region", "customers.region"} <= fields
    assert "regions.name" not in fields
    assert {
        "from": "orders.customer_id",
        "to": "customers.id",
        "kind": "many_to_one",
    } in packet["schema_card"]["relationships"]

    compact = compact_resolution_packet_for_provider(
        packet,
        max_entities=3,
        max_fields_per_entity=3,
        max_relationships=3,
    )

    assert "semantic_atlas" not in compact["query_frame"]
    compact_entities = {
        entity["name"]: entity for entity in compact["schema_card"]["entities"]
    }
    assert {
        field["name"] for field in compact_entities["orders"]["fields"]
    } >= {"region", "amount"}
    assert {
        field["name"] for field in compact_entities["customers"]["fields"]
    } >= {"region"}


def test_resolution_task_candidates_are_allowed_as_field_scoped_filters(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_ambiguous_value_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "show orders for north customers",
        query_frame={
            "bound_query_plan": {
                "base_entity": "orders",
                "reject_reason": "ambiguous_unscoped_value_field",
                "joins": [
                    {
                        "from_entity": "orders",
                        "from_field": "orders.customer_id",
                        "to_entity": "customers",
                        "to_field": "customers.id",
                    }
                ],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"}
                ],
                "projections": [{"field": "orders.id", "expression": "orders.id"}],
            },
            "runtime_query_frame": {
                "required_entities": ["orders", "customers"],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"}
                ],
                "projection": {"fields": ["orders.id"]},
            },
        },
    )
    proposal = {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.88,
        "intent": "list orders for north customers",
        "target_entities": ["orders", "customers"],
        "projections": [
            {
                "kind": "field",
                "field": "orders.id",
                "aggregate": "",
                "rationale": "orders are the requested rows",
            }
        ],
        "filters": [
            {
                "field": "customers.region",
                "operator": "=",
                "value": "North",
                "value_kind": "sample_value",
                "rationale": "resolution task maps North to customer region",
            }
        ],
        "joins": [
            {
                "from_entity": "orders",
                "from_field": "customer_id",
                "to_entity": "customers",
                "to_field": "id",
                "rationale": "relationship exists in the packet",
            }
        ],
        "group_by": [],
        "order_by": [],
        "limit": 100,
        "ambiguity_questions": [],
        "evidence": [
            {
                "claim": "North can bind to customers.region",
                "graph_refs": ["customers.region"],
            }
        ],
        "safety_notes": [],
    }

    validation = validate_resolution_proposal(packet, proposal)
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert validation["valid"] is True
    assert result["valid"] is True
    assert result["sql"] == (
        'SELECT "orders"."id" FROM "orders" '
        'JOIN "customers" ON "orders"."customer_id" = "customers"."id" '
        'WHERE "customers"."region" = \'North\' LIMIT 100'
    )


def test_resolution_task_auto_replaces_selected_ambiguous_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_ambiguous_value_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "show orders for north customers",
        query_frame={
            "bound_query_plan": {
                "base_entity": "orders",
                "reject_reason": "ambiguous_unscoped_value_field",
                "joins": [
                    {
                        "from_entity": "orders",
                        "from_field": "orders.customer_id",
                        "to_entity": "customers",
                        "to_field": "customers.id",
                    }
                ],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"}
                ],
                "projections": [{"field": "orders.id", "expression": "orders.id"}],
            },
            "runtime_query_frame": {
                "required_entities": ["orders", "customers"],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"}
                ],
                "projection": {"fields": ["orders.id"]},
            },
        },
    )
    proposal = {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.83,
        "intent": "list orders for north customers",
        "target_entities": ["orders"],
        "projections": [
            {
                "kind": "field",
                "field": "orders.id",
                "aggregate": "",
                "rationale": "orders are the requested rows",
            }
        ],
        "filters": [
            {
                "field": "orders.region",
                "operator": "=",
                "value": "North",
                "value_kind": "sample_value",
                "rationale": "runtime selected the ambiguous value field",
            }
        ],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 100,
        "ambiguity_questions": [],
        "evidence": [
            {"claim": "North is packet-backed", "graph_refs": ["orders.region"]}
        ],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert result["issues"][0]["code"] == "value_binding_auto_resolved"
    assert result["query_frame_candidate"]["predicates"] == [
        {"field": "customers.region", "value": "North", "operator": "="}
    ]
    assert result["query_frame_candidate"]["required_entities"] == [
        "customers",
        "orders",
    ]
    assert result["sql"] == (
        'SELECT "orders"."id" FROM "orders" '
        'JOIN "customers" ON "orders"."customer_id" = "customers"."id" '
        'WHERE "customers"."region" = \'North\' LIMIT 100'
    )


def test_resolution_task_auto_co_locates_followup_ambiguous_filter(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_ambiguous_value_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "show orders for north customers with priority",
        query_frame={
            "bound_query_plan": {
                "base_entity": "orders",
                "reject_reason": "ambiguous_unscoped_value_field",
                "joins": [
                    {
                        "from_entity": "orders",
                        "from_field": "orders.customer_id",
                        "to_entity": "customers",
                        "to_field": "customers.id",
                    }
                ],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"},
                    {"field": "orders.segment", "operator": "=", "value": "Priority"},
                ],
                "projections": [{"field": "orders.id", "expression": "orders.id"}],
            },
            "runtime_query_frame": {
                "required_entities": ["orders", "customers"],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"},
                    {"field": "orders.segment", "operator": "=", "value": "Priority"},
                ],
                "projection": {"fields": ["orders.id"]},
            },
        },
    )
    proposal = {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.83,
        "intent": "list priority north customer orders",
        "target_entities": ["orders"],
        "projections": [
            {
                "kind": "field",
                "field": "orders.id",
                "aggregate": "",
                "rationale": "orders are the requested rows",
            }
        ],
        "filters": [
            {
                "field": "orders.region",
                "operator": "=",
                "value": "North",
                "value_kind": "sample_value",
                "rationale": "runtime selected the ambiguous value field",
            },
            {
                "field": "orders.segment",
                "operator": "=",
                "value": "Priority",
                "value_kind": "sample_value",
                "rationale": "runtime selected another ambiguous value field",
            },
        ],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 100,
        "ambiguity_questions": [],
        "evidence": [
            {"claim": "values are packet-backed", "graph_refs": ["orders.region"]}
        ],
        "safety_notes": [],
    }

    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is True
    assert [issue["code"] for issue in result["issues"]] == [
        "value_binding_auto_resolved",
        "value_binding_auto_resolved",
    ]
    assert result["query_frame_candidate"]["predicates"] == [
        {"field": "customers.region", "value": "North", "operator": "="},
        {"field": "customers.segment", "value": "Priority", "operator": "="},
    ]
    assert result["sql"] == (
        'SELECT "orders"."id" FROM "orders" '
        'JOIN "customers" ON "orders"."customer_id" = "customers"."id" '
        'WHERE "customers"."region" = \'North\' '
        'AND "customers"."segment" = \'Priority\' LIMIT 100'
    )


def test_runtime_frame_resolution_proposal_uses_value_binding_repair(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_ambiguous_value_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "show orders for north customers",
        query_frame={
            "bound_query_plan": {
                "base_entity": "orders",
                "reject_reason": "ambiguous_unscoped_value_field",
                "joins": [
                    {
                        "from_entity": "orders",
                        "from_field": "orders.customer_id",
                        "to_entity": "customers",
                        "to_field": "customers.id",
                    }
                ],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"}
                ],
                "projections": [{"field": "orders.id", "expression": "orders.id"}],
            },
        },
    )

    proposal = build_runtime_frame_resolution_proposal(packet)

    assert proposal is not None
    assert proposal["filters"][0]["field"] == "orders.region"
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")
    assert result["valid"] is True
    assert result["issues"][0]["code"] == "value_binding_auto_resolved"
    assert result["query_frame_candidate"]["predicates"] == [
        {"field": "customers.region", "value": "North", "operator": "="}
    ]


def test_runtime_frame_resolution_proposal_fails_closed_when_value_unresolved(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_ambiguous_value_graph(graph)
    packet = build_rejected_query_packet(
        graph,
        "show orders for north",
        query_frame={
            "bound_query_plan": {
                "base_entity": "orders",
                "reject_reason": "ambiguous_unscoped_value_field",
                "joins": [
                    {
                        "from_entity": "orders",
                        "from_field": "orders.customer_id",
                        "to_entity": "customers",
                        "to_field": "customers.id",
                    }
                ],
                "predicates": [
                    {"field": "orders.region", "operator": "=", "value": "North"}
                ],
                "projections": [{"field": "orders.id", "expression": "orders.id"}],
            },
        },
    )

    proposal = build_runtime_frame_resolution_proposal(packet)
    assert proposal is not None
    result = render_resolution_proposal(packet, proposal, dialect="sqlite")

    assert result["valid"] is False
    assert result["issues"][0]["code"] == "value_binding_unresolved"
    assert result["sql"] is None


def test_openai_request_uses_compact_provider_packet(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails by organization")
    packet["query_frame"] = {
        "runtime_query_frame": {"required_entities": ["mails"]},
        "semantic_atlas": {"huge": ["x" * 2000 for _ in range(50)]},
    }

    request = build_openai_resolution_request(packet, model="gpt-test")
    provider_packet = json.loads(request["input"])

    assert request["model"] == "gpt-test"
    assert "semantic_atlas" not in provider_packet["query_frame"]
    assert provider_packet["schema_card"]["summary"]["provider_compacted"] is True


def test_resolution_json_schema_closed_objects_require_all_properties() -> None:
    schema = resolution_json_schema()

    def assert_closed_objects_require_all_properties(node: object) -> None:
        if isinstance(node, dict):
            if node.get("additionalProperties") is False and "properties" in node:
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                assert_closed_objects_require_all_properties(value)
        elif isinstance(node, list):
            for value in node:
                assert_closed_objects_require_all_properties(value)

    assert_closed_objects_require_all_properties(schema)


def test_openai_request_uses_current_default_model(tmp_path: Path) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show mails")

    request = build_openai_resolution_request(packet)

    assert request["model"] == DEFAULT_OPENAI_MODEL


def test_openai_chat_resolution_request_uses_json_object_format(
    tmp_path: Path,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails")

    request = build_openai_chat_resolution_request(packet, model="llama-test")

    assert request["model"] == "llama-test"
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][1]["role"] == "user"
    assert "semsql_rejected_query_packet" in request["messages"][1]["content"]


def test_openai_chat_compatible_provider_validates_and_redacts_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = tmp_path / "g.semsql"
    _make_graph(graph)
    packet = build_rejected_query_packet(graph, "show sent mails")
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return json.dumps(
                {
                    "id": "chatcmpl-test",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(_valid_sent_mail_proposal())
                            }
                        }
                    ],
                }
            ).encode("utf-8")

    def fake_urlopen(req: Any, timeout: float) -> FakeResponse:
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        data = req.data
        assert isinstance(data, bytes)
        captured["body"] = json.loads(data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(
        "semsql_eval.llm_resolution.urllib.request.urlopen",
        fake_urlopen,
    )

    result = call_openai_chat_compatible_resolution(
        packet,
        api_key="test-key",
        base_url=DEFAULT_GROQ_BASE_URL,
        model="llama-test",
        source="groq_chat_completions_api",
        timeout_seconds=1.5,
    )

    assert captured["url"] == DEFAULT_GROQ_BASE_URL + "/chat/completions"
    assert captured["timeout"] == 1.5
    assert result["source"] == "groq_chat_completions_api"
    assert result["model"] == "llama-test"
    assert result["validation"]["valid"] is True
    redacted_messages = result["request"]["messages"]
    assert redacted_messages[1]["content"] == "<packet redacted from retained provider result>"
