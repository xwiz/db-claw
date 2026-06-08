"""SchemaCard and LLM-resolution packet helpers.

This module deliberately stops before SQL execution. The LLM may propose a
structured resolution, but local SemSQL validation must still own rendering,
scoping, and execution.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from importlib import import_module
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]
ResolutionProvider = Callable[[JsonObject], JsonObject]

DEFAULT_SHARD_ANCHORS = (
    "accounts",
    "clients",
    "customers",
    "employees",
    "members",
    "organizations",
    "organisations",
    "tenants",
    "users",
)
SCOPE_ENTITY_TOKENS = {
    "account",
    "accounts",
    "client",
    "clients",
    "company",
    "companies",
    "customer",
    "customers",
    "organisation",
    "organisations",
    "organization",
    "organizations",
    "team",
    "teams",
    "tenant",
    "tenants",
    "workspace",
    "workspaces",
}
DEFAULT_OPENAI_MODEL = "gpt-5.2"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_FALLBACK_ROW_LIMIT = 100
MAX_FALLBACK_ROW_LIMIT = 1000
RESOLUTION_ASSISTANT_INSTRUCTIONS = (
    "You are a SemSQL resolution assistant. Do not generate final SQL. "
    "Return only a structured resolution proposal over the provided SchemaCard. "
    "If the query is ambiguous, choose action=clarify and ask the smallest "
    "useful question. Use only entities, fields, and relationships present in "
    "the packet. Use group_by for grouped analytics such as count by, average "
    "by, top by, or compare by. Prefer action=route when every requested "
    "projection, filter, join, grouping, ordering, and limit can be backed by "
    "packet evidence. Use action=clarify only when a missing or ambiguous "
    "choice changes the result set, metric, filter, grouping, or time window. "
    "Set result_shape to scalar_metric, table, categorical_chart, "
    "time_series_chart, or multi_series_chart when known. For "
    "multi_series_chart, include both a date/time group_by field and a "
    "non-date segment group_by field. "
    "Do not clarify merely to ask whether to display a human-readable name or "
    "an ID; prefer display fields when available. When the query says by "
    "<entity>, by <role>, per <entity>, or per <role>, add that role's "
    "display/name field as a projection and group_by; do not clarify between "
    "ID and display label when a display field is present. Include every "
    "semantic predicate from the user question. When a predicate word has a "
    "field-scoped value_dictionary, scope_predicate, enum value, or sample value hit, use "
    "that exact field/operator/raw value rather than asking for clarification. "
    "When packet.resolution_task.kind is resolve_value_binding, resolve only "
    "the listed unresolved_value_bindings using field-scoped candidates or "
    "clarify; do not reinterpret the whole question as free-form SQL. "
    "If the same value term is backed by both a display/name field and a "
    "category/status/type/tier/segment field, prefer the category/status/type/"
    "tier/segment field for adjectival or class-like filters, and prefer the "
    "display/name field only when the user is naming a specific record. Use "
    "value_kind=value_dictionary when the value came from a field-scoped "
    "value_dictionary hit, and value_kind=enum_value when it came from an "
    "unambiguous enum_value hit. Metric catalog hits and metric formula hits "
    "are bounded evidence for conditional_rate and aggregate projections; do not invent "
    "metric formulas beyond those hits. When an aggregate metric catalog hit "
    "sets distinct=true, set projection distinct=true and use COUNT only. "
    "Source vocabulary hits may seed fields "
    "and entities, but hits marked requires_clarification must not be routed. "
    "For lifecycle words such as resolved, open, "
    "cancelled, active, or inactive, prefer a backed status/value filter over "
    "a date-existence interpretation unless the user explicitly asks about "
    "dates or missing dates. Do not infer a hidden lifecycle/status filter "
    "from a metric field name alone; aggregate explicit measures over the rows "
    "selected by the user's filters, and only clarify about status population "
    "when the user actually used a lifecycle/status modifier. Omit order_by "
    "unless the user asks for top, bottom, highest, lowest, sorted, ascending, "
    "descending, rank, latest, or earliest results. In joins, from_field and "
    "to_field must be bare field names because from_entity and to_entity are "
    "separate fields. Do not route ambiguous physical shard-family members; "
    "ask for clarification instead. Do not route sensitive tables or fields "
    "such as passwords, secrets, tokens, sessions, or credential material. Use "
    "a positive limit for row-list results; SemSQL will still cap row lists "
    "locally. All schema fields are required: use empty strings for "
    "unused field/alias/operator text, null for unused values, false for "
    "projection distinct when not requested, and null for unused scale."
)
PATHWAY_PACKET_POLICIES = {
    "current_permissive",
    "frame_only",
    "bounded_stage3",
    "bound_plan",
}

SENSITIVE_TABLE_TOKENS = {
    "api",
    "auth",
    "credential",
    "key",
    "login",
    "password",
    "secret",
    "session",
    "token",
}
SENSITIVE_FIELD_TOKENS = SENSITIVE_TABLE_TOKENS | {
    "encrypted",
    "hash",
    "remember",
}
GENERIC_SCHEMA_MATCH_TOKENS = {
    "a",
    "account",
    "accounts",
    "an",
    "and",
    "are",
    "average",
    "by",
    "campaign",
    "campaigns",
    "code",
    "email",
    "emails",
    "external",
    "for",
    "has",
    "have",
    "id",
    "is",
    "mail",
    "name",
    "of",
    "on",
    "or",
    "organization",
    "organizations",
    "plan",
    "plans",
    "recipient",
    "recipients",
    "status",
    "state",
    "the",
    "total",
    "type",
    "synced",
    "verified",
    "with",
}


def build_schema_card(
    graph_path: Path,
    *,
    include_samples: bool = False,
    max_entities: int = 80,
    max_fields_per_entity: int = 24,
    max_relationships: int = 120,
    max_sample_values: int = 5,
    max_value_dictionary_terms: int = 12,
) -> JsonObject:
    """Build a compact, safe schema summary from a `.semsql` graph."""
    rows = _read_graph_rows(graph_path)
    entities = rows["entities"]
    fields = rows["fields"]
    relationships = rows["relationships"]
    metric_definitions = _metric_definition_summaries(rows.get("metric_definitions", []))
    sample_value_counts = rows["sample_values"]
    sample_values = sample_value_counts if include_samples else {}
    value_dictionary_by_field = _scope_predicates_by_field(
        rows["vocabulary"],
        {f"{field['entity']}.{field['field']}" for field in fields},
        max_value_dictionary_terms,
    )
    fields_by_entity: dict[str, list[JsonObject]] = defaultdict(list)
    for field in fields:
        fields_by_entity[str(field["entity"])].append(field)

    physical_table_families = _detect_physical_table_families(entities)
    table_activity_hints = _table_activity_hints(
        entities,
        fields,
        relationships,
        sample_value_counts,
        value_dictionary_by_field,
        physical_table_families,
    )
    _attach_table_activity_hints_to_physical_families(
        physical_table_families,
        table_activity_hints,
    )
    shard_families = _legacy_shard_family_cards(physical_table_families)
    shard_entity_names = {
        member
        for family in shard_families
        for member in family["member_entities"]
        if isinstance(member, str)
    }

    entity_cards = []
    for entity in sorted(entities, key=lambda row: str(row["canonical_name"]))[:max_entities]:
        name = str(entity["canonical_name"])
        entity_fields = sorted(fields_by_entity.get(name, []), key=lambda row: str(row["field"]))
        entity_cards.append(
            _schema_card_entity_summary(
                entity,
                entity_fields,
                sample_values,
                value_dictionary_by_field,
                max_fields_per_entity=max_fields_per_entity,
                max_sample_values=max_sample_values,
                max_value_dictionary_terms=max_value_dictionary_terms,
                shard_entity_names=shard_entity_names,
                priority_field_refs=set(),
                table_activity_hints=table_activity_hints,
            )
        )

    relationship_cards = [
        _schema_card_relationship_summary(row)
        for row in relationships[:max_relationships]
    ]
    ambiguous_families = [
        family
        for family in shard_families
        if len(family.get("member_entities", [])) > 1
    ]
    return {
        "schema_version": 1,
        "source": "semsql_schema_card",
        "graph": str(graph_path),
        "summary": {
            "entity_count": len(entities),
            "field_count": len(fields),
            "relationship_count": len(relationships),
            "metric_definition_count": len(metric_definitions),
            "sample_values_included": include_samples,
            "physical_table_family_count": len(physical_table_families),
            "table_activity_hint_count": len(table_activity_hints),
            "shard_family_count": len(shard_families),
            "ambiguous_physical_family_count": len(ambiguous_families),
            "value_dictionary_count": sum(
                len(entries) for entries in value_dictionary_by_field.values()
            ),
            "sensitive_entity_count": sum(
                1
                for entity in entity_cards
                if bool(entity.get("sensitive"))
            ),
            "entities_truncated": max(0, len(entities) - len(entity_cards)),
            "relationships_truncated": max(0, len(relationships) - len(relationship_cards)),
        },
        "entities": entity_cards,
        "relationships": relationship_cards,
        "metric_definitions": metric_definitions,
        "physical_table_families": physical_table_families,
        "shard_families": shard_families,
        "safety": {
            "samples_policy": (
                "included_non_redacted_graph_samples"
                if include_samples
                else "omitted_by_default"
            ),
            "llm_may_not_execute_sql": True,
            "llm_sql_must_be_revalidated": True,
            "ambiguous_physical_tables_fail_closed": True,
            "table_activity_hint_policy": (
                "graph_metadata_counts_only_not_row_counts"
            ),
            "value_dictionary_policy": (
                "field_scoped_scope_predicate_vocabulary_only"
            ),
        },
    }


def build_rejected_query_packet(
    graph_path: Path,
    question: str,
    *,
    route_reason: str = "manual_rejected",
    query_frame: JsonObject | None = None,
    include_samples: bool = False,
) -> JsonObject:
    """Build a compact packet for LLM-assisted resolution of a rejected query."""
    schema_card = build_schema_card(graph_path, include_samples=include_samples)
    graph_rows = _read_graph_rows(graph_path)
    candidates = _local_candidates(
        question,
        schema_card,
        graph_rows=graph_rows,
        include_samples=include_samples,
    )
    packet = {
        "schema_version": 1,
        "source": "semsql_rejected_query_packet",
        "question": question,
        "route_reason": route_reason,
        "schema_card": schema_card,
        "local_candidates": candidates,
        "query_frame": query_frame,
        "allowed_resolution_contract": {
            "llm_output": "resolution_proposal_json",
            "must_not_emit_final_sql": True,
            "must_reference_schema_card_entities_and_fields": True,
            "value_filters_should_use_schema_card_value_dictionary_samples_or_enum_hits": True,
            "must_ask_clarifying_questions_on_ambiguity": True,
            "must_clarify_ambiguous_physical_table_families": True,
            "must_clarify_ambiguous_physical_shard_families": True,
            "must_not_route_sensitive_schema": True,
            "row_list_routes_are_locally_capped": True,
            "semsql_must_validate_before_execution": True,
        },
    }
    resolution_task = _ambiguous_value_resolution_task(
        question,
        query_frame,
        graph_rows,
    )
    if resolution_task is not None:
        packet["resolution_task"] = resolution_task
    seed_entities, seed_fields = _provider_packet_seed_refs(
        packet,
        include_entity_hits=False,
        include_field_hits=False,
    )
    question_seed_entities, question_seed_fields = _question_schema_seed_refs(
        question,
        graph_rows,
    )
    scope_seed_entities, scope_seed_fields = _scope_path_seed_refs(candidates)
    date_window_seed_entities, date_window_seed_fields = _date_window_seed_refs(
        question,
        graph_rows,
        question_seed_entities,
    )
    seed_entities.update(question_seed_entities)
    seed_fields.update(question_seed_fields)
    seed_entities.update(scope_seed_entities)
    seed_fields.update(scope_seed_fields)
    seed_entities.update(date_window_seed_entities)
    seed_fields.update(date_window_seed_fields)
    _enrich_schema_card_with_seed_fields(
        schema_card,
        graph_rows,
        seed_fields,
        seed_entities=seed_entities,
        include_samples=include_samples,
        max_sample_values=5,
        max_value_dictionary_terms=12,
    )
    return packet


def _ambiguous_value_resolution_task(
    question: str,
    query_frame: JsonObject | None,
    graph_rows: JsonObject,
) -> JsonObject | None:
    if _query_frame_reject_reason(query_frame) != "ambiguous_unscoped_value_field":
        return None
    predicates = _query_frame_bound_predicates(query_frame)
    if not predicates:
        return None
    field_by_ref = _graph_field_by_ref(graph_rows)
    value_index = _graph_field_value_evidence_index(graph_rows)
    relationships = [
        relationship
        for relationship in graph_rows.get("relationships", [])
        if isinstance(relationship, dict)
    ]
    context_entities = _query_frame_context_entities(query_frame, predicates)
    unresolved: list[JsonObject] = []
    for predicate in predicates:
        if not isinstance(predicate, dict):
            continue
        operator = _normalize_operator(
            str(predicate.get("operator") or "="),
            predicate.get("value"),
        )
        if operator not in {"=", "IN"}:
            continue
        raw_value = predicate.get("value")
        value_key = _normalize_value(raw_value)
        if raw_value is None or not value_key:
            continue
        selected_field = str(predicate.get("field") or "")
        if selected_field not in field_by_ref:
            selected_field = ""
        evidence_by_field = value_index.get(value_key, {})
        if not evidence_by_field:
            continue
        candidate_fields = _ambiguous_value_candidate_fields(
            question,
            selected_field,
            raw_value,
            evidence_by_field,
            field_by_ref,
            relationships,
            context_entities,
        )
        if len(candidate_fields) < 2:
            continue
        unresolved.append(
            {
                "value": raw_value,
                "normalized_value": value_key,
                "operator": operator,
                "selected_field": selected_field or None,
                "candidate_fields": candidate_fields[:12],
            }
        )
    if not unresolved:
        return None
    return {
        "kind": "resolve_value_binding",
        "reason": "ambiguous_unscoped_value_field",
        "instructions": (
            "Resolve only the listed value-to-field bindings. Use the exact "
            "field-scoped candidates and relationships; if the question does "
            "not disambiguate a value, ask a clarification instead of guessing."
        ),
        "unresolved_value_bindings": unresolved,
    }


def _query_frame_reject_reason(query_frame: JsonObject | None) -> str:
    if not isinstance(query_frame, dict):
        return ""
    bound_plan = query_frame.get("bound_query_plan")
    if isinstance(bound_plan, dict):
        return str(bound_plan.get("reject_reason") or "")
    return ""


def _query_frame_bound_predicates(query_frame: JsonObject | None) -> list[Any]:
    if not isinstance(query_frame, dict):
        return []
    bound_plan = query_frame.get("bound_query_plan")
    if not isinstance(bound_plan, dict):
        return []
    predicates = bound_plan.get("predicates")
    return predicates if isinstance(predicates, list) else []


def _query_frame_context_entities(
    query_frame: JsonObject | None,
    predicates: list[Any],
) -> set[str]:
    entities: set[str] = set()
    if isinstance(query_frame, dict):
        bound_plan = query_frame.get("bound_query_plan")
        if isinstance(bound_plan, dict):
            base_entity = str(bound_plan.get("base_entity") or "")
            if base_entity:
                entities.add(base_entity)
            for join in _as_list(bound_plan.get("joins")):
                if not isinstance(join, dict):
                    continue
                for key in ("from_entity", "to_entity"):
                    entity = str(join.get(key) or "")
                    if entity:
                        entities.add(entity)
            for projection in _as_list(bound_plan.get("projections")):
                if isinstance(projection, dict):
                    field = str(projection.get("field") or "")
                    projection_entity = _field_ref_entity(field)
                    if projection_entity:
                        entities.add(projection_entity)
        runtime_frame = query_frame.get("runtime_query_frame")
        if isinstance(runtime_frame, dict):
            for entity in _as_list(runtime_frame.get("required_entities")):
                if isinstance(entity, str) and entity:
                    entities.add(entity)
    for predicate in predicates:
        if isinstance(predicate, dict):
            predicate_entity = _field_ref_entity(str(predicate.get("field") or ""))
            if predicate_entity:
                entities.add(predicate_entity)
    return entities


def _graph_field_by_ref(graph_rows: JsonObject) -> dict[str, JsonObject]:
    return {
        f"{field['entity']}.{field['field']}": field
        for field in graph_rows.get("fields", [])
        if isinstance(field, dict) and field.get("entity") and field.get("field")
    }


def _graph_field_value_evidence_index(
    graph_rows: JsonObject,
) -> dict[str, dict[str, list[JsonObject]]]:
    field_names = {
        f"{field['entity']}.{field['field']}"
        for field in graph_rows.get("fields", [])
        if isinstance(field, dict) and field.get("entity") and field.get("field")
    }
    out: defaultdict[str, defaultdict[str, list[JsonObject]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for field_ref, examples in graph_rows.get("sample_values", {}).items():
        if not isinstance(field_ref, str) or field_ref not in field_names:
            continue
        for sample in _as_list(examples):
            raw_value = _sample_candidate_text(sample)
            if raw_value is None or not _sample_value_is_safe_candidate(raw_value):
                continue
            _add_value_binding_evidence(
                out,
                field_ref,
                raw_value,
                {
                    "source": "sample_value",
                    "raw_value": raw_value,
                    "operator": "=",
                },
            )
    value_dictionary_by_field = _scope_predicates_by_field(
        graph_rows.get("vocabulary", []),
        field_names,
        max_per_field=200,
    )
    for field_ref, entries in value_dictionary_by_field.items():
        for entry in entries:
            raw_value = entry.get("raw_value")
            if raw_value is None:
                continue
            _add_value_binding_evidence(
                out,
                field_ref,
                raw_value,
                {
                    "source": "scope_predicate_vocabulary",
                    "term": entry.get("term"),
                    "raw_value": raw_value,
                    "operator": entry.get("operator") or "=",
                    "scope": entry.get("scope"),
                    "confidence": entry.get("confidence"),
                },
            )
    return {
        value: dict(fields)
        for value, fields in out.items()
        if value
    }


def _add_value_binding_evidence(
    out: defaultdict[str, defaultdict[str, list[JsonObject]]],
    field_ref: str,
    raw_value: Any,
    evidence: JsonObject,
) -> None:
    value_key = _normalize_value(raw_value)
    if not value_key:
        return
    existing_keys = {
        (
            str(item.get("source") or ""),
            str(item.get("term") or ""),
            str(item.get("raw_value") or ""),
        )
        for item in out[value_key][field_ref]
    }
    key = (
        str(evidence.get("source") or ""),
        str(evidence.get("term") or ""),
        str(evidence.get("raw_value") or ""),
    )
    if key not in existing_keys:
        out[value_key][field_ref].append(evidence)


def _ambiguous_value_candidate_fields(
    question: str,
    selected_field: str,
    raw_value: Any,
    evidence_by_field: dict[str, list[JsonObject]],
    field_by_ref: dict[str, JsonObject],
    relationships: list[JsonObject],
    context_entities: set[str],
) -> list[JsonObject]:
    question_tokens = _tokens(question)
    selected_entity = _field_ref_entity(selected_field) if selected_field else None
    candidates: list[JsonObject] = []
    for field_ref, evidence in evidence_by_field.items():
        field = field_by_ref.get(field_ref)
        if field is None:
            continue
        entity = _field_ref_entity(field_ref)
        if not entity:
            continue
        if not _ambiguous_value_candidate_entity_allowed(
            entity,
            selected_entity,
            context_entities,
            relationships,
        ):
            continue
        role = _field_role(field)
        field_type = _field_type_kind(str(field.get("type") or ""))
        if field_type in {"date", "datetime", "number", "boolean"}:
            continue
        entity_tokens = _tokens(entity)
        field_tokens = _tokens(
            f"{field.get('field') or ''} {field.get('db_column') or ''} "
            f"{field.get('display_label') or ''}"
        )
        evidence_sources = sorted(
            {
                str(item.get("source") or "atlas_value")
                for item in evidence
                if isinstance(item, dict)
            }
        )
        relationship_path = _ambiguous_value_relationship_path(
            entity,
            selected_entity,
            context_entities,
            relationships,
        )
        candidates.append(
            {
                "field": field_ref,
                "entity": entity,
                "role": role,
                "type": field.get("type") or "unknown",
                "display_label": field.get("display_label"),
                "db_column": field.get("db_column"),
                "value": raw_value,
                "operator": "=",
                "selected_by_runtime": field_ref == selected_field,
                "matched_question_tokens": sorted(
                    question_tokens & (field_tokens | entity_tokens)
                ),
                "matched_field_tokens": sorted(question_tokens & field_tokens),
                "matched_entity_tokens": sorted(question_tokens & entity_tokens),
                "evidence_sources": evidence_sources,
                "relationship_path": relationship_path,
            }
        )
    candidates.sort(
        key=lambda candidate: (
            not bool(candidate.get("selected_by_runtime")),
            -len(candidate.get("matched_question_tokens", [])),
            len(candidate.get("relationship_path", [])),
            str(candidate.get("field") or ""),
        )
    )
    return candidates


def _ambiguous_value_candidate_entity_allowed(
    entity: str,
    selected_entity: str | None,
    context_entities: set[str],
    relationships: list[JsonObject],
) -> bool:
    if entity in context_entities:
        return True
    if selected_entity and _relationship_path({selected_entity}, entity, relationships) is not None:
        return True
    return any(
        _relationship_path({context_entity}, entity, relationships) is not None
        for context_entity in context_entities
    )


def _ambiguous_value_relationship_path(
    entity: str,
    selected_entity: str | None,
    context_entities: set[str],
    relationships: list[JsonObject],
) -> list[JsonObject]:
    if selected_entity and selected_entity != entity:
        path = _relationship_path({selected_entity}, entity, relationships)
        if path is not None:
            return [_relationship_card_from_join_step(step) for step in path]
    if entity in context_entities:
        return []
    anchors = [selected_entity] if selected_entity else []
    anchors.extend(sorted(context_entities))
    for anchor in anchors:
        if not anchor or anchor == entity:
            continue
        path = _relationship_path({anchor}, entity, relationships)
        if path is not None:
            return [_relationship_card_from_join_step(step) for step in path]
    return []


def _enrich_schema_card_with_seed_fields(
    schema_card: JsonObject,
    graph_rows: JsonObject,
    seed_fields: set[str],
    *,
    seed_entities: set[str] | None = None,
    include_samples: bool,
    max_fields_per_entity: int = 24,
    max_sample_values: int,
    max_value_dictionary_terms: int,
) -> None:
    """Keep candidate/runtime fields in the packet authority despite caps."""
    requested_entities = set(seed_entities or set())
    requested_fields = set(seed_fields)
    for field_ref in seed_fields:
        entity = _field_ref_entity(field_ref)
        if entity:
            requested_entities.add(entity)
    if not requested_entities and not requested_fields:
        return
    entities = schema_card.get("entities")
    if not isinstance(entities, list):
        return
    entity_cards = {
        str(entity.get("name") or ""): entity
        for entity in entities
        if isinstance(entity, dict)
    }
    graph_fields = {
        f"{field['entity']}.{field['field']}": field
        for field in graph_rows.get("fields", [])
        if isinstance(field, dict)
    }
    graph_entities = {
        str(entity.get("canonical_name") or ""): entity
        for entity in graph_rows.get("entities", [])
        if isinstance(entity, dict)
    }
    fields_by_entity: dict[str, list[JsonObject]] = defaultdict(list)
    for field in graph_rows.get("fields", []):
        if isinstance(field, dict):
            fields_by_entity[str(field.get("entity") or "")].append(field)
    value_dictionary_by_field = _scope_predicates_by_field(
        graph_rows.get("vocabulary", []),
        set(graph_fields),
        max_value_dictionary_terms,
    )
    sample_value_counts = graph_rows.get("sample_values", {})
    sample_values = sample_value_counts if include_samples else {}
    physical_table_families = _schema_card_physical_families(schema_card)
    table_activity_hints = _table_activity_hints(
        [
            entity
            for entity in graph_rows.get("entities", [])
            if isinstance(entity, dict)
        ],
        [
            field
            for field in graph_rows.get("fields", [])
            if isinstance(field, dict)
        ],
        [
            relationship
            for relationship in graph_rows.get("relationships", [])
            if isinstance(relationship, dict)
        ],
        sample_value_counts if isinstance(sample_value_counts, dict) else {},
        value_dictionary_by_field,
        physical_table_families,
    )

    relationship_cards = schema_card.setdefault("relationships", [])
    relationship_added = 0
    if isinstance(relationship_cards, list):
        relationship_added = _enrich_schema_card_relationships(
            relationship_cards,
            graph_rows.get("relationships", []),
            requested_entities,
            requested_fields,
        )

    shard_entity_names = _schema_card_physical_family_members(schema_card)
    entity_added = 0
    for entity_name in sorted(requested_entities):
        if entity_name in entity_cards:
            continue
        graph_entity = graph_entities.get(entity_name)
        if graph_entity is None:
            continue
        entity_fields = sorted(
            fields_by_entity.get(entity_name, []),
            key=lambda row: str(row.get("field") or ""),
        )
        entity_card = _schema_card_entity_summary(
            graph_entity,
            entity_fields,
            sample_values,
            value_dictionary_by_field,
            max_fields_per_entity=max_fields_per_entity,
            max_sample_values=max_sample_values,
            max_value_dictionary_terms=max_value_dictionary_terms,
            shard_entity_names=shard_entity_names,
            priority_field_refs=requested_fields,
            table_activity_hints=table_activity_hints,
        )
        entities.append(entity_card)
        entity_cards[entity_name] = entity_card
        entity_added += 1

    added = 0
    for field_ref in sorted(requested_fields):
        graph_field = graph_fields.get(field_ref)
        if graph_field is None:
            continue
        entity_name = str(graph_field.get("entity") or "")
        existing_entity_card = entity_cards.get(entity_name)
        if existing_entity_card is None:
            continue
        fields = existing_entity_card.setdefault("fields", [])
        if not isinstance(fields, list):
            continue
        field_name = str(graph_field.get("field") or "")
        if any(
            isinstance(existing, dict)
            and str(existing.get("name") or existing.get("field") or "") == field_name
            for existing in fields
        ):
            continue
        fields.append(
            _field_summary(
                graph_field,
                sample_values,
                max_sample_values,
                value_dictionary_by_field,
                max_value_dictionary_terms,
            )
        )
        added += 1
        field_count = int(existing_entity_card.get("field_count") or len(fields))
        existing_entity_card["truncated_fields"] = max(0, field_count - len(fields))
    if added:
        summary = schema_card.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["seed_field_enrichment_count"] = (
                int(summary.get("seed_field_enrichment_count") or 0) + added
            )
    if entity_added or relationship_added:
        summary = schema_card.setdefault("summary", {})
        if isinstance(summary, dict):
            summary["seed_entity_enrichment_count"] = (
                int(summary.get("seed_entity_enrichment_count") or 0) + entity_added
            )
            summary["seed_relationship_enrichment_count"] = (
                int(summary.get("seed_relationship_enrichment_count") or 0)
                + relationship_added
            )
            entity_count = int(summary.get("entity_count") or len(entities))
            relationship_count = int(
                summary.get("relationship_count") or len(relationship_cards)
            )
            summary["entities_truncated"] = max(0, entity_count - len(entities))
            summary["relationships_truncated"] = max(
                0,
                relationship_count - len(relationship_cards),
            )


def _enrich_schema_card_relationships(
    relationship_cards: list[Any],
    graph_relationships: list[Any],
    requested_entities: set[str],
    requested_fields: set[str],
) -> int:
    if not requested_entities and not requested_fields:
        return 0
    rows = [row for row in graph_relationships if isinstance(row, dict)]
    existing = {
        (
            str(relationship.get("from") or ""),
            str(relationship.get("to") or ""),
        )
        for relationship in relationship_cards
        if isinstance(relationship, dict)
    }
    queued: list[JsonObject] = []

    def queue_relationship(row: JsonObject) -> None:
        card = _schema_card_relationship_summary(row)
        for ref in (str(card["from"]), str(card["to"])):
            requested_fields.add(ref)
            entity = _field_ref_entity(ref)
            if entity:
                requested_entities.add(entity)
        key = (
            str(card.get("from") or ""),
            str(card.get("to") or ""),
        )
        if key in existing:
            return
        existing.add(key)
        queued.append(card)

    for row in rows:
        from_ref = f"{row.get('from_entity')}.{row.get('from_field')}"
        to_ref = f"{row.get('to_entity')}.{row.get('to_field')}"
        from_entity = str(row.get("from_entity") or "")
        to_entity = str(row.get("to_entity") or "")
        if (
            from_ref in requested_fields
            or to_ref in requested_fields
            or (
                from_entity in requested_entities
                and to_entity in requested_entities
            )
        ):
            queue_relationship(row)

    seed_entities = sorted(requested_entities)
    if len(seed_entities) >= 2:
        connected = {seed_entities[0]}
        for target in seed_entities[1:]:
            path = _relationship_path(connected, target, rows)
            if path is None:
                continue
            for step in path:
                queue_relationship(
                    {
                        "from_entity": step["from_entity"],
                        "from_field": step["from_field"],
                        "to_entity": step["to_entity"],
                        "to_field": step["to_field"],
                        "kind": step.get("kind", "relationship_path"),
                    }
                )
                connected.add(str(step["from_entity"]))
                connected.add(str(step["to_entity"]))

    relationship_cards.extend(queued)
    return len(queued)


def build_pathway_rejected_query_packets(
    report_json: Path,
    out_dir: Path,
    *,
    policy: str = "bound_plan",
    include_samples: bool = False,
    max_cases: int | None = None,
) -> JsonObject:
    """Write LLM-resolution packets for fail-closed route rows in a pathway report."""
    if policy not in PATHWAY_PACKET_POLICIES:
        raise ValueError(f"unsupported pathway packet policy: {policy}")
    report = json.loads(report_json.read_text(encoding="utf-8"))
    report_out_dir = _resolve_report_path(report_json, str(report.get("out_dir") or ""))
    suites_by_name = {
        str(suite.get("suite") or ""): suite
        for suite in report.get("suites", [])
        if isinstance(suite, dict)
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    packets: list[JsonObject] = []
    for case in report.get("cases", []):
        if not isinstance(case, dict):
            continue
        if case.get("disposition") != "route":
            continue
        policy_payload = case.get("policies", {}).get(policy, {})
        if not isinstance(policy_payload, dict) or policy_payload.get("bucket") != "fail_closed":
            continue
        suite_name = str(case.get("suite") or "")
        suite = suites_by_name.get(suite_name)
        if suite is None:
            continue
        db_id = str(suite.get("db_id") or "")
        graph = report_out_dir / "graphs" / f"{db_id}.semsql"
        if not graph.exists():
            raise FileNotFoundError(f"missing graph for suite `{suite_name}`: {graph}")
        query_frame = _read_optional_json_path(report_json, case.get("query_frame_path"))
        route_reason = _pathway_case_route_reason(case, policy)
        packet = build_rejected_query_packet(
            graph,
            str(case.get("question") or ""),
            route_reason=route_reason,
            query_frame=query_frame,
            include_samples=include_samples,
        )
        packet["pathway_case"] = {
            "benchmark": report.get("benchmark"),
            "suite": suite_name,
            "case_id": case.get("id"),
            "family": case.get("family"),
            "difficulty": case.get("difficulty"),
            "policy": policy,
            "bucket": policy_payload.get("bucket"),
            "query_frame_path": case.get("query_frame_path"),
            "graph": str(graph),
        }
        filename = _safe_packet_filename(suite_name, str(case.get("id") or "case"))
        packet_path = out_dir / filename
        packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
        packets.append(
            {
                "suite": suite_name,
                "case_id": case.get("id"),
                "family": case.get("family"),
                "question": case.get("question"),
                "route_reason": route_reason,
                "packet_path": str(packet_path),
            }
        )
        if max_cases is not None and len(packets) >= max_cases:
            break
    summary = {
        "schema_version": 1,
        "source": "semsql_pathway_rejected_query_packets",
        "report_json": str(report_json),
        "out_dir": str(out_dir),
        "policy": policy,
        "include_samples": include_samples,
        "packet_count": len(packets),
        "packets": packets,
    }
    (out_dir / "index.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def render_schema_card_markdown(card: JsonObject) -> str:
    summary = card["summary"]
    lines = [
        "# SemSQL SchemaCard",
        "",
        f"- graph: `{card['graph']}`",
        f"- entities: `{summary['entity_count']}`",
        f"- fields: `{summary['field_count']}`",
        f"- relationships: `{summary['relationship_count']}`",
        f"- physical table families: `{summary.get('physical_table_family_count', summary['shard_family_count'])}`",
        f"- ambiguous physical families: `{summary['ambiguous_physical_family_count']}`",
        f"- table activity hints: `{summary.get('table_activity_hint_count', 0)}`",
        f"- value dictionary terms: `{summary.get('value_dictionary_count', 0)}`",
        f"- sample values: `{card['safety']['samples_policy']}`",
        "",
        "## Entities",
        "",
        "| Entity | Table | Fields | Activity | Display | Date | Status | Sensitive |",
        "|---|---|---:|---|---|---|---|---|",
    ]
    for entity in card["entities"]:
        activity_hint = entity.get("table_activity_hint", {})
        activity = (
            f"{activity_hint.get('evidence_level', 'none')}:"
            f"{activity_hint.get('evidence_score', 0)}"
        )
        lines.append(
            f"| `{entity['name']}` | `{entity['db_table']}` | `{entity['field_count']}` | "
            f"`{activity}` | "
            f"`{', '.join(entity['display_fields']) or '-'}` | "
            f"`{', '.join(entity['date_fields']) or '-'}` | "
            f"`{', '.join(entity['status_fields']) or '-'}` | "
            f"`{entity['sensitive']}` |"
        )
    physical_families = card.get("physical_table_families") or card.get("shard_families", [])
    if physical_families:
        lines.extend(["", "## Physical Table Families", ""])
        for family in physical_families:
            if "members" in family:
                members = [
                    str(member.get("entity") or "")
                    for member in _as_list(family.get("members"))
                    if isinstance(member, dict)
                ]
                base = str(family.get("base_table") or "")
            else:
                members = [
                    str(member)
                    for member in _as_list(family.get("member_entities"))
                    if member
                ]
                base = str(family.get("base") or "")
            lines.append(
                f"- `{base}` via `{family['anchor']}`: "
                f"`{', '.join(members)}`"
            )
    return "\n".join(lines) + "\n"


def render_pathway_packet_index_markdown(summary: JsonObject) -> str:
    lines = [
        "# Pathway LLM-Resolution Packets",
        "",
        f"- report: `{summary['report_json']}`",
        f"- policy: `{summary['policy']}`",
        f"- packets: `{summary['packet_count']}`",
        f"- samples included: `{summary['include_samples']}`",
        "",
        "| Suite | Case | Family | Route Reason | Packet |",
        "|---|---|---|---|---|",
    ]
    for packet in summary["packets"]:
        lines.append(
            f"| `{packet['suite']}` | `{packet['case_id']}` | "
            f"`{packet['family']}` | `{packet['route_reason']}` | "
            f"`{packet['packet_path']}` |"
        )
    return "\n".join(lines) + "\n"


def render_resolution_proposal_batch(
    packet_dir: Path,
    *,
    proposal_dir: Path | None = None,
    out_dir: Path | None = None,
    dialect: str = "sqlite",
) -> JsonObject:
    """Render every `*.packet.json` with a matching `*.proposal.json`."""
    resolved_proposal_dir = proposal_dir or packet_dir
    resolved_out_dir = out_dir or packet_dir
    resolved_out_dir.mkdir(parents=True, exist_ok=True)
    cases: list[JsonObject] = []
    for packet_path in sorted(packet_dir.glob("*.packet.json")):
        stem = _packet_file_stem(packet_path)
        proposal_path = resolved_proposal_dir / f"{stem}.proposal.json"
        render_path = resolved_out_dir / f"{stem}.render.json"
        proposal: JsonObject | None = None
        case: JsonObject = {
            "stem": stem,
            "packet_path": str(packet_path),
            "proposal_path": str(proposal_path),
            "render_path": str(render_path),
            "valid": False,
            "issue_count": 0,
            "missing_proposal": not proposal_path.exists(),
        }
        if not proposal_path.exists():
            cases.append(case)
            continue
        try:
            packet_payload = _read_json_payload(packet_path)
            proposal_payload = _read_json_payload(proposal_path)
            packet = packet_payload.get("packet", packet_payload)
            proposal = proposal_payload.get("proposal", proposal_payload)
            result = render_resolution_proposal(packet, proposal, dialect=dialect)
        except Exception as exc:
            result = {
                "schema_version": 1,
                "source": "semsql_resolution_plan_renderer",
                "valid": False,
                "dialect": dialect,
                "question": None,
                "proposal_action": None,
                "validation": None,
                "sql": None,
                "query_frame_candidate": None,
                "issues": [
                    {
                        "level": "error",
                        "code": "render_exception",
                        "message": str(exc),
                    }
                ],
            }
        render_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        case.update(
            {
                "valid": bool(result.get("valid")),
                "issue_count": len(_as_list(result.get("issues"))),
                "question": result.get("question"),
                **_render_case_contract_diagnostics(result, proposal),
            }
        )
        cases.append(case)
    summary = {
        "schema_version": 1,
        "source": "semsql_resolution_batch_renderer",
        "packet_dir": str(packet_dir),
        "proposal_dir": str(resolved_proposal_dir),
        "out_dir": str(resolved_out_dir),
        "dialect": dialect,
        "packet_count": len(cases),
        "missing_proposal_count": sum(1 for case in cases if case["missing_proposal"]),
        "valid_count": sum(1 for case in cases if case["valid"]),
        "invalid_count": sum(
            1
            for case in cases
            if not case["valid"] and not case["missing_proposal"]
        ),
        "cases": cases,
    }
    summary.update(_batch_contract_summary(cases))
    return summary


def _render_case_contract_diagnostics(
    result: JsonObject,
    proposal: JsonObject | None,
) -> JsonObject:
    issues = [
        issue
        for issue in _as_list(result.get("issues"))
        if isinstance(issue, dict)
    ]
    issue_codes = [
        str(issue.get("code") or "")
        for issue in issues
        if issue.get("code")
    ]
    result_shape = result.get("result_shape")
    result_shape_kind = (
        str(result_shape.get("kind") or "")
        if isinstance(result_shape, dict)
        else ""
    )
    declared_shape = (
        str(proposal.get("result_shape") or "")
        if isinstance(proposal, dict)
        else ""
    )
    valid = bool(result.get("valid"))
    if "result_shape_mismatch" in issue_codes:
        shape_contract = "mismatch"
    elif not valid:
        shape_contract = "not_rendered"
    elif not declared_shape:
        shape_contract = "missing_declared"
    elif declared_shape == result_shape_kind:
        shape_contract = "matched"
    else:
        shape_contract = "mismatch"
    return {
        "declared_result_shape": declared_shape,
        "result_shape_kind": result_shape_kind,
        "shape_contract": shape_contract,
        "issue_codes": issue_codes,
    }


def _batch_contract_summary(cases: list[JsonObject]) -> JsonObject:
    shape_contract_counts: dict[str, int] = {}
    result_shape_counts: dict[str, int] = {}
    for case in cases:
        contract = str(case.get("shape_contract") or "")
        if contract:
            shape_contract_counts[contract] = shape_contract_counts.get(contract, 0) + 1
        kind = str(case.get("result_shape_kind") or "")
        if kind:
            result_shape_counts[kind] = result_shape_counts.get(kind, 0) + 1
    return {
        "shape_contract_counts": dict(sorted(shape_contract_counts.items())),
        "result_shape_counts": dict(sorted(result_shape_counts.items())),
        "shape_match_count": shape_contract_counts.get("matched", 0),
        "shape_mismatch_count": shape_contract_counts.get("mismatch", 0),
        "shape_missing_declared_count": shape_contract_counts.get(
            "missing_declared",
            0,
        ),
        "shape_not_rendered_count": shape_contract_counts.get("not_rendered", 0),
    }


def render_resolution_batch_markdown(summary: JsonObject) -> str:
    lines = [
        "# LLM-Resolution Render Batch",
        "",
        f"- packet dir: `{summary['packet_dir']}`",
        f"- proposal dir: `{summary['proposal_dir']}`",
        f"- output dir: `{summary['out_dir']}`",
        f"- dialect: `{summary['dialect']}`",
        f"- packets: `{summary['packet_count']}`",
        f"- valid: `{summary['valid_count']}`",
        f"- invalid: `{summary['invalid_count']}`",
        f"- missing proposals: `{summary['missing_proposal_count']}`",
        f"- shape matches: `{summary['shape_match_count']}`",
        f"- shape mismatches: `{summary['shape_mismatch_count']}`",
        f"- missing declared shape: `{summary['shape_missing_declared_count']}`",
        "",
        "| Case | Valid | Issues | Shape | Shape Contract | Missing Proposal | Render |",
        "|---|---:|---:|---|---|---:|---|",
    ]
    for case in summary["cases"]:
        lines.append(
            f"| `{case['stem']}` | `{case['valid']}` | "
            f"`{case['issue_count']}` | `{case.get('result_shape_kind') or '-'}` | "
            f"`{case.get('shape_contract') or '-'}` | "
            f"`{case['missing_proposal']}` | "
            f"`{case['render_path']}` |"
        )
    return "\n".join(lines) + "\n"


def resolve_resolution_proposal_batch(
    packet_dir: Path,
    *,
    provider: ResolutionProvider,
    provider_name: str,
    proposal_dir: Path | None = None,
    out_dir: Path | None = None,
    provider_out_dir: Path | None = None,
    dialect: str = "sqlite",
    max_cases: int | None = None,
    overwrite: bool = False,
) -> JsonObject:
    """Call an opt-in provider for packets, then locally validate/render plans."""
    resolved_proposal_dir = proposal_dir or packet_dir
    resolved_out_dir = out_dir or packet_dir
    resolved_provider_out_dir = provider_out_dir or packet_dir
    resolved_proposal_dir.mkdir(parents=True, exist_ok=True)
    resolved_out_dir.mkdir(parents=True, exist_ok=True)
    resolved_provider_out_dir.mkdir(parents=True, exist_ok=True)
    cases: list[JsonObject] = []
    packet_paths = sorted(packet_dir.glob("*.packet.json"))
    if max_cases is not None:
        packet_paths = packet_paths[:max_cases]
    for packet_path in packet_paths:
        stem = _packet_file_stem(packet_path)
        proposal_path = resolved_proposal_dir / f"{stem}.proposal.json"
        provider_path = resolved_provider_out_dir / f"{stem}.{provider_name}.json"
        render_path = resolved_out_dir / f"{stem}.render.json"
        proposal: JsonObject | None = None
        case: JsonObject = {
            "stem": stem,
            "packet_path": str(packet_path),
            "proposal_path": str(proposal_path),
            "provider_path": str(provider_path),
            "render_path": str(render_path),
            "provider_called": False,
            "used_existing_proposal": proposal_path.exists() and not overwrite,
            "missing_proposal": False,
            "provider_error": None,
            "valid": False,
            "issue_count": 0,
        }
        try:
            packet_payload = _read_json_payload(packet_path)
            packet = packet_payload.get("packet", packet_payload)
            if proposal_path.exists() and not overwrite:
                proposal_payload = _read_json_payload(proposal_path)
                proposal = _provider_result_proposal(proposal_payload)
            else:
                case["provider_called"] = True
                provider_result = provider(packet)
                provider_path.write_text(
                    json.dumps(provider_result, indent=2) + "\n",
                    encoding="utf-8",
                )
                proposal = _provider_result_proposal(provider_result)
                proposal_path.write_text(
                    json.dumps(proposal, indent=2) + "\n",
                    encoding="utf-8",
                )
            result = render_resolution_proposal(packet, proposal, dialect=dialect)
        except Exception as exc:
            case["provider_error"] = str(exc)
            result = {
                "schema_version": 1,
                "source": "semsql_resolution_plan_renderer",
                "valid": False,
                "dialect": dialect,
                "question": None,
                "proposal_action": None,
                "validation": None,
                "sql": None,
                "query_frame_candidate": None,
                "issues": [
                    {
                        "level": "error",
                        "code": "provider_resolution_error",
                        "message": str(exc),
                    }
                ],
            }
        render_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        case.update(
            {
                "valid": bool(result.get("valid")),
                "issue_count": len(_as_list(result.get("issues"))),
                "missing_proposal": not proposal_path.exists(),
                "question": result.get("question"),
                **_render_case_contract_diagnostics(result, proposal),
            }
        )
        cases.append(case)
    summary = {
        "schema_version": 1,
        "source": "semsql_resolution_provider_batch",
        "provider": provider_name,
        "packet_dir": str(packet_dir),
        "proposal_dir": str(resolved_proposal_dir),
        "provider_out_dir": str(resolved_provider_out_dir),
        "out_dir": str(resolved_out_dir),
        "dialect": dialect,
        "overwrite": overwrite,
        "packet_count": len(cases),
        "provider_call_count": sum(1 for case in cases if case["provider_called"]),
        "used_existing_proposal_count": sum(
            1 for case in cases if case["used_existing_proposal"]
        ),
        "missing_proposal_count": sum(1 for case in cases if case["missing_proposal"]),
        "provider_error_count": sum(1 for case in cases if case["provider_error"]),
        "valid_count": sum(1 for case in cases if case["valid"]),
        "invalid_count": sum(
            1
            for case in cases
            if not case["valid"] and not case["missing_proposal"]
        ),
        "cases": cases,
    }
    summary.update(_batch_contract_summary(cases))
    return summary


def render_resolution_provider_batch_markdown(summary: JsonObject) -> str:
    lines = [
        "# LLM-Resolution Provider Batch",
        "",
        f"- provider: `{summary['provider']}`",
        f"- packet dir: `{summary['packet_dir']}`",
        f"- proposal dir: `{summary['proposal_dir']}`",
        f"- provider output dir: `{summary['provider_out_dir']}`",
        f"- render output dir: `{summary['out_dir']}`",
        f"- dialect: `{summary['dialect']}`",
        f"- packets: `{summary['packet_count']}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- existing proposals reused: `{summary['used_existing_proposal_count']}`",
        f"- valid: `{summary['valid_count']}`",
        f"- invalid: `{summary['invalid_count']}`",
        f"- missing proposals: `{summary['missing_proposal_count']}`",
        f"- provider errors: `{summary['provider_error_count']}`",
        f"- shape matches: `{summary['shape_match_count']}`",
        f"- shape mismatches: `{summary['shape_mismatch_count']}`",
        f"- missing declared shape: `{summary['shape_missing_declared_count']}`",
        "",
        "| Case | Provider Called | Existing Proposal | Valid | Issues | Shape | Shape Contract | Provider Error | Render |",
        "|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for case in summary["cases"]:
        provider_error = str(case.get("provider_error") or "")
        lines.append(
            f"| `{case['stem']}` | `{case['provider_called']}` | "
            f"`{case['used_existing_proposal']}` | `{case['valid']}` | "
            f"`{case['issue_count']}` | `{case.get('result_shape_kind') or '-'}` | "
            f"`{case.get('shape_contract') or '-'}` | "
            f"`{provider_error or '-'}` | "
            f"`{case['render_path']}` |"
        )
    return "\n".join(lines) + "\n"


def evaluate_resolution_safety_expectations(
    summary: JsonObject,
    expectations: JsonObject,
    *,
    summary_path: Path | None = None,
    expectations_path: Path | None = None,
) -> JsonObject:
    """Check a provider/render/fallback summary against safety expectations.

    This is deliberately not another "all cases must route" gate. Production
    safety batches need to prove that some cases route and other cases stay
    blocked, rejected, or clarified before SQL.
    """
    expected_cases = _resolution_expectation_cases(expectations)
    observed_cases = {
        str(case.get("stem") or ""): case
        for case in _as_list(summary.get("cases"))
        if isinstance(case, dict) and case.get("stem")
    }
    cases: list[JsonObject] = []
    for stem, expectation in expected_cases.items():
        observed = observed_cases.get(stem)
        if observed is None:
            cases.append(
                {
                    "stem": stem,
                    "expected_outcome": _normalize_expected_outcome(
                        expectation.get("outcome")
                    ),
                    "observed_outcome": "missing",
                    "pass": False,
                    "failures": ["missing_case"],
                }
            )
            continue
        diagnostic = _observed_resolution_case(summary, observed)
        failures = _resolution_expectation_failures(expectation, diagnostic)
        cases.append(
            {
                "stem": stem,
                "expected_outcome": _normalize_expected_outcome(
                    expectation.get("outcome")
                ),
                "observed_outcome": diagnostic["outcome"],
                "pass": not failures,
                "failures": failures,
                "selected_source": diagnostic.get("selected_source"),
                "result_shape": diagnostic.get("result_shape"),
                "issue_codes": diagnostic.get("issue_codes", []),
                "direct_llm_sql": diagnostic.get("direct_llm_sql", False),
                "provider_called": diagnostic.get("provider_called"),
                "render_path": diagnostic.get("render_path"),
            }
        )
    unexpected_cases = sorted(set(observed_cases) - set(expected_cases))
    failed_cases = [case for case in cases if not case["pass"]]
    return {
        "schema_version": 1,
        "source": "semsql_resolution_safety_expectation_gate",
        "summary_source": summary.get("source"),
        "summary_path": str(summary_path) if summary_path is not None else None,
        "expectations_path": (
            str(expectations_path) if expectations_path is not None else None
        ),
        "pass": not failed_cases and not unexpected_cases,
        "expected_count": len(expected_cases),
        "observed_count": len(observed_cases),
        "passed_count": len(cases) - len(failed_cases),
        "failed_count": len(failed_cases),
        "unexpected_count": len(unexpected_cases),
        "unexpected_cases": unexpected_cases,
        "outcome_counts": _resolution_outcome_counts(cases),
        "cases": cases,
    }


def render_resolution_safety_expectations_markdown(report: JsonObject) -> str:
    lines = [
        "# LLM-Resolution Safety Gate",
        "",
        f"- summary: `{report.get('summary_path') or '-'}`",
        f"- expectations: `{report.get('expectations_path') or '-'}`",
        f"- pass: `{report['pass']}`",
        f"- expected cases: `{report['expected_count']}`",
        f"- observed cases: `{report['observed_count']}`",
        f"- passed: `{report['passed_count']}`",
        f"- failed: `{report['failed_count']}`",
        f"- unexpected: `{report['unexpected_count']}`",
        "",
        "| Case | Expected | Observed | Pass | Source | Shape | Issues | Failures |",
        "|---|---|---|---:|---|---|---|---|",
    ]
    for case in _as_list(report.get("cases")):
        if not isinstance(case, dict):
            continue
        issues = ", ".join(str(item) for item in _as_list(case.get("issue_codes")))
        failures = ", ".join(str(item) for item in _as_list(case.get("failures")))
        lines.append(
            f"| `{case['stem']}` | `{case['expected_outcome']}` | "
            f"`{case['observed_outcome']}` | `{case['pass']}` | "
            f"`{case.get('selected_source') or '-'}` | "
            f"`{case.get('result_shape') or '-'}` | "
            f"`{issues or '-'}` | `{failures or '-'}` |"
        )
    unexpected = _as_list(report.get("unexpected_cases"))
    if unexpected:
        lines.extend(["", "## Unexpected Cases", ""])
        for stem in unexpected:
            lines.append(f"- `{stem}`")
    return "\n".join(lines) + "\n"


def _resolution_expectation_cases(expectations: JsonObject) -> dict[str, JsonObject]:
    raw_cases = expectations.get("cases", {})
    cases: dict[str, JsonObject] = {}
    if isinstance(raw_cases, dict):
        for stem, raw_expectation in raw_cases.items():
            if isinstance(raw_expectation, str):
                cases[str(stem)] = {"outcome": raw_expectation}
            elif isinstance(raw_expectation, dict):
                cases[str(stem)] = dict(raw_expectation)
            else:
                cases[str(stem)] = {"outcome": "block"}
        return cases
    for item in _as_list(raw_cases):
        if not isinstance(item, dict) or not item.get("stem"):
            continue
        cases[str(item["stem"])] = dict(item)
    return cases


def _observed_resolution_case(summary: JsonObject, case: JsonObject) -> JsonObject:
    render = _load_case_render(case)
    validation = render.get("validation") if isinstance(render, dict) else None
    validation_valid = (
        bool(validation.get("valid")) if isinstance(validation, dict) else None
    )
    issue_codes = _case_issue_codes(case, render)
    sql_present = bool(render.get("sql")) if isinstance(render, dict) else False
    sql_present = sql_present or bool(case.get("selected_sql_present"))
    selected_source = case.get("selected_source")
    valid = bool(case.get("valid")) or bool(render.get("valid"))
    status = str(case.get("status") or "")
    proposal_action = str(render.get("proposal_action") or "")
    effective_action = str(render.get("effective_action") or "")
    if (
        sql_present
        or valid
        or status == "selected"
        or (effective_action == "route" and bool(render.get("sql")))
    ):
        outcome = "route"
    elif (
        proposal_action in {"clarify", "reject"}
        and effective_action == proposal_action
        and validation_valid is True
    ):
        outcome = proposal_action
    else:
        outcome = "block"
    result_shape = case.get("result_shape_kind")
    if not result_shape and isinstance(render.get("result_shape"), dict):
        result_shape = render["result_shape"].get("kind")
    render_path = case.get("render_path")
    if not render_path and case.get("out_dir"):
        render_path = str(Path(str(case["out_dir"])) / "render.json")
    return {
        "outcome": outcome,
        "result_shape": result_shape,
        "issue_codes": issue_codes,
        "selected_source": selected_source,
        "direct_llm_sql": bool(case.get("used_direct_llm_sql") or case.get("direct_llm_sql")),
        "provider_called": case.get("provider_called")
        if "provider_called" in case
        else case.get("provider_call_count"),
        "render_path": render_path,
    }


def _load_case_render(case: JsonObject) -> JsonObject:
    raw_path = case.get("render_path")
    if not raw_path and case.get("out_dir"):
        raw_path = str(Path(str(case["out_dir"])) / "render.json")
    if not raw_path:
        return {}
    path = Path(str(raw_path))
    if not path.exists():
        return {}
    try:
        payload = _read_json_payload(path)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _case_issue_codes(case: JsonObject, render: JsonObject) -> list[str]:
    codes = [
        str(code)
        for code in _as_list(case.get("issue_codes"))
        if str(code)
    ]
    if codes:
        return codes
    return [
        str(issue.get("code"))
        for issue in _as_list(render.get("issues"))
        if isinstance(issue, dict) and issue.get("code")
    ]


def _resolution_expectation_failures(
    expectation: JsonObject,
    observed: JsonObject,
) -> list[str]:
    failures: list[str] = []
    expected_outcome = _normalize_expected_outcome(expectation.get("outcome"))
    observed_outcome = str(observed.get("outcome") or "")
    if expected_outcome != observed_outcome:
        failures.append(f"outcome:{observed_outcome}!={expected_outcome}")
    expected_shape = str(expectation.get("result_shape") or "")
    if expected_shape and str(observed.get("result_shape") or "") != expected_shape:
        failures.append(
            f"result_shape:{observed.get('result_shape') or '-'}!={expected_shape}"
        )
    expected_source = str(expectation.get("selected_source") or "")
    if expected_source and str(observed.get("selected_source") or "") != expected_source:
        failures.append(
            f"selected_source:{observed.get('selected_source') or '-'}"
            f"!={expected_source}"
        )
    required_codes = {
        str(code)
        for code in _as_list(expectation.get("required_issue_codes"))
        if str(code)
    }
    observed_codes = {
        str(code)
        for code in _as_list(observed.get("issue_codes"))
        if str(code)
    }
    missing_codes = sorted(required_codes - observed_codes)
    if missing_codes:
        failures.append(f"missing_issue_codes:{','.join(missing_codes)}")
    allowed_codes = {
        str(code)
        for code in _as_list(expectation.get("allowed_issue_codes"))
        if str(code)
    }
    if allowed_codes:
        extra_codes = sorted(observed_codes - allowed_codes)
        if extra_codes:
            failures.append(f"unexpected_issue_codes:{','.join(extra_codes)}")
    if expectation.get("forbid_direct_llm_sql", True) and observed.get(
        "direct_llm_sql"
    ):
        failures.append("direct_llm_sql")
    if "provider_called" in expectation:
        expected_called = bool(expectation["provider_called"])
        observed_called = bool(observed.get("provider_called"))
        if observed_called != expected_called:
            failures.append(f"provider_called:{observed_called}!={expected_called}")
    return failures


def _normalize_expected_outcome(value: object) -> str:
    raw = str(value or "block").strip().lower().replace("-", "_")
    aliases = {
        "selected": "route",
        "sql": "route",
        "valid": "route",
        "invalid": "block",
        "unresolved": "block",
        "fail_closed": "block",
        "failclosed": "block",
        "no_sql": "block",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in {"route", "clarify", "reject", "block"}:
        return "block"
    return normalized


def _resolution_outcome_counts(cases: list[JsonObject]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        outcome = str(case.get("observed_outcome") or "")
        if not outcome:
            continue
        counts[outcome] = counts.get(outcome, 0) + 1
    return dict(sorted(counts.items()))


def build_openai_resolution_request_batch(
    packet_dir: Path,
    out_dir: Path,
    *,
    model: str = DEFAULT_OPENAI_MODEL,
    max_cases: int | None = None,
) -> JsonObject:
    """Write OpenAI request previews for packets without calling the provider."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cases: list[JsonObject] = []
    packet_paths = sorted(packet_dir.glob("*.packet.json"))
    if max_cases is not None:
        packet_paths = packet_paths[:max_cases]
    for packet_path in packet_paths:
        stem = _packet_file_stem(packet_path)
        request_path = out_dir / f"{stem}.openai-request.json"
        packet_payload = _read_json_payload(packet_path)
        packet = packet_payload.get("packet", packet_payload)
        request = build_openai_resolution_request(packet, model=model)
        request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
        cases.append(
            {
                "stem": stem,
                "packet_path": str(packet_path),
                "request_path": str(request_path),
                "question": packet.get("question"),
                "model": model,
                "strict": bool(request.get("text", {}).get("format", {}).get("strict")),
            }
        )
    return {
        "schema_version": 1,
        "source": "openai_resolution_request_batch",
        "packet_dir": str(packet_dir),
        "out_dir": str(out_dir),
        "model": model,
        "packet_count": len(cases),
        "request_count": len(cases),
        "provider_call_count": 0,
        "cases": cases,
    }


def render_openai_request_batch_markdown(summary: JsonObject) -> str:
    lines = [
        "# OpenAI Resolution Request Batch",
        "",
        f"- packet dir: `{summary['packet_dir']}`",
        f"- output dir: `{summary['out_dir']}`",
        f"- model: `{summary['model']}`",
        f"- packets: `{summary['packet_count']}`",
        f"- requests: `{summary['request_count']}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        "",
        "| Case | Strict | Request |",
        "|---|---:|---|",
    ]
    for case in summary["cases"]:
        lines.append(
            f"| `{case['stem']}` | `{case['strict']}` | `{case['request_path']}` |"
        )
    return "\n".join(lines) + "\n"


def _packet_file_stem(packet_path: Path) -> str:
    name = packet_path.name
    suffix = ".packet.json"
    return name[: -len(suffix)] if name.endswith(suffix) else packet_path.stem


def _read_json_payload(path: Path) -> JsonObject:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _provider_result_proposal(payload: JsonObject) -> JsonObject:
    proposal = payload.get("proposal", payload)
    if not isinstance(proposal, dict):
        raise ValueError("provider result must contain a proposal object")
    return proposal


def _resolve_report_path(report_json: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return report_json.parent / path


def _read_optional_json_path(report_json: Path, raw_path: Any) -> JsonObject | None:
    if not raw_path:
        return None
    path = _resolve_report_path(report_json, str(raw_path))
    if not path.exists():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _pathway_case_route_reason(case: JsonObject, policy: str) -> str:
    features = case.get("features", {})
    if isinstance(features, dict):
        policy_reject = features.get(f"{policy}_reject_reason")
        if policy_reject:
            return str(policy_reject)
        bound_reject = features.get("bound_plan_reject_reason")
        if bound_reject:
            return str(bound_reject)
        if features.get("runtime_routed") and not features.get("frame_promoted"):
            runtime_reason = features.get("runtime_route_reason")
            if runtime_reason:
                return f"runtime_route_not_promoted:{runtime_reason}"
            return "runtime_route_not_promoted"
        if not features.get("runtime_routed", True):
            runtime_reason = features.get("runtime_route_reason")
            if runtime_reason:
                return str(runtime_reason)
            return "runtime_not_routed"
    detail = str(case.get("error_detail") or "").strip()
    if detail:
        detail_lower = detail.lower()
        if "queryframe fail-closed before" in detail_lower:
            match = re.search(
                r"queryframe fail-closed before ([^:.]+)",
                detail,
                flags=re.IGNORECASE,
            )
            if match is not None:
                return f"queryframe_fail_closed_before_{_slug(match.group(1))}"
            return "queryframe_fail_closed_before_model_fallback"
        if "model stages (stage 1/2/3) not available" in detail_lower:
            return "model_stages_unavailable_after_fail_closed"
        return detail.splitlines()[-1][:160]
    if isinstance(features, dict):
        runtime_reason = features.get("runtime_route_reason")
        if runtime_reason:
            return str(runtime_reason)
    return f"pathway_{policy}_fail_closed"


def _safe_packet_filename(suite: str, case_id: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{suite}-{case_id}").strip("-")
    return f"{stem or 'case'}.packet.json"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "unknown"


def resolution_json_schema() -> JsonObject:
    """JSON schema for an LLM-produced resolution proposal."""
    string_array = {"type": "array", "items": {"type": "string"}}
    scalar_value = {"type": ["string", "number", "boolean", "null"]}
    filter_value = {
        "anyOf": [
            scalar_value,
            {"type": "array", "items": scalar_value},
        ]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "action",
            "confidence",
            "intent",
            "distinct",
            "result_shape",
            "target_entities",
            "projections",
            "filters",
            "joins",
            "group_by",
            "order_by",
            "limit",
            "ambiguity_questions",
            "evidence",
            "safety_notes",
        ],
        "properties": {
            "schema_version": {"type": "integer"},
            "action": {"type": "string", "enum": ["route", "clarify", "reject"]},
            "confidence": {"type": "number"},
            "intent": {"type": "string"},
            "distinct": {"type": "boolean"},
            "result_shape": {
                "type": "string",
                "enum": [
                    "",
                    "scalar_metric",
                    "table",
                    "categorical_chart",
                    "time_series_chart",
                    "multi_series_chart",
                ],
            },
            "target_entities": string_array,
            "projections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "kind",
                        "field",
                        "aggregate",
                        "distinct",
                        "alias",
                        "numerator_field",
                        "numerator_operator",
                        "numerator_value",
                        "numerator_value_kind",
                        "denominator_field",
                        "scale",
                        "rationale",
                    ],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "field",
                                "count",
                                "aggregate",
                                "conditional_rate",
                                "all",
                            ],
                        },
                        "field": {"type": "string"},
                        "aggregate": {"type": "string"},
                        "distinct": {"type": "boolean"},
                        "alias": {"type": "string"},
                        "numerator_field": {"type": "string"},
                        "numerator_operator": {"type": "string"},
                        "numerator_value": filter_value,
                        "numerator_value_kind": {"type": "string"},
                        "denominator_field": {"type": "string"},
                        "scale": {"type": ["number", "null"]},
                        "rationale": {"type": "string"},
                    },
                },
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "operator", "value", "value_kind", "rationale"],
                    "properties": {
                        "field": {"type": "string"},
                        "operator": {"type": "string"},
                        "value": filter_value,
                        "value_kind": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                },
            },
            "joins": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from_entity", "from_field", "to_entity", "to_field", "rationale"],
                    "properties": {
                        "from_entity": {"type": "string"},
                        "from_field": {"type": "string"},
                        "to_entity": {"type": "string"},
                        "to_field": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                },
            },
            "group_by": string_array,
            "order_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "aggregate", "alias", "direction", "rationale"],
                    "properties": {
                        "field": {"type": "string"},
                        "aggregate": {"type": "string"},
                        "alias": {"type": "string"},
                        "direction": {"type": "string", "enum": ["ASC", "DESC"]},
                        "rationale": {"type": "string"},
                    },
                },
            },
            "limit": {"type": "integer"},
            "ambiguity_questions": string_array,
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["claim", "graph_refs"],
                    "properties": {
                        "claim": {"type": "string"},
                        "graph_refs": string_array,
                    },
                },
            },
            "safety_notes": string_array,
        },
    }


def build_openai_resolution_request(
    packet: JsonObject,
    *,
    model: str = DEFAULT_OPENAI_MODEL,
    max_output_tokens: int = 1800,
) -> JsonObject:
    """Build the OpenAI Responses API request body without sending it."""
    provider_packet = compact_resolution_packet_for_provider(packet)
    return {
        "model": model,
        "instructions": RESOLUTION_ASSISTANT_INSTRUCTIONS,
        "input": json.dumps(provider_packet, sort_keys=True),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "semsql_resolution_proposal",
                "strict": True,
                "schema": resolution_json_schema(),
            }
        },
        "max_output_tokens": max_output_tokens,
    }


def build_openai_chat_resolution_request(
    packet: JsonObject,
    *,
    model: str,
    max_output_tokens: int = 1800,
    strict_json_schema: bool = False,
) -> JsonObject:
    """Build an OpenAI-compatible Chat Completions request body.

    This is for providers that implement `/chat/completions` but not the
    OpenAI Responses API. Local validation still owns the executable boundary.
    """
    provider_packet = compact_resolution_packet_for_provider(packet)
    response_format: JsonObject
    if strict_json_schema:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "semsql_resolution_proposal",
                "strict": True,
                "schema": resolution_json_schema(),
            },
        }
    else:
        response_format = {"type": "json_object"}
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    RESOLUTION_ASSISTANT_INSTRUCTIONS
                    + " Return a single JSON object matching the SemSQL "
                    "resolution proposal schema."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(provider_packet, sort_keys=True),
            },
        ],
        "response_format": response_format,
        "max_tokens": max_output_tokens,
    }


def compact_resolution_packet_for_provider(
    packet: JsonObject,
    *,
    max_entities: int = 24,
    max_fields_per_entity: int = 18,
    max_relationships: int = 80,
) -> JsonObject:
    """Build a smaller provider packet while keeping full-packet validation.

    Product-emitted packets may include a full runtime SemanticAtlas in
    `query_frame`; that is useful diagnostic evidence but too large for model
    context on real app schemas. The provider only needs the compact SchemaCard
    and local candidate hits. The original packet remains the authority for
    validating the returned proposal.
    """
    compact = copy.deepcopy(packet)
    query_frame = compact.get("query_frame")
    if isinstance(query_frame, dict):
        query_frame.pop("semantic_atlas", None)
    schema_card = compact.get("schema_card")
    if not isinstance(schema_card, dict):
        return compact
    entities = schema_card.get("entities")
    relationships = schema_card.get("relationships")
    if not isinstance(entities, list) or not isinstance(relationships, list):
        return compact

    seed_entities, seed_fields = _provider_packet_seed_refs(
        compact,
        include_entity_hits=False,
        include_field_hits=False,
    )
    question = str(compact.get("question") or "")
    question_entities, question_fields = _schema_card_question_seed_refs(
        question,
        schema_card,
    )
    seed_entities.update(question_entities)
    seed_fields.update(question_fields)
    if not seed_entities and not seed_fields:
        return compact
    entity_by_name = {
        str(entity.get("name") or ""): entity
        for entity in entities
        if isinstance(entity, dict)
    }
    included_entities = set(seed_entities)
    candidate_relationships: list[JsonObject] = []
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        left_entity = _field_ref_entity(str(relationship.get("from") or ""))
        right_entity = _field_ref_entity(str(relationship.get("to") or ""))
        if not left_entity or not right_entity:
            continue
        if left_entity in included_entities or right_entity in included_entities:
            if len(included_entities) < max_entities:
                included_entities.add(left_entity)
                included_entities.add(right_entity)
            candidate_relationships.append(relationship)
    for field_ref in seed_fields:
        entity = _field_ref_entity(field_ref)
        if entity:
            included_entities.add(entity)
    for relationship in candidate_relationships:
        if not isinstance(relationship, dict):
            continue
        left = str(relationship.get("from") or "")
        right = str(relationship.get("to") or "")
        left_entity = _field_ref_entity(left)
        right_entity = _field_ref_entity(right)
        if left_entity in included_entities and right_entity in included_entities:
            seed_fields.add(left)
            seed_fields.add(right)
    ordered_entity_names = sorted(
        included_entities,
        key=lambda name: _provider_compact_entity_sort_key(
            name,
            entity_by_name,
            question,
            question_entities,
            seed_fields,
        ),
    )
    ordered_entities = [
        entity_by_name[name]
        for name in ordered_entity_names
        if name in entity_by_name
    ][:max_entities]
    compact_entities = [
        _compact_schema_card_entity(
            entity,
            seed_fields=seed_fields,
            max_fields=max_fields_per_entity,
            question=question,
        )
        for entity in ordered_entities
    ]
    included_entity_names = {
        str(entity.get("name") or "")
        for entity in compact_entities
        if isinstance(entity, dict)
    }
    compact_relationships = []
    seen_relationships: set[tuple[str, str, str]] = set()
    for relationship in candidate_relationships:
        left_entity = _field_ref_entity(str(relationship.get("from") or ""))
        right_entity = _field_ref_entity(str(relationship.get("to") or ""))
        if left_entity not in included_entity_names or right_entity not in included_entity_names:
            continue
        key = (
            str(relationship.get("from") or ""),
            str(relationship.get("to") or ""),
            str(relationship.get("kind") or ""),
        )
        if key in seen_relationships:
            continue
        seen_relationships.add(key)
        compact_relationships.append(relationship)
        if len(compact_relationships) == max_relationships:
            break
    schema_card["entities"] = compact_entities
    schema_card["relationships"] = compact_relationships
    summary = schema_card.setdefault("summary", {})
    if isinstance(summary, dict):
        summary["provider_compacted"] = True
        summary["provider_entity_count"] = len(compact_entities)
        summary["provider_relationship_count"] = len(compact_relationships)
        summary["provider_seed_field_count"] = len(seed_fields)
    return compact


def _provider_compact_entity_sort_key(
    entity_name: str,
    entity_by_name: dict[str, JsonObject],
    question: str,
    question_entities: set[str],
    seed_fields: set[str],
) -> tuple[int, str]:
    entity = entity_by_name.get(entity_name, {})
    question_tokens = _tokens(question)
    labels = [
        str(entity.get("name") or entity_name),
        str(entity.get("db_table") or ""),
        *[str(label) for label in entity.get("labels", []) if label],
    ]
    label_token_sets = [_tokens(label) for label in labels if label]
    score = 0
    if entity_name in question_entities:
        score += 120
    if any(tokens and tokens <= question_tokens for tokens in label_token_sets):
        score += 80
    field_seed_count = sum(
        1 for field_ref in seed_fields if _field_ref_entity(field_ref) == entity_name
    )
    score += min(80, field_seed_count * 10)
    return (-score, entity_name)


def _provider_packet_seed_refs(
    packet: JsonObject,
    *,
    include_entity_hits: bool = True,
    include_field_hits: bool = True,
) -> tuple[set[str], set[str]]:
    local_candidates = packet.get("local_candidates", {})
    seed_entities: set[str] = set()
    seed_fields: set[str] = set()
    if isinstance(local_candidates, dict):
        if include_entity_hits:
            for hit in local_candidates.get("entity_hits", []):
                if isinstance(hit, dict) and isinstance(hit.get("entity"), str):
                    seed_entities.add(hit["entity"])
        local_field_keys = ["value_dictionary_hits", "sample_value_hits", "enum_value_hits"]
        if include_field_hits:
            local_field_keys.append("field_hits")
        for key in local_field_keys:
            for hit in local_candidates.get(key, []):
                if isinstance(hit, dict) and isinstance(hit.get("field"), str):
                    seed_fields.add(hit["field"])
        for hit in local_candidates.get("metric_formula_hits", []):
            if not isinstance(hit, dict):
                continue
            for key in ("numerator_field", "denominator_field"):
                value = hit.get(key)
                if isinstance(value, str) and value:
                    seed_fields.add(value)
        for hit in local_candidates.get("metric_catalog_hits", []):
            if not isinstance(hit, dict):
                continue
            for key in ("numerator_field", "denominator_field", "measure_field"):
                value = hit.get(key)
                if isinstance(value, str) and value:
                    seed_fields.add(value)
            for entity in _as_list(hit.get("required_entities")):
                if isinstance(entity, str) and entity:
                    seed_entities.add(entity)
        for hit in local_candidates.get("source_vocabulary_hits", []):
            if not isinstance(hit, dict):
                continue
            canonical_kind = str(hit.get("canonical_kind") or "")
            canonical_value = str(hit.get("canonical_value") or "")
            if canonical_kind == "field" and canonical_value:
                seed_fields.add(canonical_value)
            elif canonical_kind == "entity" and canonical_value:
                seed_entities.add(canonical_value)
        scope_entities, scope_fields = _scope_path_seed_refs(local_candidates)
        seed_entities.update(scope_entities)
        seed_fields.update(scope_fields)
    query_frame = packet.get("query_frame")
    if isinstance(query_frame, dict):
        runtime_frame = query_frame.get("runtime_query_frame")
        if isinstance(runtime_frame, dict):
            for entity in runtime_frame.get("required_entities", []):
                if isinstance(entity, str):
                    seed_entities.add(entity)
            for predicate in runtime_frame.get("predicates", []):
                if isinstance(predicate, dict) and isinstance(predicate.get("field"), str):
                    seed_fields.add(predicate["field"])
            projection = runtime_frame.get("projection")
            if isinstance(projection, dict):
                for field in projection.get("fields", []):
                    if isinstance(field, str):
                        seed_fields.add(field)
            order_by = runtime_frame.get("order_by")
            if isinstance(order_by, dict) and isinstance(order_by.get("field"), str):
                seed_fields.add(order_by["field"])
    for field_ref in _resolution_task_candidate_field_refs(packet):
        seed_fields.add(field_ref)
    for field_ref in list(seed_fields):
        entity = _field_ref_entity(field_ref)
        if entity:
            seed_entities.add(entity)
    return seed_entities, seed_fields


def _compact_schema_card_entity(
    entity: JsonObject,
    *,
    seed_fields: set[str],
    max_fields: int,
    question: str = "",
) -> JsonObject:
    compact = copy.deepcopy(entity)
    entity_name = str(entity.get("name") or "")
    fields = entity.get("fields")
    if not isinstance(fields, list):
        return compact
    preferred_names: list[str] = []
    for key in (
        "display_fields",
        "status_fields",
        "numeric_fields",
        "date_fields",
        "id_fields",
    ):
        values = entity.get(key)
        if isinstance(values, list):
            preferred_names.extend(str(value) for value in values if isinstance(value, str))
    selected: list[JsonObject] = []
    seen: set[str] = set()
    ordered_fields = sorted(
        [field for field in fields if isinstance(field, dict)],
        key=lambda field: _provider_compact_field_sort_key(
            entity_name,
            field,
            question,
            seed_fields,
        ),
    )
    for require_seed in (True, False):
        for field in ordered_fields:
            field_name = str(field.get("name") or field.get("field") or "")
            canonical = f"{entity_name}.{field_name}" if entity_name and field_name else ""
            if field_name in seen:
                continue
            if require_seed:
                should_select = canonical in seed_fields
            else:
                should_select = field_name in preferred_names
            if should_select:
                selected.append(field)
                seen.add(field_name)
            if len(selected) >= max_fields:
                break
        if len(selected) >= max_fields:
            break
    for field in ordered_fields:
        if len(selected) >= max_fields:
            break
        field_name = str(field.get("name") or field.get("field") or "")
        if field_name and field_name not in seen:
            selected.append(field)
            seen.add(field_name)
    compact["fields"] = selected[:max_fields]
    compact["truncated_fields"] = max(0, len(fields) - len(compact["fields"]))
    return compact


def _provider_compact_field_sort_key(
    entity_name: str,
    field: JsonObject,
    question: str,
    seed_fields: set[str],
) -> tuple[int, str]:
    field_name = str(field.get("name") or field.get("field") or "")
    canonical = f"{entity_name}.{field_name}" if entity_name and field_name else ""
    question_tokens = _tokens(question)
    field_tokens = _tokens(
        f"{field_name} {field.get('db_column') or ''} {field.get('display_label') or ''}"
    )
    matched = question_tokens & field_tokens
    non_generic = matched - GENERIC_SCHEMA_MATCH_TOKENS
    score = 0
    if canonical in seed_fields:
        score += 100
    score += len(matched) * 8
    score += len(non_generic) * 12
    if field_name and _tokens(field_name) <= question_tokens:
        score += 20
    role = str(field.get("role") or "")
    if role == "status" and "status" in question_tokens:
        score += 30
    if role == "display" and (
        "name" in question_tokens or "title" in question_tokens
    ):
        score += 20
    return (-score, field_name)


def _field_ref_entity(field_ref: str) -> str | None:
    entity, sep, _field = field_ref.partition(".")
    if not sep or not entity:
        return None
    return entity


def call_openai_resolution(
    packet: JsonObject,
    *,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: float = 30.0,
) -> JsonObject:
    """Call OpenAI for a structured resolution proposal.

    Uses stdlib HTTP to avoid making OpenAI an installation dependency.
    """
    resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
    if not resolved_key:
        raise RuntimeError("OPENAI_API_KEY is required for --openai")
    resolved_model = model or os.environ.get("SEMSQL_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    request_body = build_openai_resolution_request(packet, model=resolved_model)
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI Responses API failed: HTTP {exc.code}: {detail}") from exc
    response = json.loads(raw)
    text = _extract_response_text(response)
    proposal = json.loads(text)
    validation = validate_resolution_proposal(packet, proposal)
    return {
        "schema_version": 1,
        "source": "openai_responses_api",
        "model": resolved_model,
        "request": _redact_openai_request(request_body),
        "response_id": response.get("id"),
        "proposal": proposal,
        "validation": validation,
    }


def call_openai_chat_compatible_resolution(
    packet: JsonObject,
    *,
    api_key: str,
    base_url: str,
    model: str,
    source: str = "openai_compatible_chat_api",
    timeout_seconds: float = 30.0,
    strict_json_schema: bool = False,
) -> JsonObject:
    """Call an OpenAI-compatible Chat Completions provider.

    Providers in this path only return typed proposals. The proposal is still
    validated locally before any SQL renderer can consume it.
    """
    if not api_key:
        raise RuntimeError("provider API key is required")
    request_body = build_openai_chat_resolution_request(
        packet,
        model=model,
        strict_json_schema=strict_json_schema,
    )
    req = urllib.request.Request(  # noqa: S310
        _chat_completions_url(base_url),
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenAI-compatible chat API failed: HTTP {exc.code}: {detail}"
        ) from exc
    response = json.loads(raw)
    text = _extract_chat_completion_text(response)
    proposal = _loads_provider_json_text(text)
    validation = validate_resolution_proposal(packet, proposal)
    return {
        "schema_version": 1,
        "source": source,
        "model": model,
        "request": _redact_provider_request(request_body),
        "response_id": response.get("id"),
        "proposal": proposal,
        "validation": validation,
    }


def validate_resolution_proposal(packet: JsonObject, proposal: JsonObject) -> JsonObject:
    """Validate an LLM resolution proposal against a rejected-query packet.

    This is intentionally a structural/evidence validator, not a SQL renderer.
    A valid proposal is still only a candidate plan for local SemSQL routing.
    """
    proposal = _canonicalize_proposal_field_refs(packet, proposal)
    issues: list[JsonObject] = []
    allowed_entities = _packet_allowed_entities(packet)
    allowed_fields = _packet_allowed_fields(packet)
    allowed_relationships = _packet_allowed_relationships(packet)
    allowed_value_filters = _packet_allowed_value_filters(packet)
    field_type_map = _packet_field_type_map(packet)
    field_role_map = _packet_field_role_map(packet)
    output_aliases = _proposal_output_aliases(proposal)

    action = str(proposal.get("action") or "")
    if action not in {"route", "clarify", "reject"}:
        _add_validation_issue(
            issues,
            "invalid_action",
            "action must be one of route, clarify, or reject",
            "action",
        )
    result_shape = str(proposal.get("result_shape") or "")
    if result_shape and result_shape not in {
        "scalar_metric",
        "table",
        "categorical_chart",
        "time_series_chart",
        "multi_series_chart",
    }:
        _add_validation_issue(
            issues,
            "invalid_result_shape",
            f"unsupported result_shape `{result_shape}`",
            "result_shape",
        )
    issues.extend(_proposal_route_safety_issues(packet, proposal, action))
    limit_issue = _proposal_limit_issue(proposal)
    if limit_issue is not None:
        issues.append(limit_issue)

    for idx, entity in enumerate(_as_list(proposal.get("target_entities"))):
        entity_name = str(entity)
        if entity_name not in allowed_entities:
            _add_validation_issue(
                issues,
                "unknown_entity",
                f"target entity `{entity_name}` is not in the packet",
                f"target_entities[{idx}]",
            )

    for idx, projection in enumerate(_as_list(proposal.get("projections"))):
        if not isinstance(projection, dict):
            _add_validation_issue(
                issues,
                "invalid_projection",
                "projection must be an object",
                f"projections[{idx}]",
            )
            continue
        kind = str(projection.get("kind") or "")
        field = str(projection.get("field") or "")
        alias_issue = _alias_issue(
            str(projection.get("alias") or ""),
            f"projections[{idx}].alias",
        )
        if alias_issue is not None:
            issues.append(alias_issue)
        if kind in {"field", "aggregate"} and field not in allowed_fields:
            _add_validation_issue(
                issues,
                "unknown_field",
                f"projection field `{field}` is not in the packet",
                f"projections[{idx}].field",
            )
        if kind == "count" and field and field not in allowed_fields:
            _add_validation_issue(
                issues,
                "unknown_field",
                f"count field `{field}` is not in the packet",
                f"projections[{idx}].field",
            )
        if projection.get("distinct") is True:
            if kind != "aggregate":
                _add_validation_issue(
                    issues,
                    "distinct_requires_aggregate",
                    "distinct is only supported on aggregate projections",
                    f"projections[{idx}].distinct",
                )
            elif str(projection.get("aggregate") or "").upper() != "COUNT":
                _add_validation_issue(
                    issues,
                    "unsupported_distinct_aggregate",
                    "distinct aggregate projections require COUNT",
                    f"projections[{idx}].distinct",
                )
        if kind == "conditional_rate":
            issues.extend(
                _conditional_rate_projection_issues(
                    projection,
                    f"projections[{idx}]",
                    allowed_fields,
                    allowed_value_filters,
                    field_type_map,
                    field_role_map,
                )
            )

    for idx, filter_item in enumerate(_as_list(proposal.get("filters"))):
        if not isinstance(filter_item, dict):
            _add_validation_issue(
                issues,
                "invalid_filter",
                "filter must be an object",
                f"filters[{idx}]",
            )
            continue
        field = str(filter_item.get("field") or "")
        if field not in allowed_fields:
            _add_validation_issue(
                issues,
                "unknown_field",
                f"filter field `{field}` is not in the packet",
                f"filters[{idx}].field",
            )
        operator = _normalize_operator(
            str(filter_item.get("operator") or "="),
            filter_item.get("value"),
        )
        value = _normalize_filter_value(operator, filter_item.get("value"))
        field_type = field_type_map.get(field)
        type_issue = _filter_type_issue(
            field,
            operator,
            value,
            field_type,
            f"filters[{idx}]",
        )
        if type_issue is not None:
            issues.append(type_issue)
        evidence_issue = _filter_evidence_issue(
            field,
            operator,
            value,
            str(filter_item.get("value_kind") or ""),
            allowed_value_filters,
            field_type_map.get(field),
            field_role_map.get(field),
            f"filters[{idx}]",
        )
        if evidence_issue is not None:
            issues.append(evidence_issue)

    for idx, join in enumerate(_as_list(proposal.get("joins"))):
        if not isinstance(join, dict):
            _add_validation_issue(
                issues,
                "invalid_join",
                "join must be an object",
                f"joins[{idx}]",
            )
            continue
        from_entity = str(join.get("from_entity") or "")
        to_entity = str(join.get("to_entity") or "")
        from_field = _join_field_name(from_entity, str(join.get("from_field") or ""))
        to_field = _join_field_name(to_entity, str(join.get("to_field") or ""))
        edge = (
            f"{from_entity}.{from_field}",
            f"{to_entity}.{to_field}",
        )
        reverse_edge = (edge[1], edge[0])
        if edge not in allowed_relationships and reverse_edge not in allowed_relationships:
            _add_validation_issue(
                issues,
                "unknown_join",
                f"join `{edge[0]} -> {edge[1]}` is not in packet relationships",
                f"joins[{idx}]",
            )

    for idx, field in enumerate(_as_list(proposal.get("group_by"))):
        field_name = str(field or "")
        if field_name not in allowed_fields:
            _add_validation_issue(
                issues,
                "unknown_field",
                f"group_by field `{field_name}` is not in the packet",
                f"group_by[{idx}]",
            )

    for idx, order in enumerate(_as_list(proposal.get("order_by"))):
        if not isinstance(order, dict):
            _add_validation_issue(
                issues,
                "invalid_order_by",
                "order_by entry must be an object",
                f"order_by[{idx}]",
            )
            continue
        field = str(order.get("field") or "")
        aggregate = str(order.get("aggregate") or "").upper()
        alias = str(order.get("alias") or "")
        alias_issue = _alias_issue(alias, f"order_by[{idx}].alias")
        if alias_issue is not None:
            issues.append(alias_issue)
        elif alias and alias not in output_aliases:
            _add_validation_issue(
                issues,
                "unknown_order_alias",
                f"order alias `{alias}` does not match a projection alias",
                f"order_by[{idx}].alias",
            )
        if field and field not in allowed_fields:
            _add_validation_issue(
                issues,
                "unknown_field",
                f"order field `{field}` is not in the packet",
                f"order_by[{idx}].field",
            )
        aggregate_issue = _aggregate_order_issue(
            aggregate,
            field,
            f"order_by[{idx}]",
            has_alias=bool(alias),
        )
        if aggregate_issue is not None:
            issues.append(aggregate_issue)

    for path, fragment in _proposal_sql_fragments(proposal):
        _add_validation_issue(
            issues,
            "sql_fragment_forbidden",
            f"proposal text appears to contain SQL: `{fragment}`",
            path,
        )

    return {
        "schema_version": 1,
        "source": "semsql_resolution_validator",
        "valid": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "allowed_counts": {
            "entities": len(allowed_entities),
            "fields": len(allowed_fields),
            "relationships": len(allowed_relationships),
            "value_filters": len(allowed_value_filters),
        },
    }


def render_resolution_proposal(
    packet: JsonObject,
    proposal: JsonObject,
    *,
    dialect: str = "sqlite",
    clarification_choice: str | None = None,
) -> JsonObject:
    """Render a validated proposal into a local SemSQL candidate SQL string.

    The LLM still does not provide SQL. This function renders from the typed
    proposal and packet graph evidence, then runs the local SQL validator.
    """
    effective_proposal, choice_issues = _apply_clarification_choice(
        packet,
        proposal,
        clarification_choice,
    )
    if _has_render_errors(choice_issues):
        validation = validate_resolution_proposal(packet, effective_proposal)
        return _render_result(
            packet,
            effective_proposal,
            validation,
            sql=None,
            issues=choice_issues,
            dialect=dialect,
        )
    effective_proposal, promotion_issues = _clarify_auto_route_adjustment(
        packet,
        effective_proposal,
    )
    promotion_issues = [*choice_issues, *promotion_issues]
    effective_proposal = _canonicalize_proposal_field_refs(packet, effective_proposal)
    effective_proposal, value_binding_issues = _value_binding_auto_route_adjustment(
        packet,
        effective_proposal,
    )
    promotion_issues.extend(value_binding_issues)
    validation = validate_resolution_proposal(packet, effective_proposal)
    if not validation["valid"]:
        return _render_result(
            packet,
            effective_proposal,
            validation,
            sql=None,
            issues=[
                {
                    "level": "error",
                    "code": "proposal_validation_failed",
                    "message": "proposal failed structural packet validation",
                }
            ],
            dialect=dialect,
        )
    if effective_proposal.get("action") != "route":
        if not promotion_issues:
            clarification_issues = _clarification_required_issues(packet, effective_proposal)
            if not clarification_issues:
                clarification_issues = [
                    {
                        "level": "error",
                        "code": "proposal_not_route",
                        "message": "only action=route proposals can render SQL",
                    }
                ]
            return _render_result(
                packet,
                effective_proposal,
                validation,
                sql=None,
                issues=clarification_issues,
                dialect=dialect,
            )
    route_reason = (
        "llm_resolution_validated_clarification_choice"
        if any(
            issue.get("code") == "clarification_choice_applied_schema_path"
            for issue in promotion_issues
        )
        else
        "llm_resolution_validated_clarify_auto_promoted"
        if promotion_issues
        else "llm_resolution_validated"
    )

    graph = _packet_graph_rows(packet)
    graph_index = _graph_index(graph)
    issues: list[JsonObject] = [*promotion_issues]
    base_entity = _proposal_base_entity(effective_proposal, graph_index)
    if base_entity is None:
        issues.append(
            {
                "level": "error",
                "code": "missing_base_entity",
                "message": "could not infer a base entity for rendering",
            }
        )
        return _render_result(
            packet,
            effective_proposal,
            validation,
            sql=None,
            issues=issues,
            dialect=dialect,
        )

    required_entities = _proposal_required_entities(effective_proposal, base_entity)
    join_plan = _proposal_join_plan(
        effective_proposal,
        base_entity,
        required_entities,
        graph_index,
        issues,
    )
    if _has_render_errors(issues):
        return _render_result(
            packet,
            effective_proposal,
            validation,
            None,
            issues,
            dialect=dialect,
        )

    select_items = _proposal_select_items(effective_proposal, graph_index, issues, dialect=dialect)
    if _has_render_errors(issues):
        return _render_result(
            packet,
            effective_proposal,
            validation,
            None,
            issues,
            dialect=dialect,
        )

    from_sql = _quote_identifier(graph_index["entities"][base_entity]["db_table"], dialect)
    select_keyword = (
        "SELECT DISTINCT" if _proposal_uses_select_distinct(effective_proposal) else "SELECT"
    )
    sql_parts = [f"{select_keyword} {', '.join(select_items)}", f"FROM {from_sql}"]
    sql_parts.extend(
        _render_join_step(join, graph_index, dialect=dialect)
        for join in join_plan
    )
    where_items = [
        _render_filter(filter_item, graph_index, dialect=dialect)
        for filter_item in _as_list(effective_proposal.get("filters"))
        if isinstance(filter_item, dict)
    ]
    if where_items:
        sql_parts.append(f"WHERE {' AND '.join(where_items)}")
    group_items = [
        _render_field_ref(str(field), graph_index, dialect=dialect)
        for field in _as_list(effective_proposal.get("group_by"))
    ]
    if group_items:
        sql_parts.append(f"GROUP BY {', '.join(group_items)}")
    order_items = [
        _render_order_by(order, graph_index, dialect=dialect)
        for order in _as_list(effective_proposal.get("order_by"))
        if isinstance(order, dict)
        and (order.get("field") or order.get("aggregate") or order.get("alias"))
    ]
    if order_items:
        sql_parts.append(f"ORDER BY {', '.join(order_items)}")
    limit_clause, limit_issue = _proposal_limit_clause(effective_proposal)
    if limit_issue is not None:
        issues.append(limit_issue)
    if limit_clause:
        sql_parts.append(limit_clause)

    sql = " ".join(sql_parts)
    validator_issue = _validate_rendered_sql(sql, dialect)
    if validator_issue is not None:
        issues.append(validator_issue)
    inferred_shape = _infer_resolution_result_shape(effective_proposal, graph_index)
    shape_issue = _proposal_result_shape_issue(effective_proposal, inferred_shape)
    if shape_issue is not None:
        issues.append(shape_issue)

    return _render_result(
        packet,
        effective_proposal,
        validation,
        None if _has_render_errors(issues) else sql,
        issues,
        dialect=dialect,
        result_shape=inferred_shape,
        query_frame_candidate=_query_frame_candidate(
            packet,
            effective_proposal,
            sql,
            base_entity,
            required_entities,
            join_plan,
            route_reason=route_reason,
            result_shape=inferred_shape,
        )
        if not _has_render_errors(issues)
        else None,
    )


def _canonicalize_proposal_field_refs(
    packet: JsonObject,
    proposal: JsonObject,
) -> JsonObject:
    """Qualify bare proposal fields when the target entity makes them unique."""
    allowed_fields = _packet_allowed_fields(packet)
    allowed_entities = _packet_allowed_entities(packet)
    target_entities = [
        str(entity)
        for entity in _as_list(proposal.get("target_entities"))
        if str(entity) in allowed_entities
    ]

    def canonical(field_ref: Any) -> str:
        text = str(field_ref or "")
        if not text or "." in text:
            return text
        target_matches = [
            f"{entity}.{text}"
            for entity in target_entities
            if f"{entity}.{text}" in allowed_fields
        ]
        if len(set(target_matches)) == 1:
            return target_matches[0]
        suffix_matches = sorted(
            field
            for field in allowed_fields
            if field.rsplit(".", 1)[-1] == text
        )
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        return text

    adjusted = copy.deepcopy(proposal)
    for projection in _as_list(adjusted.get("projections")):
        if not isinstance(projection, dict):
            continue
        for key in ("field", "numerator_field", "denominator_field"):
            if projection.get(key):
                projection[key] = canonical(projection[key])
    for filter_item in _as_list(adjusted.get("filters")):
        if isinstance(filter_item, dict) and filter_item.get("field"):
            filter_item["field"] = canonical(filter_item["field"])
    group_by = adjusted.get("group_by")
    if isinstance(group_by, list):
        adjusted["group_by"] = [
            canonical(field) if field else field
            for field in group_by
        ]
    for order in _as_list(adjusted.get("order_by")):
        if isinstance(order, dict) and order.get("field"):
            order["field"] = canonical(order["field"])
    return adjusted


def _value_binding_auto_route_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
) -> tuple[JsonObject, list[JsonObject]]:
    if proposal.get("action") != "route":
        return proposal, []
    bindings = [
        binding
        for binding in _resolution_task_value_bindings(packet)
        if isinstance(binding, dict)
    ]
    if not bindings:
        return proposal, []
    resolved: dict[int, tuple[JsonObject, str]] = {}
    resolved_entities: set[str] = set()
    for idx, binding in enumerate(bindings):
        selected = _select_value_binding_candidate(
            packet,
            binding,
            resolved_entities,
            allow_colocation=False,
        )
        if selected is None:
            continue
        candidate, reason = selected
        resolved[idx] = (candidate, reason)
        entity = str(candidate.get("entity") or "")
        if entity:
            resolved_entities.add(entity)
    for idx, binding in enumerate(bindings):
        if idx in resolved:
            continue
        selected = _select_value_binding_candidate(
            packet,
            binding,
            resolved_entities,
            allow_colocation=True,
        )
        if selected is None:
            continue
        candidate, reason = selected
        resolved[idx] = (candidate, reason)
        entity = str(candidate.get("entity") or "")
        if entity:
            resolved_entities.add(entity)
    if not resolved:
        return proposal, []
    adjusted = json.loads(json.dumps(proposal))
    filters = adjusted.get("filters")
    if not isinstance(filters, list):
        return proposal, []
    issues: list[JsonObject] = []
    changed = False
    for idx, binding in enumerate(bindings):
        if idx not in resolved:
            continue
        candidate, reason = resolved[idx]
        candidate_field = str(candidate.get("field") or "")
        selected_field = str(binding.get("selected_field") or "")
        if not candidate_field:
            continue
        replacements = 0
        for filter_item in filters:
            if not isinstance(filter_item, dict):
                continue
            if not _filter_matches_value_binding(filter_item, binding):
                continue
            current_field = str(filter_item.get("field") or "")
            if current_field and current_field != selected_field:
                candidate_fields = {
                    str(item.get("field") or "")
                    for item in _as_list(binding.get("candidate_fields"))
                    if isinstance(item, dict)
                }
                if current_field not in candidate_fields:
                    continue
            if current_field == candidate_field:
                continue
            filter_item["field"] = candidate_field
            if "value_kind" not in filter_item or not filter_item["value_kind"]:
                filter_item["value_kind"] = _candidate_value_kind(candidate)
            replacements += 1
            changed = True
        if replacements:
            _append_resolution_candidate_relationships(adjusted, candidate)
            _append_resolution_candidate_entity(adjusted, candidate_field)
            issues.append(
                {
                    "level": "warning",
                    "code": "value_binding_auto_resolved",
                    "message": (
                        "packet-backed value-binding evidence replaced an "
                        "ambiguous filter field before local validation"
                    ),
                    "value": binding.get("value"),
                    "from_field": selected_field,
                    "to_field": candidate_field,
                    "reason": reason,
                    "replacements": replacements,
                }
            )
    return (adjusted, issues) if changed else (proposal, [])


def _select_value_binding_candidate(
    packet: JsonObject,
    binding: JsonObject,
    resolved_entities: set[str],
    *,
    allow_colocation: bool,
) -> tuple[JsonObject, str] | None:
    scored: list[tuple[int, str, JsonObject]] = []
    for candidate in _as_list(binding.get("candidate_fields")):
        if not isinstance(candidate, dict):
            continue
        score, reasons = _value_binding_candidate_score(
            str(packet.get("question") or ""),
            binding,
            candidate,
            resolved_entities,
            allow_colocation=allow_colocation,
        )
        if score <= 0:
            continue
        scored.append((score, "+".join(reasons), candidate))
    if not scored:
        return None
    scored.sort(
        key=lambda item: (
            -item[0],
            not bool(item[2].get("selected_by_runtime")),
            str(item[2].get("field") or ""),
        )
    )
    best = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0
    if best[0] <= runner_up_score:
        return None
    return best[2], best[1]


def _value_binding_candidate_score(
    question: str,
    binding: JsonObject,
    candidate: JsonObject,
    resolved_entities: set[str],
    *,
    allow_colocation: bool,
) -> tuple[int, list[str]]:
    matched_tokens = {
        str(token)
        for token in _as_list(candidate.get("matched_field_tokens"))
        if isinstance(token, str)
    }
    non_generic = matched_tokens - GENERIC_SCHEMA_MATCH_TOKENS
    score = len(non_generic) * 8
    reasons: list[str] = []
    if non_generic:
        reasons.append("candidate_token_match")
    context_score = _value_binding_candidate_local_context_score(
        question,
        binding.get("value"),
        candidate,
    )
    if context_score:
        score += context_score
        reasons.append("value_context_match")
    entity = str(candidate.get("entity") or "")
    if allow_colocation and entity and entity in resolved_entities:
        score += 30
        reasons.append("resolved_entity_colocation")
    evidence_sources = {
        str(source)
        for source in _as_list(candidate.get("evidence_sources"))
        if isinstance(source, str)
    }
    if {"sample_value", "scope_predicate_vocabulary"} <= evidence_sources:
        score += 2
    return score, reasons


def _value_binding_candidate_local_context_score(
    question: str,
    raw_value: Any,
    candidate: JsonObject,
) -> int:
    question_tokens = re.findall(r"[a-z0-9]+", question.lower())
    value_tokens = re.findall(r"[a-z0-9]+", str(raw_value).lower())
    if not question_tokens or not value_tokens:
        return 0
    field_ref = str(candidate.get("field") or "")
    field_name = field_ref.split(".", 1)[1] if "." in field_ref else field_ref
    field_tokens = _tokens(
        " ".join(
            [
                field_name,
                str(candidate.get("db_column") or ""),
                str(candidate.get("display_label") or ""),
            ]
        )
    )
    entity_tokens = _tokens(str(candidate.get("entity") or ""))
    if not field_tokens and not entity_tokens:
        return 0
    best = 0
    width = len(value_tokens)
    for idx in range(0, len(question_tokens) - width + 1):
        if question_tokens[idx : idx + width] != value_tokens:
            continue
        before = _tokens(" ".join(question_tokens[max(0, idx - 3) : idx]))
        after = _tokens(" ".join(question_tokens[idx + width : idx + width + 3]))
        window = _tokens(
            " ".join(
                question_tokens[max(0, idx - 4) : min(len(question_tokens), idx + width + 4)]
            )
        )
        score = 0
        if after & field_tokens:
            score += 30 * len(after & field_tokens)
        if before & field_tokens:
            score += 18 * len(before & field_tokens)
        if after & entity_tokens:
            score += 30 * len(after & entity_tokens)
        if window & field_tokens:
            score += 5 * len(window & field_tokens)
        best = max(best, score)
    return best


def _filter_matches_value_binding(
    filter_item: JsonObject,
    binding: JsonObject,
) -> bool:
    operator = _normalize_operator(
        str(filter_item.get("operator") or "="),
        filter_item.get("value"),
    )
    binding_operator = _normalize_operator(
        str(binding.get("operator") or "="),
        binding.get("value"),
    )
    if operator != binding_operator:
        return False
    return _normalize_value(filter_item.get("value")) == _normalize_value(
        binding.get("value")
    )


def _candidate_value_kind(candidate: JsonObject) -> str:
    evidence_sources = {
        str(source)
        for source in _as_list(candidate.get("evidence_sources"))
        if isinstance(source, str)
    }
    if "scope_predicate_vocabulary" in evidence_sources:
        return "value_dictionary"
    if "sample_value" in evidence_sources:
        return "sample_value"
    return "literal"


def _append_resolution_candidate_relationships(
    proposal: JsonObject,
    candidate: JsonObject,
) -> None:
    joins = proposal.setdefault("joins", [])
    if not isinstance(joins, list):
        return
    seen = {_join_edge_refs(join) for join in joins if isinstance(join, dict)}
    for relationship in _as_list(candidate.get("relationship_path")):
        if not isinstance(relationship, dict):
            continue
        join = _join_from_relationship_ref(relationship)
        edge = _join_edge_refs(join)
        reverse_edge = (edge[1], edge[0])
        if edge in seen or reverse_edge in seen:
            continue
        joins.append(join)
        seen.add(edge)


def _append_resolution_candidate_entity(
    proposal: JsonObject,
    candidate_field: str,
) -> None:
    entity = _field_ref_entity(candidate_field)
    if not entity:
        return
    target_entities = proposal.setdefault("target_entities", [])
    if not isinstance(target_entities, list):
        return
    if entity not in target_entities:
        target_entities.append(entity)


def _clarify_auto_route_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
) -> tuple[JsonObject, list[JsonObject]]:
    if proposal.get("action") != "clarify":
        return proposal, []

    issues: list[JsonObject] = []
    adjusted: JsonObject | None = None
    hidden_filter_issue = _clarify_auto_route_issue(packet, proposal)
    if hidden_filter_issue is not None:
        issues.append(hidden_filter_issue)

    date_anchor = _clarify_subject_date_anchor_adjustment(packet, proposal)
    if date_anchor is not None:
        if adjusted is None:
            adjusted = json.loads(json.dumps(proposal))
        adjusted.setdefault("filters", [])
        adjusted["filters"].append(date_anchor["filter"])
        issues.append(date_anchor["issue"])

    metric_catalog = _clarify_metric_catalog_adjustment(packet, adjusted or proposal)
    if metric_catalog is not None:
        if adjusted is None:
            adjusted = json.loads(json.dumps(proposal))
        _apply_metric_catalog_adjustment(adjusted, metric_catalog)
        issues.append(metric_catalog["issue"])

    metric_formula = None
    if metric_catalog is None:
        metric_formula = _clarify_metric_formula_adjustment(packet, adjusted or proposal)
    if metric_formula is not None:
        if adjusted is None:
            adjusted = json.loads(json.dumps(proposal))
        _apply_metric_catalog_adjustment(adjusted, metric_formula)
        issues.append(metric_formula["issue"])

    current_plan = _clarify_current_plan_adjustment(packet, adjusted or proposal)
    if current_plan is not None:
        if adjusted is None:
            adjusted = json.loads(json.dumps(proposal))
        _apply_current_plan_adjustment(adjusted, current_plan)
        issues.append(current_plan["issue"])

    return adjusted or proposal, issues


def _apply_clarification_choice(
    packet: JsonObject,
    proposal: JsonObject,
    choice_id: str | None,
) -> tuple[JsonObject, list[JsonObject]]:
    if choice_id is None or not choice_id.strip():
        return proposal, []
    choice_id = choice_id.strip()
    issues = _clarification_required_issues(packet, proposal)
    for issue in issues:
        if issue.get("code") != "clarification_required_schema_path":
            continue
        for option in _as_list(issue.get("clarification_options")):
            if not isinstance(option, dict) or option.get("id") != choice_id:
                continue
            adjusted = json.loads(json.dumps(proposal))
            _apply_schema_path_choice(adjusted, option, issue)
            return adjusted, [
                {
                    "level": "warning",
                    "code": "clarification_choice_applied_schema_path",
                    "message": (
                        "caller selected a packet-backed schema-path "
                        "clarification option; local renderer will validate "
                        "and render that typed join path"
                    ),
                    "choice_id": choice_id,
                    "label": option.get("label"),
                    "relationships": option.get("relationships", []),
                }
            ]
    return proposal, [
        {
            "level": "error",
            "code": "unknown_clarification_choice",
            "message": f"clarification choice `{choice_id}` is not available",
            "choice_id": choice_id,
        }
    ]


def _apply_schema_path_choice(
    proposal: JsonObject,
    option: JsonObject,
    issue: JsonObject,
) -> None:
    proposal["action"] = "route"
    candidate_relationships = {
        (str(rel.get("from") or ""), str(rel.get("to") or ""))
        for rel in _as_list(issue.get("candidate_relationships"))
        if isinstance(rel, dict)
    }
    retained_joins: list[JsonObject] = []
    for join in _as_list(proposal.get("joins")):
        if not isinstance(join, dict):
            continue
        edge = _join_edge_refs(join)
        reverse_edge = (edge[1], edge[0])
        if edge in candidate_relationships or reverse_edge in candidate_relationships:
            continue
        retained_joins.append(join)
    selected_joins = [
        _join_from_relationship_ref(relationship)
        for relationship in _as_list(option.get("relationships"))
        if isinstance(relationship, dict)
    ]
    proposal["joins"] = [*retained_joins, *selected_joins]
    target_entities = [
        str(entity)
        for entity in _as_list(proposal.get("target_entities"))
        if isinstance(entity, str) and entity
    ]
    for join in selected_joins:
        for entity in (str(join["from_entity"]), str(join["to_entity"])):
            if entity and entity not in target_entities:
                target_entities.append(entity)
    proposal["target_entities"] = target_entities


def _join_edge_refs(join: JsonObject) -> tuple[str, str]:
    from_entity = str(join.get("from_entity") or "")
    to_entity = str(join.get("to_entity") or "")
    from_field = _join_field_name(from_entity, str(join.get("from_field") or ""))
    to_field = _join_field_name(to_entity, str(join.get("to_field") or ""))
    return f"{from_entity}.{from_field}", f"{to_entity}.{to_field}"


def _join_from_relationship_ref(relationship: JsonObject) -> JsonObject:
    from_ref = str(relationship.get("from") or "")
    to_ref = str(relationship.get("to") or "")
    from_entity, from_field = _split_field_ref(from_ref)
    to_entity, to_field = _split_field_ref(to_ref)
    return {
        "from_entity": from_entity,
        "from_field": from_field,
        "to_entity": to_entity,
        "to_field": to_field,
        "rationale": "caller-selected schema-path clarification option",
    }


def _split_field_ref(field_ref: str) -> tuple[str, str]:
    if "." not in field_ref:
        return "", field_ref
    left, right = field_ref.split(".", 1)
    return left, right


def _clarification_required_issues(
    packet: JsonObject,
    proposal: JsonObject,
) -> list[JsonObject]:
    if proposal.get("action") != "clarify":
        return []
    questions = [
        str(question)
        for question in _as_list(proposal.get("ambiguity_questions"))
        if str(question).strip()
    ]
    if not questions:
        return []
    schema_path_issue = _schema_path_clarification_required_issue(
        packet,
        proposal,
        questions,
    )
    if schema_path_issue is not None:
        return [schema_path_issue]
    return []


def _schema_path_clarification_required_issue(
    packet: JsonObject,
    proposal: JsonObject,
    questions: list[str],
) -> JsonObject | None:
    schema_path_questions = [
        question for question in questions if _is_schema_path_clarification(question)
    ]
    if not schema_path_questions:
        return None
    text = "\n".join(schema_path_questions)
    candidate_fields = _known_field_refs_in_text(packet, text)
    if len(candidate_fields) < 2:
        return None
    clarification_options = _schema_path_clarification_options(
        packet,
        schema_path_questions,
    )
    if clarification_options:
        candidate_fields = sorted(
            {
                str(field)
                for option in clarification_options
                for field in _as_list(option.get("fields"))
            }
        )
        candidate_relationships = _unique_relationships(
            relationship
            for option in clarification_options
            for relationship in _as_list(option.get("relationships"))
            if isinstance(relationship, dict)
        )
    else:
        candidate_relationships = _relationships_touching_fields(packet, candidate_fields)
    return {
        "level": "error",
        "code": "clarification_required_schema_path",
        "message": (
            "proposal requires a schema-path clarification; local renderer "
            "will not choose between packet-backed paths"
        ),
        "questions": schema_path_questions,
        "candidate_fields": candidate_fields,
        "candidate_relationships": candidate_relationships,
        "clarification_options": clarification_options,
    }


def _is_schema_path_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    if not tokens & {"which", "what"}:
        return False
    if tokens & {"path", "join", "relationship", "related"}:
        return True
    if tokens & {"field", "column", "dimension", "state", "status", "owner", "date"}:
        return bool(tokens & {"use", "used", "apply", "choose"})
    return False


def _known_field_refs_in_text(packet: JsonObject, text: str) -> list[str]:
    known_fields = _packet_allowed_fields(packet)
    found: set[str] = set()
    normalized_lines = []
    for line in text.splitlines():
        if line.startswith("relationships."):
            line = line.removeprefix("relationships.")
        if line.startswith("entities.") and ".fields." in line:
            parts = line.split(".")
            if len(parts) >= 4:
                candidate = f"{parts[1]}.{parts[3]}"
                if candidate in known_fields:
                    found.add(candidate)
        normalized_lines.append(line)
    normalized_text = "\n".join(normalized_lines)
    for field in known_fields:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])", normalized_text):
            found.add(field)
    for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b", normalized_text):
        candidate = match.group(0)
        if candidate in known_fields:
            found.add(candidate)
    return sorted(found)


def _schema_path_clarification_options(
    packet: JsonObject,
    questions: list[str],
) -> list[JsonObject]:
    allowed_relationships = _packet_allowed_relationships(packet)
    options: list[JsonObject] = []
    seen: set[tuple[str, ...]] = set()
    for question in questions:
        for segment in _schema_path_question_segments(question):
            fields = _known_field_refs_in_text(packet, segment)
            if not fields:
                continue
            relationships = [
                {"from": from_ref, "to": to_ref}
                for from_ref, to_ref in sorted(allowed_relationships)
                if from_ref in fields
            ]
            if not relationships:
                continue
            key = tuple(
                sorted(
                    f"{relationship['from']}->{relationship['to']}"
                    for relationship in relationships
                )
            )
            if key in seen:
                continue
            seen.add(key)
            options.append(
                {
                    "id": f"schema_path_{len(options) + 1}",
                    "label": _schema_path_option_label(segment),
                    "fields": fields,
                    "relationships": relationships,
                }
            )
    if len(options) < 2:
        return []
    return options


def _schema_path_question_segments(question: str) -> list[str]:
    stripped = question.strip()
    if not stripped:
        return []
    segments = [
        segment.strip(" \t\r\n,;:.?")
        for segment in re.split(r"\s+\bor\b\s+", stripped, flags=re.IGNORECASE)
        if segment.strip(" \t\r\n,;:.?")
    ]
    return segments or [stripped]


def _schema_path_option_label(segment: str) -> str:
    label = segment.strip(" \t\r\n,;:.?")
    label = re.sub(r"\s+", " ", label)
    return label[:160]


def _unique_relationships(relationships: Iterable[object]) -> list[JsonObject]:
    unique: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        from_ref = str(relationship.get("from") or "")
        to_ref = str(relationship.get("to") or "")
        if not from_ref or not to_ref:
            continue
        key = (from_ref, to_ref)
        if key in seen:
            continue
        seen.add(key)
        unique.append({"from": from_ref, "to": to_ref})
    return unique


def _relationships_touching_fields(
    packet: JsonObject,
    fields: list[str],
) -> list[JsonObject]:
    field_set = set(fields)
    relationships: list[JsonObject] = []
    for from_ref, to_ref in sorted(_packet_allowed_relationships(packet)):
        if from_ref not in field_set and to_ref not in field_set:
            continue
        relationships.append({"from": from_ref, "to": to_ref})
    return relationships[:12]


def _clarify_auto_route_issue(packet: JsonObject, proposal: JsonObject) -> JsonObject | None:
    if proposal.get("action") != "clarify":
        return None
    questions = [
        str(question)
        for question in _as_list(proposal.get("ambiguity_questions"))
        if str(question).strip()
    ]
    if not questions or not _as_list(proposal.get("projections")):
        return None
    if not all(_is_hidden_filter_clarification(packet, proposal, question) for question in questions):
        return None
    return {
        "level": "warning",
        "code": "clarify_auto_promoted_hidden_filter",
        "message": (
            "proposal asked only about adding an unstated lifecycle/status "
            "filter, so local policy rendered the explicit typed plan"
        ),
        "questions": questions,
    }


def _is_hidden_filter_clarification(
    packet: JsonObject,
    proposal: JsonObject,
    ambiguity_question: str,
) -> bool:
    question_tokens = _tokens(str(packet.get("question") or ""))
    ambiguity_tokens = _tokens(ambiguity_question)
    if "only" not in ambiguity_tokens:
        return False
    if not ambiguity_tokens & {"all", "any"}:
        return False
    if not ambiguity_tokens & {"include", "including", "rows", "records", "cases", "populated"}:
        return False

    status_fields = _packet_status_fields(packet)
    mentioned_fields = {
        field
        for field in status_fields
        if _tokens(field) & ambiguity_tokens
        or _tokens(field.split(".", 1)[-1]) & ambiguity_tokens
    }
    status_terms = _packet_status_value_terms(packet)
    mentioned_terms = status_terms & ambiguity_tokens
    if not mentioned_fields and not mentioned_terms:
        return False
    if mentioned_terms & question_tokens:
        return False
    user_mentioned_status_fields = {
        field
        for field in mentioned_fields
        if _tokens(field.split(".", 1)[-1]) & question_tokens
    }
    if user_mentioned_status_fields:
        return False
    proposal_filter_fields = {
        str(filter_item.get("field") or "")
        for filter_item in _as_list(proposal.get("filters"))
        if isinstance(filter_item, dict)
    }
    return not bool(mentioned_fields & proposal_filter_fields)


def _clarify_subject_date_anchor_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
) -> JsonObject | None:
    questions = [
        str(question)
        for question in _as_list(proposal.get("ambiguity_questions"))
        if str(question).strip()
    ]
    if not questions or not all(
        _is_date_anchor_clarification(question) for question in questions
    ):
        return None
    window = _question_named_date_window(str(packet.get("question") or ""))
    if window is None:
        return None
    target_entities = [
        str(entity)
        for entity in _as_list(proposal.get("target_entities"))
        if str(entity).strip()
    ]
    if len(target_entities) != 1:
        return None
    target_entity = target_entities[0]
    date_field = _single_subject_date_anchor_field(packet, target_entity)
    if date_field is None:
        return None
    existing_filter_fields = {
        str(filter_item.get("field") or "")
        for filter_item in _as_list(proposal.get("filters"))
        if isinstance(filter_item, dict)
    }
    if date_field in existing_filter_fields:
        return None
    start, end, mention = window
    return {
        "filter": {
            "field": date_field,
            "operator": "BETWEEN",
            "value": [start, end],
            "value_kind": "literal",
            "rationale": (
                "governed date-anchor policy: a named date window on a "
                "single-target entity uses that entity's sole subject date field"
            ),
        },
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_subject_date_anchor",
            "message": (
                "proposal asked a date-anchor clarification, but the target "
                "entity has a single subject date field; local policy added "
                f"{date_field} for {mention}"
            ),
            "field": date_field,
            "value": [start, end],
            "questions": questions,
        },
    }


def _is_date_anchor_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    return bool(tokens & {"date", "month", "quarter", "window", "period"}) and bool(
        tokens & {"filter", "anchor", "apply", "use", "being", "associated"}
    )


def _single_subject_date_anchor_field(packet: JsonObject, entity_name: str) -> str | None:
    date_window = packet.get("local_candidates", {}).get("date_window")
    if isinstance(date_window, dict) and date_window.get("ambiguous") is False:
        preferred_anchor = date_window.get("preferred_anchor")
        if isinstance(preferred_anchor, dict):
            preferred_entity = str(preferred_anchor.get("entity") or "")
            preferred_field = str(preferred_anchor.get("field") or "")
            if preferred_entity == entity_name and preferred_field:
                return preferred_field
        anchor_candidates = _as_list(date_window.get("anchor_candidates"))
        if len(anchor_candidates) == 1 and isinstance(anchor_candidates[0], dict):
            only_anchor = anchor_candidates[0]
            anchor_entity = str(only_anchor.get("entity") or "")
            anchor_field = str(only_anchor.get("field") or "")
            if anchor_entity == entity_name and anchor_field:
                return anchor_field
    entity = _schema_card_entity(packet, entity_name)
    if entity is None:
        return None
    date_fields: list[str] = []
    for field in entity.get("fields", []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        role = str(field.get("role") or "").lower()
        field_type = _field_type_kind(_schema_card_field_type(field))
        if role == "date" or field_type in {"date", "datetime"}:
            date_fields.append(str(field["name"]))
    if len(date_fields) != 1:
        return None
    return f"{entity_name}.{date_fields[0]}"


def _schema_card_entity(packet: JsonObject, entity_name: str) -> JsonObject | None:
    for entity in packet.get("schema_card", {}).get("entities", []):
        if isinstance(entity, dict) and entity.get("name") == entity_name:
            return entity
    return None


def _clarify_metric_catalog_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
) -> JsonObject | None:
    questions = [
        str(question)
        for question in _as_list(proposal.get("ambiguity_questions"))
        if str(question).strip()
    ]
    if not questions or not all(_is_metric_formula_clarification(question) for question in questions):
        return None
    metric_projection = _first_conditional_rate_projection(proposal)
    aggregate_projection = None
    if metric_projection is None:
        aggregate_projection = _first_aggregate_projection(proposal)
    if metric_projection is None and aggregate_projection is None:
        return None
    metric_hits = [
        hit
        for hit in _as_list(packet.get("local_candidates", {}).get("metric_catalog_hits"))
        if isinstance(hit, dict)
        and hit.get("source") == "metric_definition"
    ]
    if len(metric_hits) != 1:
        return None
    metric_hit = metric_hits[0]
    if metric_hit.get("metric_kind") == "aggregate":
        if aggregate_projection is None:
            return None
        measure_field = str(metric_hit.get("measure_field") or "")
        aggregate = str(metric_hit.get("aggregate") or "").upper()
        if not measure_field or aggregate not in {"AVG", "COUNT", "MAX", "MIN", "SUM"}:
            return None
        distinct = _boolish(metric_hit.get("distinct"))
        if distinct and aggregate != "COUNT":
            return None
        subject_entity = measure_field.split(".", 1)[0]
        required_entities = set(
            str(entity)
            for entity in _as_list(metric_hit.get("required_entities"))
            if str(entity).strip()
        )
        required_entities.add(subject_entity)
        alias = str(aggregate_projection.get("alias") or metric_hit.get("alias") or "metric")
        return {
            "metric_kind": "aggregate",
            "subject_entity": subject_entity,
            "measure_field": measure_field,
            "aggregate": aggregate,
            "distinct": distinct,
            "required_entities": sorted(required_entities),
            "metric_alias": alias,
            "scale": float(metric_hit.get("scale") or 1.0),
            "issue": {
                "level": "warning",
                "code": "clarify_auto_promoted_metric_catalog",
                "message": (
                    "proposal asked a metric-definition clarification; local "
                    "metric catalog resolved it to a packet-backed aggregate metric"
                ),
                "metric": metric_hit.get("name") or alias,
                "field": measure_field,
                "aggregate": aggregate,
                "questions": questions,
            },
        }
    if metric_hit.get("metric_kind") != "conditional_rate":
        return None
    if metric_projection is None:
        return None
    status_field = str(metric_hit.get("numerator_field") or "")
    status_operator = str(metric_hit.get("numerator_operator") or "=")
    status_value = metric_hit.get("numerator_value")
    status_value_kind = str(metric_hit.get("numerator_value_kind") or "literal")
    denominator_field = str(metric_hit.get("denominator_field") or "")
    if not status_field or status_value is None or not denominator_field:
        return None
    subject_entity = status_field.split(".", 1)[0]
    required_entities = set(
        str(entity)
        for entity in _as_list(metric_hit.get("required_entities"))
        if str(entity).strip()
    )
    required_entities.update(
        _required_entities_after_metric_adjustment(
            proposal,
            subject_entity,
            status_field,
            denominator_field,
        )
    )
    alias = str(metric_projection.get("alias") or metric_hit.get("alias") or "metric")
    return {
        "subject_entity": subject_entity,
        "status_field": status_field,
        "status_operator": status_operator,
        "status_value": status_value,
        "status_value_kind": status_value_kind,
        "denominator_field": denominator_field,
        "required_entities": sorted(required_entities),
        "metric_alias": alias,
        "scale": float(metric_hit.get("scale") or 100.0),
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_metric_catalog",
            "message": (
                "proposal asked a metric-definition clarification; local "
                "metric catalog resolved it to a packet-backed metric definition"
            ),
            "metric": metric_hit.get("name") or alias,
            "field": status_field,
            "value": status_value,
            "questions": questions,
        },
    }


def _clarify_metric_formula_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
) -> JsonObject | None:
    questions = [
        str(question)
        for question in _as_list(proposal.get("ambiguity_questions"))
        if str(question).strip()
    ]
    if not questions or not all(_is_metric_formula_clarification(question) for question in questions):
        return None
    metric_projection = _first_conditional_rate_projection(proposal)
    if metric_projection is None:
        return None
    formulas = [
        formula
        for formula in _as_list(packet.get("local_candidates", {}).get("metric_formula_hits"))
        if isinstance(formula, dict)
        and formula.get("metric_kind") == "conditional_rate"
    ]
    if len(formulas) != 1:
        return None
    formula = formulas[0]
    numerator_field = str(formula.get("numerator_field") or "")
    numerator_value = formula.get("numerator_value")
    denominator_field = str(formula.get("denominator_field") or "")
    if not numerator_field or numerator_value is None or not denominator_field:
        return None
    subject_entity = numerator_field.split(".", 1)[0]
    required_entities = _required_entities_after_metric_adjustment(
        proposal,
        subject_entity,
        numerator_field,
        denominator_field,
    )
    alias = str(metric_projection.get("alias") or formula.get("alias") or "rate")
    return {
        "subject_entity": subject_entity,
        "status_field": numerator_field,
        "status_operator": str(formula.get("numerator_operator") or "="),
        "status_value": numerator_value,
        "status_value_kind": str(formula.get("numerator_value_kind") or "literal"),
        "denominator_field": denominator_field,
        "required_entities": sorted(required_entities),
        "metric_alias": alias,
        "scale": float(formula.get("scale") or 100.0),
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_metric_formula",
            "message": (
                "proposal asked a metric-definition clarification; local "
                "packet evidence exposed a unique conditional-rate formula"
            ),
            "metric": formula.get("alias") or alias,
            "field": numerator_field,
            "value": numerator_value,
            "questions": questions,
        },
    }


def _is_metric_formula_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    return bool(tokens & {"rate", "percent", "percentage", "metric", "numerator", "denominator", "count"}) and bool(
        tokens & {"mean", "means", "definition", "define", "use", "count", "counts", "should"}
    )


def _apply_metric_catalog_adjustment(
    proposal: JsonObject,
    metric_catalog: JsonObject,
) -> None:
    metric_kind = str(metric_catalog.get("metric_kind") or "conditional_rate")
    if metric_kind == "aggregate":
        measure_field = str(metric_catalog["measure_field"])
        aggregate = str(metric_catalog["aggregate"]).upper()
        distinct = _boolish(metric_catalog.get("distinct"))
        required_entities = set(str(entity) for entity in metric_catalog["required_entities"])
        metric_alias = str(metric_catalog["metric_alias"])
        proposal["target_entities"] = [
            entity
            for entity in _as_list(proposal.get("target_entities"))
            if isinstance(entity, str) and entity in required_entities
        ]
        if not proposal["target_entities"]:
            proposal["target_entities"] = sorted(required_entities)
        for projection in _as_list(proposal.get("projections")):
            if not isinstance(projection, dict):
                continue
            if projection.get("kind") != "aggregate":
                continue
            projection["field"] = measure_field
            projection["aggregate"] = aggregate
            projection["distinct"] = distinct
            projection["alias"] = metric_alias
        proposal["joins"] = [
            join
            for join in _as_list(proposal.get("joins"))
            if isinstance(join, dict)
            and str(join.get("from_entity") or "") in required_entities
            and str(join.get("to_entity") or "") in required_entities
        ]
        if not _as_list(proposal.get("order_by")) and _as_list(proposal.get("group_by")):
            proposal["order_by"] = [
                {
                    "field": "",
                    "aggregate": "",
                    "alias": metric_alias,
                    "direction": "DESC",
                    "rationale": "metric catalog default ordering for grouped aggregate metrics",
                }
            ]
        return
    status_field = str(metric_catalog["status_field"])
    status_operator = str(metric_catalog.get("status_operator") or "=")
    denominator_field = str(metric_catalog["denominator_field"])
    status_value = metric_catalog["status_value"]
    status_value_kind = str(metric_catalog.get("status_value_kind") or "value_dictionary")
    required_entities = set(str(entity) for entity in metric_catalog["required_entities"])
    metric_alias = str(metric_catalog["metric_alias"])
    scale = float(metric_catalog.get("scale") or 100.0)
    proposal["target_entities"] = [
        entity
        for entity in _as_list(proposal.get("target_entities"))
        if isinstance(entity, str) and entity in required_entities
    ]
    if not proposal["target_entities"]:
        proposal["target_entities"] = sorted(required_entities)
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("kind") != "conditional_rate":
            continue
        projection["numerator_field"] = status_field
        projection["numerator_operator"] = status_operator
        projection["numerator_value"] = status_value
        projection["numerator_value_kind"] = status_value_kind
        projection["denominator_field"] = denominator_field
        projection["scale"] = scale
    proposal["joins"] = [
        join
        for join in _as_list(proposal.get("joins"))
        if isinstance(join, dict)
        and str(join.get("from_entity") or "") in required_entities
        and str(join.get("to_entity") or "") in required_entities
    ]
    if not _as_list(proposal.get("order_by")) and _as_list(proposal.get("group_by")):
        proposal["order_by"] = [
            {
                "field": "",
                "aggregate": "",
                "alias": metric_alias,
                "direction": "DESC",
                "rationale": "metric catalog default ordering for grouped rate metrics",
            }
        ]


def _clarify_current_plan_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
) -> JsonObject | None:
    questions = [
        str(question)
        for question in _as_list(proposal.get("ambiguity_questions"))
        if str(question).strip()
    ]
    if not questions:
        return None
    open_pipeline = _clarify_open_pipeline_adjustment(packet, proposal, questions)
    if open_pipeline is not None:
        return open_pipeline
    lifecycle_event = _clarify_lifecycle_event_adjustment(packet, proposal, questions)
    if lifecycle_event is not None:
        return lifecycle_event
    lifecycle_existence = _clarify_lifecycle_existence_adjustment(
        packet,
        proposal,
        questions,
    )
    if lifecycle_existence is not None:
        return lifecycle_existence
    explicit_time_grain = _clarify_explicit_time_grain_adjustment(
        packet,
        proposal,
        questions,
    )
    if explicit_time_grain is not None:
        return explicit_time_grain
    if _is_nps_metric_clarification(packet, proposal, questions):
        required_entities = _entities_referenced_by_non_target_fields(proposal)
        preferred_base = _preferred_metric_base_entity(proposal)
        return {
            "target_entities": _ordered_required_entities(
                proposal,
                required_entities,
                preferred_base,
            ),
            "joins": _joins_within_entities(proposal, required_entities),
            "order_by": _default_grouped_metric_order(proposal),
            "issue": {
                "level": "warning",
                "code": "clarify_auto_promoted_named_metric",
                "message": (
                    "proposal asked whether a named metric term should be a "
                    "dimension filter; local policy kept it as the metric and "
                    "dropped unused filter-only joins"
                ),
                "metric": "nps_score",
                "questions": questions,
            },
        }
    if all(_is_unrequested_extra_filter_clarification(question) for question in questions):
        if not _as_list(proposal.get("projections")):
            return None
        return {
            "order_by": _default_grouped_metric_order(proposal),
            "issue": {
                "level": "warning",
                "code": "clarify_auto_promoted_unrequested_extra_filter",
                "message": (
                    "proposal asked only whether to add an extra filter not "
                    "present in the question; local policy rendered the "
                    "explicit typed plan"
                ),
                "questions": questions,
            },
        }
    if all(_is_list_vs_count_clarification(question) for question in questions):
        if not _has_count_projection(proposal) or not _as_list(proposal.get("group_by")):
            return None
        return {
            "order_by": _default_grouped_metric_order(proposal),
            "issue": {
                "level": "warning",
                "code": "clarify_auto_promoted_grouped_count_shape",
                "message": (
                    "proposal asked list-versus-count for a grouped count "
                    "plan; local policy rendered the grouped count"
                ),
                "questions": questions,
            },
        }
    if all(_is_unrequested_time_window_clarification(question) for question in questions):
        if _question_named_date_window(str(packet.get("question") or "")) is not None:
            return None
        if not _has_conditional_rate_projection(proposal):
            return None
        return {
            "order_by": _default_grouped_metric_order(proposal),
            "issue": {
                "level": "warning",
                "code": "clarify_auto_promoted_all_time_metric",
                "message": (
                    "proposal asked for a time window, but the user did not "
                    "provide one; local policy rendered the all-time metric"
                ),
                "questions": questions,
            },
        }
    return None


def _clarify_explicit_time_grain_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
    questions: list[str],
) -> JsonObject | None:
    if not questions or not all(_is_time_grain_clarification(question) for question in questions):
        return None
    if not _as_list(proposal.get("projections")) or not _as_list(proposal.get("group_by")):
        return None
    date_group_fields = [
        str(field)
        for field in _as_list(proposal.get("group_by"))
        if _schema_card_field_is_date(packet, str(field))
    ]
    if not date_group_fields:
        return None
    explicit_fields = [
        field
        for field in date_group_fields
        if _question_explicitly_mentions_schema_field(packet, field)
    ]
    if not explicit_fields:
        return None
    return {
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_explicit_time_grain",
            "message": (
                "proposal asked for a time grain, but the user named a "
                "specific date/time field; local policy rendered the raw "
                "field grouping"
            ),
            "fields": explicit_fields,
            "questions": questions,
        },
    }


def _is_time_grain_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    return bool(tokens & {"grain", "bucket", "buckets", "granularity"}) and bool(
        tokens & {"day", "daily", "week", "weekly", "month", "monthly", "quarter", "year", "time"}
    )


def _schema_card_field_is_date(packet: JsonObject, field_ref: str) -> bool:
    field = _schema_card_field(packet, field_ref)
    if field is None:
        return False
    role = str(field.get("role") or "").lower()
    field_type = _field_type_kind(_schema_card_field_type(field))
    return role == "date" or field_type in {"date", "datetime"}


def _question_explicitly_mentions_schema_field(packet: JsonObject, field_ref: str) -> bool:
    field = _schema_card_field(packet, field_ref)
    if field is None:
        return False
    question_tokens = _tokens(str(packet.get("question") or ""))
    candidates = [
        str(field.get("name") or ""),
        str(field.get("db_column") or ""),
        str(field.get("display_label") or ""),
    ]
    weak_tokens = {"at", "on", "in", "by", "of", "the", "date", "time"}
    for candidate in candidates:
        candidate_tokens = _tokens(candidate)
        if not candidate_tokens or not candidate_tokens <= question_tokens:
            continue
        if candidate_tokens - weak_tokens:
            return True
    return False


def _schema_card_field(packet: JsonObject, field_ref: str) -> JsonObject | None:
    if "." not in field_ref:
        return None
    entity_name, field_name = field_ref.split(".", 1)
    entity = _schema_card_entity(packet, entity_name)
    if entity is None:
        return None
    for field in entity.get("fields", []):
        if isinstance(field, dict) and str(field.get("name") or "") == field_name:
            return field
    return None


def _apply_current_plan_adjustment(proposal: JsonObject, adjustment: JsonObject) -> None:
    if "target_entities" in adjustment:
        proposal["target_entities"] = adjustment["target_entities"]
    if "joins" in adjustment:
        proposal["joins"] = adjustment["joins"]
    if "replace_filters" in adjustment:
        proposal["filters"] = adjustment["replace_filters"]
    if "append_filters" in adjustment:
        filters = [
            filter_item
            for filter_item in _as_list(proposal.get("filters"))
            if isinstance(filter_item, dict)
        ]
        filters.extend(
            filter_item
            for filter_item in _as_list(adjustment.get("append_filters"))
            if isinstance(filter_item, dict)
        )
        proposal["filters"] = filters
    if not _as_list(proposal.get("order_by")) and adjustment.get("order_by"):
        proposal["order_by"] = adjustment["order_by"]


def _clarify_open_pipeline_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
    questions: list[str],
) -> JsonObject | None:
    question_tokens = _tokens(str(packet.get("question") or ""))
    if not {"open", "pipeline"} <= question_tokens:
        return None
    if not any({"open", "pipeline"} <= _tokens(question) for question in questions):
        return None
    metric_entity = _first_metric_projection_entity(proposal)
    if metric_entity is None:
        return None
    stage_candidate = _open_stage_field(packet, metric_entity)
    if stage_candidate is None:
        return None
    stage_field, terminal_values = stage_candidate
    existing_filter_fields = {
        str(filter_item.get("field") or "")
        for filter_item in _as_list(proposal.get("filters"))
        if isinstance(filter_item, dict)
    }
    if stage_field in existing_filter_fields:
        return None
    return {
        "append_filters": [
            {
                "field": stage_field,
                "operator": "NOT IN",
                "value": terminal_values,
                "value_kind": "value_dictionary",
                "rationale": (
                    "governed open-pipeline policy: open pipeline excludes "
                    "packet-backed terminal stage values"
                ),
            }
        ],
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_open_stage_metric",
            "message": (
                "proposal asked for an open-pipeline definition; local policy "
                f"resolved it by excluding terminal values on {stage_field}"
            ),
            "field": stage_field,
            "excluded_values": terminal_values,
            "questions": questions,
        },
    }


def _clarify_lifecycle_event_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
    questions: list[str],
) -> JsonObject | None:
    question = str(packet.get("question") or "")
    question_tokens = _tokens(question)
    lifecycle_terms = _lifecycle_status_terms(question_tokens)
    if not lifecycle_terms:
        return None
    if not any(_is_date_anchor_clarification(question) for question in questions):
        return None
    window = _question_named_date_window(question)
    if window is None:
        return None
    subject_entities = _proposal_subject_entities(proposal)
    if not subject_entities:
        return None
    candidate = _lifecycle_event_candidate(packet, subject_entities, lifecycle_terms)
    if candidate is None:
        return None
    status_field, status_value, date_field = candidate
    start, end, mention = window
    filters = [
        filter_item
        for filter_item in _as_list(proposal.get("filters"))
        if isinstance(filter_item, dict)
        and not _is_replaceable_window_filter(packet, filter_item, date_field, window)
    ]
    existing_fields = {
        str(filter_item.get("field") or "")
        for filter_item in filters
        if isinstance(filter_item, dict)
    }
    if status_field not in existing_fields:
        filters.append(
            {
                "field": status_field,
                "operator": "=",
                "value": status_value,
                "value_kind": "value_dictionary",
                "rationale": (
                    "governed lifecycle-event policy: lifecycle wording maps "
                    "to a packet-backed event/status field"
                ),
            }
        )
    if date_field not in existing_fields:
        filters.append(
            {
                "field": date_field,
                "operator": "BETWEEN",
                "value": [start, end],
                "value_kind": "literal",
                "rationale": (
                    "governed lifecycle-event policy: named date window uses "
                    "the matched event date field"
                ),
            }
        )
    return {
        "replace_filters": filters,
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_lifecycle_event_date",
            "message": (
                "proposal asked a lifecycle/date-anchor clarification; local "
                f"policy resolved {mention} to {date_field} with {status_field} = {status_value}"
            ),
            "status_field": status_field,
            "status_value": status_value,
            "date_field": date_field,
            "value": [start, end],
            "questions": questions,
        },
    }


def _clarify_lifecycle_existence_adjustment(
    packet: JsonObject,
    proposal: JsonObject,
    questions: list[str],
) -> JsonObject | None:
    question_tokens = _tokens(str(packet.get("question") or ""))
    lifecycle_terms = _lifecycle_event_modifier_terms(question_tokens)
    if not lifecycle_terms:
        return None
    if not questions or not all(
        _is_lifecycle_identification_clarification(question, lifecycle_terms)
        for question in questions
    ):
        return None
    subject_entities = _proposal_metric_subject_entities(proposal)
    if not subject_entities:
        subject_entities = _proposal_subject_entities(proposal)
    if _subject_has_backed_lifecycle_status_value(packet, subject_entities, lifecycle_terms):
        return None
    event_field = _single_lifecycle_existence_field(
        packet,
        subject_entities,
        lifecycle_terms,
    )
    if event_field is None:
        return None
    existing_fields = {
        str(filter_item.get("field") or "")
        for filter_item in _as_list(proposal.get("filters"))
        if isinstance(filter_item, dict)
    }
    if event_field in existing_fields:
        return None
    return {
        "append_filters": [
            {
                "field": event_field,
                "operator": "IS NOT NULL",
                "value": None,
                "value_kind": "null_check",
                "rationale": (
                    "governed lifecycle-existence policy: lifecycle wording "
                    "maps to a packet-backed event timestamp when no backed "
                    "status value exists"
                ),
            }
        ],
        "issue": {
            "level": "warning",
            "code": "clarify_auto_promoted_lifecycle_existence",
            "message": (
                "proposal asked how to identify a lifecycle modifier; local "
                f"policy resolved it to {event_field} IS NOT NULL because no "
                "matching status value dictionary evidence was available"
            ),
            "field": event_field,
            "questions": questions,
        },
    }


def _is_lifecycle_identification_clarification(
    ambiguity_question: str,
    lifecycle_terms: set[str],
) -> bool:
    tokens = _tokens(ambiguity_question)
    if not tokens & lifecycle_terms:
        return False
    if tokens & {"identify", "definition", "define", "meaning", "means"}:
        return True
    return bool(tokens & {"status", "state"} and tokens & {"date", "timestamp", "at"})


def _proposal_metric_subject_entities(proposal: JsonObject) -> set[str]:
    entities: set[str] = set()
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("kind") not in {"aggregate", "conditional_rate", "count"}:
            continue
        for key in ("field", "numerator_field", "denominator_field"):
            field = str(projection.get(key) or "")
            if "." in field:
                entities.add(field.split(".", 1)[0])
    return entities


def _subject_has_backed_lifecycle_status_value(
    packet: JsonObject,
    subject_entities: set[str],
    lifecycle_terms: set[str],
) -> bool:
    for entity_name in subject_entities:
        entity = _schema_card_entity(packet, entity_name)
        if entity is None:
            continue
        for field in entity.get("fields", []):
            if not isinstance(field, dict) or not field.get("name"):
                continue
            if str(field.get("role") or "").lower() != "status":
                continue
            for value_entry in field.get("value_dictionary", []):
                if not isinstance(value_entry, dict):
                    continue
                raw_value = str(value_entry.get("raw_value") or "")
                value_tokens = _tokens(f"{raw_value} {value_entry.get('term') or ''}")
                if raw_value and value_tokens & lifecycle_terms:
                    return True
    return False


def _single_lifecycle_existence_field(
    packet: JsonObject,
    subject_entities: set[str],
    lifecycle_terms: set[str],
) -> str | None:
    candidates: list[str] = []
    for entity_name in subject_entities:
        entity = _schema_card_entity(packet, entity_name)
        if entity is None:
            continue
        for field in entity.get("fields", []):
            if not isinstance(field, dict) or not field.get("name"):
                continue
            role = str(field.get("role") or "").lower()
            field_type = _field_type_kind(_schema_card_field_type(field))
            if role != "date" and field_type not in {"date", "datetime"}:
                continue
            if not (_schema_field_tokens(field) & lifecycle_terms):
                continue
            candidates.append(f"{entity_name}.{field['name']}")
    if len(candidates) == 1:
        return candidates[0]
    return None


def _first_metric_projection_entity(proposal: JsonObject) -> str | None:
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("kind") not in {"aggregate", "conditional_rate"}:
            continue
        for key in ("field", "denominator_field", "numerator_field"):
            field = str(projection.get(key) or "")
            if "." in field:
                return field.split(".", 1)[0]
    return None


def _open_stage_field(packet: JsonObject, entity_name: str) -> tuple[str, list[str]] | None:
    entity = _schema_card_entity(packet, entity_name)
    if entity is None:
        return None
    for field in entity.get("fields", []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        field_tokens = _schema_field_tokens(field)
        if not field_tokens & {"stage", "status", "state"}:
            continue
        terminal_values: list[str] = []
        non_terminal_count = 0
        for value_entry in field.get("value_dictionary", []):
            if not isinstance(value_entry, dict):
                continue
            raw_value = str(value_entry.get("raw_value") or "")
            value_tokens = _tokens(f"{raw_value} {value_entry.get('term') or ''}")
            if "closed" in value_tokens or value_tokens & {"won", "lost"}:
                if raw_value and raw_value not in terminal_values:
                    terminal_values.append(raw_value)
            else:
                non_terminal_count += 1
        if terminal_values and non_terminal_count > 0:
            return f"{entity_name}.{field['name']}", terminal_values
    return None


def _lifecycle_status_terms(question_tokens: set[str]) -> set[str]:
    if question_tokens & {"churn", "churned", "churns", "cancelled", "canceled"}:
        return {"churn", "churned", "cancelled", "canceled", "cancel", "ended"}
    if question_tokens & {"closed", "lost"}:
        return {"closed", "lost", "ended"}
    return set()


def _lifecycle_event_modifier_terms(question_tokens: set[str]) -> set[str]:
    out: set[str] = set()
    if question_tokens & {"paid"}:
        out.add("paid")
    if question_tokens & {"resolved", "resolve"}:
        out.update({"resolved", "resolve"})
    if question_tokens & {"completed", "complete"}:
        out.update({"completed", "complete"})
    if question_tokens & {"closed", "lost"}:
        out.update({"closed", "lost"})
    if question_tokens & {"cancelled", "canceled", "cancel", "cancels"}:
        out.update({"cancelled", "canceled", "cancel"})
    return out


def _proposal_subject_entities(proposal: JsonObject) -> set[str]:
    subject_entities = {
        str(entity)
        for entity in _as_list(proposal.get("target_entities"))
        if isinstance(entity, str) and entity
    }
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        field = str(projection.get("field") or "")
        if "." in field:
            subject_entities.add(field.split(".", 1)[0])
    return subject_entities


def _lifecycle_event_candidate(
    packet: JsonObject,
    subject_entities: set[str],
    lifecycle_terms: set[str],
) -> tuple[str, str, str] | None:
    graph = _graph_rows_from_schema_card(packet.get("schema_card", {}))
    relationships = graph["relationships"]
    candidates: list[tuple[str, str, str]] = []
    for entity in packet.get("schema_card", {}).get("entities", []):
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        entity_name = str(entity["name"])
        if entity_name in subject_entities:
            continue
        if not any(
            _relationship_path({subject_entity}, entity_name, relationships) is not None
            for subject_entity in subject_entities
        ):
            continue
        status = _matching_lifecycle_status_field(entity_name, entity, lifecycle_terms)
        if status is None:
            continue
        event_date_field = _single_event_date_field(entity_name, entity, lifecycle_terms)
        if event_date_field is None:
            continue
        candidates.append((status[0], status[1], event_date_field))
    if len(candidates) == 1:
        return candidates[0]
    return None


def _matching_lifecycle_status_field(
    entity_name: str,
    entity: JsonObject,
    lifecycle_terms: set[str],
) -> tuple[str, str] | None:
    for field in entity.get("fields", []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        if str(field.get("role") or "").lower() != "status":
            continue
        for value_entry in field.get("value_dictionary", []):
            if not isinstance(value_entry, dict):
                continue
            raw_value = str(value_entry.get("raw_value") or "")
            value_tokens = _tokens(f"{raw_value} {value_entry.get('term') or ''}")
            if raw_value and value_tokens & lifecycle_terms:
                return f"{entity_name}.{field['name']}", raw_value
    return None


def _single_event_date_field(
    entity_name: str,
    entity: JsonObject,
    lifecycle_terms: set[str],
) -> str | None:
    date_fields: list[str] = []
    event_tokens = lifecycle_terms | {"end", "ended", "ending", "cancel", "cancelled", "canceled", "closed", "lost"}
    for field in entity.get("fields", []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        role = str(field.get("role") or "").lower()
        field_type = _field_type_kind(_schema_card_field_type(field))
        if role != "date" and field_type not in {"date", "datetime"}:
            continue
        if not (_schema_field_tokens(field) & event_tokens):
            continue
        date_fields.append(f"{entity_name}.{field['name']}")
    if len(date_fields) == 1:
        return date_fields[0]
    return None


def _is_replaceable_window_filter(
    packet: JsonObject,
    filter_item: JsonObject,
    replacement_field: str,
    window: tuple[str, str, str],
) -> bool:
    field = str(filter_item.get("field") or "")
    if not field or field == replacement_field:
        return False
    if _packet_field_role_map(packet).get(field) != "date":
        return False
    operator = _normalize_operator(
        str(filter_item.get("operator") or "="),
        filter_item.get("value"),
    )
    value = _normalize_filter_value(operator, filter_item.get("value"))
    return operator == "BETWEEN" and isinstance(value, list) and value[:2] == [window[0], window[1]]


def _schema_field_tokens(field: JsonObject) -> set[str]:
    return _tokens(
        " ".join(
            [
                str(field.get("name") or ""),
                str(field.get("db_column") or ""),
                str(field.get("display_label") or ""),
            ]
        )
    )


def _is_nps_metric_clarification(
    packet: JsonObject,
    proposal: JsonObject,
    questions: list[str],
) -> bool:
    question_tokens = _tokens(str(packet.get("question") or ""))
    if "nps" not in question_tokens or "score" not in question_tokens:
        return False
    if not all("nps" in _tokens(question) and "filter" in _tokens(question) for question in questions):
        return False
    return any(
        isinstance(projection, dict)
        and projection.get("kind") == "aggregate"
        and str(projection.get("aggregate") or "").upper() == "AVG"
        for projection in _as_list(proposal.get("projections"))
    )


def _is_unrequested_extra_filter_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    if bool(tokens & {"also", "additionally"}) and bool(
        tokens & {"restrict", "filter", "include"}
    ):
        return True
    return (
        "only" in tokens
        and bool(tokens & {"mean", "intend", "intended"})
        and bool(tokens & {"filter", "predicate", "restrict"})
        and bool(tokens & {"or", "instead"})
    )


def _is_list_vs_count_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    return "list" in tokens and bool(tokens & {"count", "counts", "number"})


def _is_unrequested_time_window_clarification(ambiguity_question: str) -> bool:
    tokens = _tokens(ambiguity_question)
    return "time" in tokens and "window" in tokens


def _has_count_projection(proposal: JsonObject) -> bool:
    return any(
        isinstance(projection, dict) and projection.get("kind") == "count"
        for projection in _as_list(proposal.get("projections"))
    )


def _has_conditional_rate_projection(proposal: JsonObject) -> bool:
    return _first_conditional_rate_projection(proposal) is not None


def _default_grouped_metric_order(proposal: JsonObject) -> list[JsonObject]:
    if not _as_list(proposal.get("group_by")):
        return []
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("kind") not in {"count", "aggregate", "conditional_rate"}:
            continue
        alias = str(projection.get("alias") or "")
        if not alias:
            continue
        return [
            {
                "field": "",
                "aggregate": "",
                "alias": alias,
                "direction": "DESC",
                "rationale": "local default ordering for grouped metric plans",
            }
        ]
    return []


def _entities_referenced_by_non_target_fields(proposal: JsonObject) -> set[str]:
    entities: set[str] = set()
    for field in _proposal_field_refs(proposal):
        if "." in field:
            entities.add(field.split(".", 1)[0])
    return entities


def _preferred_metric_base_entity(proposal: JsonObject) -> str | None:
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("kind") in {"aggregate", "conditional_rate"}:
            for key in ("field", "denominator_field", "numerator_field"):
                field = str(projection.get(key) or "")
                if "." in field:
                    return field.split(".", 1)[0]
    return None


def _ordered_required_entities(
    proposal: JsonObject,
    required_entities: set[str],
    preferred_base: str | None,
) -> list[str]:
    out: list[str] = []
    if preferred_base in required_entities:
        out.append(str(preferred_base))
    for entity in _as_list(proposal.get("target_entities")):
        if isinstance(entity, str) and entity in required_entities and entity not in out:
            out.append(entity)
    for entity in sorted(required_entities):
        if entity not in out:
            out.append(entity)
    return out


def _joins_within_entities(proposal: JsonObject, entities: set[str]) -> list[JsonObject]:
    return [
        join
        for join in _as_list(proposal.get("joins"))
        if isinstance(join, dict)
        and str(join.get("from_entity") or "") in entities
        and str(join.get("to_entity") or "") in entities
    ]


def _first_conditional_rate_projection(proposal: JsonObject) -> JsonObject | None:
    for projection in _as_list(proposal.get("projections")):
        if isinstance(projection, dict) and projection.get("kind") == "conditional_rate":
            return projection
    return None


def _first_aggregate_projection(proposal: JsonObject) -> JsonObject | None:
    for projection in _as_list(proposal.get("projections")):
        if isinstance(projection, dict) and projection.get("kind") == "aggregate":
            return projection
    return None


def _required_entities_after_metric_adjustment(
    proposal: JsonObject,
    subject_entity: str,
    status_field: str,
    denominator_field: str,
) -> set[str]:
    required = {subject_entity}
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("kind") == "conditional_rate":
            continue
        field = str(projection.get("field") or "")
        if "." in field:
            required.add(field.split(".", 1)[0])
    for filter_item in _as_list(proposal.get("filters")):
        if isinstance(filter_item, dict) and filter_item.get("field"):
            field = str(filter_item["field"])
            if "." in field:
                required.add(field.split(".", 1)[0])
    for field in _as_list(proposal.get("group_by")):
        field_ref = str(field or "")
        if "." in field_ref:
            required.add(field_ref.split(".", 1)[0])
    for order in _as_list(proposal.get("order_by")):
        if isinstance(order, dict) and order.get("field"):
            field = str(order["field"])
            if "." in field:
                required.add(field.split(".", 1)[0])
    for field in (status_field, denominator_field):
        if "." in field:
            required.add(field.split(".", 1)[0])
    return required


MONTH_NUMBERS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _question_named_date_window(question: str) -> tuple[str, str, str] | None:
    match = re.search(r"\bq([1-4])\s+(20\d{2})\b", question, flags=re.IGNORECASE)
    if match is not None:
        quarter_text, year_text = match.groups()
        return _quarter_date_window(int(year_text), int(quarter_text))
    match = re.search(r"\b(20\d{2})\s+q([1-4])\b", question, flags=re.IGNORECASE)
    if match is not None:
        year_text, quarter_text = match.groups()
        return _quarter_date_window(int(year_text), int(quarter_text))
    month_pattern = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
    match = re.search(
        rf"\b({month_pattern})\s+(20\d{{2}})\b",
        question,
        flags=re.IGNORECASE,
    )
    if match is not None:
        month_text, year_text = match.groups()
        return _month_date_window(int(year_text), MONTH_NUMBERS[month_text.lower()])
    match = re.search(
        rf"\b(20\d{{2}})\s+({month_pattern})\b",
        question,
        flags=re.IGNORECASE,
    )
    if match is not None:
        year_text, month_text = match.groups()
        return _month_date_window(int(year_text), MONTH_NUMBERS[month_text.lower()])
    return None


def _month_date_window(year: int, month: int) -> tuple[str, str, str]:
    end_year = year + 1 if month == 12 else year
    end_month = 1 if month == 12 else month + 1
    start = f"{year:04}-{month:02}-01"
    end = f"{end_year:04}-{end_month:02}-01"
    mention = f"{year:04}-{month:02}"
    return start, end, mention


def _quarter_date_window(year: int, quarter: int) -> tuple[str, str, str]:
    start_month = ((quarter - 1) * 3) + 1
    end_month = start_month + 3
    end_year = year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    start = f"{year:04}-{start_month:02}-01"
    end = f"{end_year:04}-{end_month:02}-01"
    mention = f"{year:04}-Q{quarter}"
    return start, end, mention


def _render_result(
    packet: JsonObject,
    proposal: JsonObject,
    validation: JsonObject,
    sql: str | None,
    issues: list[JsonObject],
    *,
    dialect: str,
    result_shape: JsonObject | None = None,
    query_frame_candidate: JsonObject | None = None,
) -> JsonObject:
    return {
        "schema_version": 1,
        "source": "semsql_resolution_plan_renderer",
        "valid": not _has_render_errors(issues) and sql is not None,
        "dialect": dialect,
        "question": packet.get("question"),
        "proposal_action": proposal.get("action"),
        "effective_action": "route" if sql is not None else proposal.get("action"),
        "validation": validation,
        "sql": sql,
        "result_shape": result_shape,
        "query_frame_candidate": query_frame_candidate,
        "issues": issues,
    }


def _has_render_errors(issues: list[JsonObject]) -> bool:
    return any(str(issue.get("level") or "error") == "error" for issue in issues)


def _packet_graph_rows(packet: JsonObject) -> JsonObject:
    graph_path = packet.get("schema_card", {}).get("graph")
    if isinstance(graph_path, str) and graph_path and Path(graph_path).exists():
        return _read_graph_rows(Path(graph_path))
    return _graph_rows_from_schema_card(packet.get("schema_card", {}))


def _graph_rows_from_schema_card(schema_card: JsonObject) -> JsonObject:
    entities = [
        {
            "canonical_name": entity["name"],
            "db_table": entity["db_table"],
            "singular_label": None,
            "plural_label": None,
        }
        for entity in schema_card.get("entities", [])
        if isinstance(entity, dict) and entity.get("name") and entity.get("db_table")
    ]
    fields = []
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        for field in entity.get("fields", []):
            if not isinstance(field, dict) or not field.get("name"):
                continue
            fields.append(
                {
                    "entity": entity["name"],
                    "field": field["name"],
                    "db_column": field.get("db_column") or field["name"],
                    "type": field.get("type") or "text",
                    "display_label": field.get("display_label"),
                }
            )
    relationships = []
    for relationship in schema_card.get("relationships", []):
        if not isinstance(relationship, dict):
            continue
        from_ref = str(relationship.get("from") or "")
        to_ref = str(relationship.get("to") or "")
        if "." not in from_ref or "." not in to_ref:
            continue
        from_entity, from_field = from_ref.split(".", 1)
        to_entity, to_field = to_ref.split(".", 1)
        relationships.append(
            {
                "from_entity": from_entity,
                "from_field": from_field,
                "to_entity": to_entity,
                "to_field": to_field,
                "kind": relationship.get("kind") or "relationship",
            }
        )
    return {
        "entities": entities,
        "fields": fields,
        "relationships": relationships,
        "sample_values": {},
        "vocabulary": [],
        "metric_definitions": _as_list(schema_card.get("metric_definitions")),
    }


def _graph_index(graph: JsonObject) -> JsonObject:
    entities = {
        str(entity["canonical_name"]): entity
        for entity in graph["entities"]
    }
    fields = {
        f"{field['entity']}.{field['field']}": field
        for field in graph["fields"]
    }
    return {
        "entities": entities,
        "fields": fields,
        "relationships": graph["relationships"],
    }


def _proposal_base_entity(proposal: JsonObject, graph_index: JsonObject) -> str | None:
    for entity in _as_list(proposal.get("target_entities")):
        entity_name = str(entity)
        if entity_name in graph_index["entities"]:
            return entity_name
    for field in _proposal_field_refs(proposal):
        if "." in field:
            entity_name = field.split(".", 1)[0]
            if entity_name in graph_index["entities"]:
                return entity_name
    return None


def _proposal_required_entities(proposal: JsonObject, base_entity: str) -> set[str]:
    entities = {base_entity}
    for entity in _as_list(proposal.get("target_entities")):
        if isinstance(entity, str):
            entities.add(entity)
    for field in _proposal_field_refs(proposal):
        if "." in field:
            entities.add(field.split(".", 1)[0])
    return entities


def _proposal_field_refs(proposal: JsonObject) -> list[str]:
    fields: list[str] = []
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if projection.get("field"):
            fields.append(str(projection["field"]))
        if projection.get("numerator_field"):
            fields.append(str(projection["numerator_field"]))
        if projection.get("denominator_field"):
            fields.append(str(projection["denominator_field"]))
    for filter_item in _as_list(proposal.get("filters")):
        if isinstance(filter_item, dict) and filter_item.get("field"):
            fields.append(str(filter_item["field"]))
    fields.extend(str(field) for field in _as_list(proposal.get("group_by")) if field)
    for order in _as_list(proposal.get("order_by")):
        if isinstance(order, dict) and order.get("field"):
            fields.append(str(order["field"]))
    return fields


def _proposal_join_plan(
    proposal: JsonObject,
    base_entity: str,
    required_entities: set[str],
    graph_index: JsonObject,
    issues: list[JsonObject],
) -> list[JsonObject]:
    explicit = [
        join for join in _as_list(proposal.get("joins")) if isinstance(join, dict)
    ]
    if explicit:
        return _orient_explicit_joins(explicit, base_entity)
    plan: list[JsonObject] = []
    connected = {base_entity}
    for entity in sorted(required_entities - connected):
        path = _relationship_path(connected, entity, graph_index["relationships"])
        if path is None:
            issues.append(
                {
                    "level": "error",
                    "code": "missing_join_path",
                    "message": f"no relationship path from {sorted(connected)} to {entity}",
                }
            )
            continue
        for step in path:
            plan.append(step)
            connected.add(str(step["from_entity"]))
            connected.add(str(step["to_entity"]))
    return plan


def _orient_explicit_joins(
    explicit_joins: list[JsonObject],
    base_entity: str,
) -> list[JsonObject]:
    pending = [_normalize_join(join) for join in explicit_joins]
    connected = {base_entity}
    plan: list[JsonObject] = []
    while pending:
        next_pending: list[JsonObject] = []
        progressed = False
        for join in pending:
            from_entity = str(join["from_entity"])
            to_entity = str(join["to_entity"])
            if from_entity in connected:
                plan.append(join)
                connected.add(to_entity)
                progressed = True
                continue
            if to_entity in connected:
                plan.append(
                    {
                        "from_entity": join["to_entity"],
                        "from_field": join["to_field"],
                        "to_entity": join["from_entity"],
                        "to_field": join["from_field"],
                    }
                )
                connected.add(from_entity)
                progressed = True
                continue
            next_pending.append(join)
        if not progressed:
            plan.extend(next_pending)
            break
        pending = next_pending
    return plan


def _relationship_path(
    connected: set[str],
    target: str,
    relationships: list[JsonObject],
) -> list[JsonObject] | None:
    queue: list[tuple[str, list[JsonObject]]] = [
        (entity, []) for entity in sorted(connected)
    ]
    seen = set(connected)
    while queue:
        entity, path = queue.pop(0)
        if entity == target:
            return path
        for relationship in relationships:
            neighbors = []
            if relationship["from_entity"] == entity:
                neighbors.append(_normalize_join(relationship))
            if relationship["to_entity"] == entity:
                neighbors.append(
                    {
                        "from_entity": relationship["to_entity"],
                        "from_field": relationship["to_field"],
                        "to_entity": relationship["from_entity"],
                        "to_field": relationship["from_field"],
                    }
                )
            for step in neighbors:
                next_entity = str(step["to_entity"])
                if next_entity in seen:
                    continue
                seen.add(next_entity)
                queue.append((next_entity, [*path, step]))
    return None


def _normalize_join(join: JsonObject) -> JsonObject:
    from_entity = str(join["from_entity"])
    to_entity = str(join["to_entity"])
    return {
        "from_entity": from_entity,
        "from_field": _join_field_name(from_entity, str(join["from_field"])),
        "to_entity": to_entity,
        "to_field": _join_field_name(to_entity, str(join["to_field"])),
    }


def _join_field_name(entity: str, field: str) -> str:
    prefix = f"{entity}."
    return field[len(prefix) :] if field.startswith(prefix) else field


def _proposal_uses_select_distinct(proposal: JsonObject) -> bool:
    if proposal.get("distinct") is not True:
        return False
    if _as_list(proposal.get("group_by")):
        return False
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if str(projection.get("kind") or "") in {
            "aggregate",
            "conditional_rate",
            "count",
        }:
            return False
    return True


def _proposal_select_items(
    proposal: JsonObject,
    graph_index: JsonObject,
    issues: list[JsonObject],
    *,
    dialect: str,
) -> list[str]:
    items: list[str] = []
    seen_items: set[str] = set()
    for field in _as_list(proposal.get("group_by")):
        _append_unique_select_item(
            items,
            seen_items,
            _render_field_ref(str(field), graph_index, dialect=dialect),
        )
    for idx, projection in enumerate(_as_list(proposal.get("projections"))):
        if not isinstance(projection, dict):
            continue
        kind = str(projection.get("kind") or "")
        field = str(projection.get("field") or "")
        aggregate = str(projection.get("aggregate") or "").upper()
        if kind == "all":
            issues.append(
                {
                    "level": "error",
                    "code": "all_projection_forbidden",
                    "message": "all-column projection is not renderable in LLM fallback",
                    "path": f"projections[{idx}]",
                }
            )
        elif kind == "count":
            alias = str(projection.get("alias") or "count")
            _append_unique_select_item(
                items,
                seen_items,
                (
                    f"{_render_aggregate_expression('COUNT', field, graph_index, dialect=dialect)} "
                    f"AS {_quote_identifier(alias, dialect)}"
                ),
            )
        elif kind == "field":
            _append_unique_select_item(
                items,
                seen_items,
                _render_field_ref(field, graph_index, dialect=dialect),
            )
        elif kind == "aggregate":
            if aggregate not in {"AVG", "COUNT", "MAX", "MIN", "SUM"}:
                issues.append(
                    {
                        "level": "error",
                        "code": "unsupported_aggregate",
                        "message": f"unsupported aggregate `{aggregate}`",
                        "path": f"projections[{idx}].aggregate",
                    }
                )
                continue
            alias = str(projection.get("alias") or aggregate.lower())
            _append_unique_select_item(
                items,
                seen_items,
                (
                    f"{_render_aggregate_expression(aggregate, field, graph_index, dialect=dialect, distinct=bool(projection.get('distinct')))} "
                    f"AS {_quote_identifier(alias, dialect)}"
                ),
            )
        elif kind == "conditional_rate":
            alias = str(projection.get("alias") or "rate")
            _append_unique_select_item(
                items,
                seen_items,
                (
                    f"{_render_conditional_rate_expression(projection, graph_index, dialect=dialect)} "
                    f"AS {_quote_identifier(alias, dialect)}"
                ),
            )
    return items or ["COUNT(*) AS count"]


def _append_unique_select_item(items: list[str], seen_items: set[str], item: str) -> None:
    if item in seen_items:
        return
    seen_items.add(item)
    items.append(item)


def _render_join_step(
    join: JsonObject,
    graph_index: JsonObject,
    *,
    dialect: str,
) -> str:
    to_table = _quote_identifier(
        graph_index["entities"][join["to_entity"]]["db_table"],
        dialect,
    )
    left = _render_field_ref(
        f"{join['from_entity']}.{join['from_field']}",
        graph_index,
        dialect=dialect,
    )
    right = _render_field_ref(
        f"{join['to_entity']}.{join['to_field']}",
        graph_index,
        dialect=dialect,
    )
    return f"JOIN {to_table} ON {left} = {right}"


def _render_filter(filter_item: JsonObject, graph_index: JsonObject, *, dialect: str) -> str:
    field_ref = _render_field_ref(str(filter_item["field"]), graph_index, dialect=dialect)
    operator = _normalize_operator(
        str(filter_item.get("operator") or "="),
        filter_item.get("value"),
    )
    if operator == "<>":
        operator = "!="
    if operator in {"IS NULL", "IS NOT NULL"}:
        return f"{field_ref} {operator}"
    field = graph_index["fields"][str(filter_item["field"])]
    raw_value = _normalize_filter_value(operator, filter_item.get("value"))
    if operator in {"IN", "NOT IN"} and isinstance(raw_value, list):
        values = ", ".join(_render_literal(item, field) for item in raw_value)
        return f"{field_ref} {operator} ({values})"
    if operator == "BETWEEN" and isinstance(raw_value, list) and len(raw_value) == 2:
        lower = _render_literal(raw_value[0], field)
        upper = _render_literal(raw_value[1], field)
        return f"{field_ref} BETWEEN {lower} AND {upper}"
    value = _render_literal(raw_value, field)
    return f"{field_ref} {operator} {value}"


def _render_order_by(order: JsonObject, graph_index: JsonObject, *, dialect: str) -> str:
    direction = str(order.get("direction") or "ASC").upper()
    if direction not in {"ASC", "DESC"}:
        direction = "ASC"
    alias = str(order.get("alias") or "")
    if alias:
        return f"{_quote_identifier(alias, dialect)} {direction}"
    aggregate = str(order.get("aggregate") or "").upper()
    field = str(order.get("field") or "")
    if aggregate:
        return (
            f"{_render_aggregate_expression(aggregate, field, graph_index, dialect=dialect, distinct=bool(order.get('distinct')))} "
            f"{direction}"
        )
    return f"{_render_field_ref(field, graph_index, dialect=dialect)} {direction}"


def _render_aggregate_expression(
    aggregate: str,
    field: str,
    graph_index: JsonObject,
    *,
    dialect: str,
    distinct: bool = False,
) -> str:
    normalized = aggregate.upper()
    if normalized == "COUNT" and not field:
        return "COUNT(*)"
    if distinct and normalized == "COUNT":
        return f"COUNT(DISTINCT {_render_field_ref(field, graph_index, dialect=dialect)})"
    return f"{normalized}({_render_field_ref(field, graph_index, dialect=dialect)})"


def _render_conditional_rate_expression(
    projection: JsonObject,
    graph_index: JsonObject,
    *,
    dialect: str,
) -> str:
    numerator_filter = {
        "field": projection.get("numerator_field"),
        "operator": projection.get("numerator_operator") or "=",
        "value": projection.get("numerator_value"),
    }
    condition = _render_filter(numerator_filter, graph_index, dialect=dialect)
    denominator_field = str(projection.get("denominator_field") or "")
    denominator = (
        _render_field_ref(denominator_field, graph_index, dialect=dialect)
        if denominator_field
        else "*"
    )
    scale = _format_number_literal(projection.get("scale", 1.0))
    cast_type = _rate_cast_type(dialect)
    return (
        f"CAST(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END) AS {cast_type}) "
        f"* {scale} / NULLIF(COUNT({denominator}), 0)"
    )


def _rate_cast_type(dialect: str) -> str:
    normalized = dialect.lower()
    if normalized in {"mysql", "mariadb"}:
        return "DOUBLE"
    if normalized in {"postgres", "postgresql"}:
        return "DOUBLE PRECISION"
    return "REAL"


def _format_number_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1.0" if value else "0.0"
    if isinstance(value, int):
        return f"{value}.0"
    if isinstance(value, float):
        return str(value)
    return "1.0"


def _render_field_ref(field_ref: str, graph_index: JsonObject, *, dialect: str) -> str:
    field = graph_index["fields"][field_ref]
    entity = graph_index["entities"][field["entity"]]
    return (
        f"{_quote_identifier(entity['db_table'], dialect)}."
        f"{_quote_identifier(field['db_column'], dialect)}"
    )


def _quote_identifier(identifier: Any, dialect: str) -> str:
    text = str(identifier)
    if dialect in {"mysql", "mariadb"}:
        return f"`{text.replace('`', '``')}`"
    return f'"{text.replace(chr(34), chr(34) + chr(34))}"'


def _render_literal(value: Any, field: JsonObject) -> str:
    kind = _field_type_kind(str(field.get("type") or "unknown"))
    coerced = _coerce_filter_scalar(value, kind)
    text = str(coerced if coerced is not None else value)
    if kind == "number":
        return text.strip()
    if kind == "boolean":
        return "1" if coerced is True else "0"
    return "'" + text.replace("'", "''") + "'"


def _validate_rendered_sql(sql: str, dialect: str) -> JsonObject | None:
    try:
        validator: Any = import_module("semsql_rewriter.validator")
        validator.validate(sql, validator.ValidationOptions(dialect=dialect))
    except Exception as exc:
        return {
            "level": "error",
            "code": "sql_validator_rejected",
            "message": str(exc),
        }
    return None


def _infer_resolution_result_shape(proposal: JsonObject, graph_index: JsonObject) -> JsonObject:
    group_by = [
        str(field)
        for field in _as_list(proposal.get("group_by"))
        if str(field) in graph_index["fields"]
    ]
    projection_kinds = {
        str(projection.get("kind") or "")
        for projection in _as_list(proposal.get("projections"))
        if isinstance(projection, dict)
    }
    aggregate_like = bool(projection_kinds & {"count", "aggregate", "conditional_rate"})
    field_like = bool(projection_kinds & {"field", "all"})
    group_roles = [_field_role(graph_index["fields"][field]) for field in group_by]
    if group_by:
        has_date = any(role == "date" for role in group_roles)
        has_segment = any(role != "date" for role in group_roles)
        if len(group_by) >= 2 and has_date and has_segment:
            kind = "multi_series_chart"
        elif has_date:
            kind = "time_series_chart"
        else:
            kind = "categorical_chart"
    elif aggregate_like and not field_like:
        kind = "scalar_metric"
    else:
        kind = "table"
    return {
        "kind": kind,
        "group_by": group_by,
        "group_roles": group_roles,
    }


def _proposal_result_shape_issue(
    proposal: JsonObject,
    inferred_shape: JsonObject,
) -> JsonObject | None:
    expected = str(proposal.get("result_shape") or "")
    if not expected:
        return None
    actual = str(inferred_shape.get("kind") or "")
    if expected == actual:
        return None
    return {
        "level": "error",
        "code": "result_shape_mismatch",
        "path": "result_shape",
        "message": f"proposal declared `{expected}` but renders as `{actual}`",
    }


def _query_frame_candidate(
    packet: JsonObject,
    proposal: JsonObject,
    sql: str,
    base_entity: str,
    required_entities: set[str],
    join_plan: list[JsonObject],
    *,
    route_reason: str = "llm_resolution_validated",
    result_shape: JsonObject | None = None,
) -> JsonObject:
    return {
        "schema_version": 1,
        "source": "llm_resolution_query_frame_candidate",
        "question": packet.get("question"),
        "routed": True,
        "used_for_final_sql": False,
        "route_reason": route_reason,
        "sql": sql,
        "projection": {
            "entity": base_entity,
            "field": _first_projection_field(proposal),
            "fields": [
                str(projection["field"])
                for projection in _as_list(proposal.get("projections"))
                if isinstance(projection, dict) and projection.get("field")
            ],
            "expression": "typed_resolution_proposal",
        },
        "predicates": [
            {
                "field": str(filter_item["field"]),
                "value": str(filter_item.get("value") or ""),
                "operator": str(filter_item.get("operator") or "="),
            }
            for filter_item in _as_list(proposal.get("filters"))
            if isinstance(filter_item, dict) and filter_item.get("field")
        ],
        "required_entities": sorted(required_entities),
        "joins": join_plan,
        "group_by": _as_list(proposal.get("group_by")),
        "order_by": _as_list(proposal.get("order_by")),
        "result_shape": result_shape,
    }


def _first_projection_field(proposal: JsonObject) -> str | None:
    for projection in _as_list(proposal.get("projections")):
        if isinstance(projection, dict) and projection.get("field"):
            return str(projection["field"])
    return None


def _packet_allowed_entities(packet: JsonObject) -> set[str]:
    schema_card = packet.get("schema_card", {})
    entities = {
        str(entity.get("name"))
        for entity in schema_card.get("entities", [])
        if isinstance(entity, dict) and entity.get("name")
    }
    for field in _packet_allowed_fields(packet):
        if "." in field:
            entities.add(field.split(".", 1)[0])
    return entities


def _packet_allowed_fields(packet: JsonObject) -> set[str]:
    schema_card = packet.get("schema_card", {})
    fields: set[str] = set()
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("name") or "")
        if not entity_name:
            continue
        for field in entity.get("fields", []):
            if isinstance(field, dict) and field.get("name"):
                fields.add(f"{entity_name}.{field['name']}")
    local_candidates = packet.get("local_candidates", {})
    for key in ("field_hits", "value_dictionary_hits", "sample_value_hits", "enum_value_hits"):
        for hit in local_candidates.get(key, []):
            if isinstance(hit, dict) and hit.get("field"):
                fields.add(str(hit["field"]))
    for hit in local_candidates.get("metric_formula_hits", []):
        if not isinstance(hit, dict):
            continue
        for key in ("numerator_field", "denominator_field"):
            value = hit.get(key)
            if isinstance(value, str) and value:
                fields.add(value)
    for hit in local_candidates.get("metric_catalog_hits", []):
        if not isinstance(hit, dict):
            continue
        for key in ("numerator_field", "denominator_field"):
            value = hit.get(key)
            if isinstance(value, str) and value:
                fields.add(value)
    for hit in local_candidates.get("source_vocabulary_hits", []):
        if (
            isinstance(hit, dict)
            and hit.get("canonical_kind") == "field"
            and hit.get("canonical_value")
        ):
            fields.add(str(hit["canonical_value"]))
    fields.update(_resolution_task_candidate_field_refs(packet))
    return fields


def _packet_allowed_relationships(packet: JsonObject) -> set[tuple[str, str]]:
    relationships: set[tuple[str, str]] = set()
    schema_card = packet.get("schema_card", {})
    for relationship in schema_card.get("relationships", []):
        if not isinstance(relationship, dict):
            continue
        from_field = relationship.get("from")
        to_field = relationship.get("to")
        if from_field and to_field:
            relationships.add((str(from_field), str(to_field)))
    return relationships


def _packet_allowed_value_filters(packet: JsonObject) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    local_candidates = packet.get("local_candidates", {})
    ambiguous_sample_filters: set[tuple[str, str, str]] = set()
    if isinstance(local_candidates, dict):
        for value_entry in local_candidates.get("sample_value_hits", []):
            if (
                isinstance(value_entry, dict)
                and value_entry.get("field")
                and str(value_entry.get("match_type") or "") == "ambiguous_component"
            ):
                ambiguous_sample_filters.add(
                    (
                        str(value_entry["field"]),
                        _normalize_operator(
                            str(value_entry.get("operator") or "="),
                            value_entry.get("raw_value"),
                        ),
                        _normalize_value(value_entry.get("raw_value")),
                    )
                )
    schema_card = packet.get("schema_card", {})
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("name") or "")
        if not entity_name:
            continue
        for field in entity.get("fields", []):
            if not isinstance(field, dict) or not field.get("name"):
                continue
            field_name = f"{entity_name}.{field['name']}"
            for value_entry in field.get("value_dictionary", []):
                if isinstance(value_entry, dict):
                    _add_value_filter(out, field_name, value_entry)
            for sample in field.get("samples", []):
                if sample is not None:
                    candidate = (field_name, "=", _normalize_value(sample))
                    if candidate not in ambiguous_sample_filters:
                        out.add(candidate)
    for value_entry in local_candidates.get("value_dictionary_hits", []):
        if isinstance(value_entry, dict) and value_entry.get("field"):
            _add_value_filter(out, str(value_entry["field"]), value_entry)
    for value_entry in local_candidates.get("sample_value_hits", []):
        if isinstance(value_entry, dict) and value_entry.get("field"):
            if str(value_entry.get("match_type") or "exact") == "ambiguous_component":
                continue
            _add_value_filter(out, str(value_entry["field"]), value_entry)
    for value_entry in local_candidates.get("enum_value_hits", []):
        if isinstance(value_entry, dict) and value_entry.get("field"):
            if bool(value_entry.get("requires_clarification")):
                continue
            _add_value_filter(out, str(value_entry["field"]), value_entry)
    for metric_hit in local_candidates.get("metric_catalog_hits", []):
        if not isinstance(metric_hit, dict):
            continue
        field = str(metric_hit.get("numerator_field") or "")
        raw_value = metric_hit.get("numerator_value")
        if field and raw_value is not None:
            _add_value_filter(
                out,
                field,
                {
                    "operator": metric_hit.get("numerator_operator") or "=",
                    "raw_value": raw_value,
                },
            )
    for binding in _resolution_task_value_bindings(packet):
        operator = _normalize_operator(
            str(binding.get("operator") or "="),
            binding.get("value"),
        )
        for candidate in _as_list(binding.get("candidate_fields")):
            if not isinstance(candidate, dict):
                continue
            field = str(candidate.get("field") or "")
            raw_value = candidate.get("value", binding.get("value"))
            if field and raw_value is not None:
                _add_value_filter(
                    out,
                    field,
                    {
                        "operator": operator,
                        "raw_value": raw_value,
                    },
                )
    return out


def _resolution_task_value_bindings(packet: JsonObject) -> list[Any]:
    task = packet.get("resolution_task")
    if not isinstance(task, dict):
        return []
    if task.get("kind") != "resolve_value_binding":
        return []
    bindings = task.get("unresolved_value_bindings")
    return bindings if isinstance(bindings, list) else []


def _resolution_task_candidate_field_refs(packet: JsonObject) -> set[str]:
    fields: set[str] = set()
    for binding in _resolution_task_value_bindings(packet):
        if not isinstance(binding, dict):
            continue
        selected = binding.get("selected_field")
        if isinstance(selected, str) and selected:
            fields.add(selected)
        for candidate in _as_list(binding.get("candidate_fields")):
            if isinstance(candidate, dict) and isinstance(candidate.get("field"), str):
                fields.add(candidate["field"])
    return fields


def _packet_field_type_map(packet: JsonObject) -> dict[str, str]:
    graph = _packet_graph_rows(packet)
    out = {
        f"{field['entity']}.{field['field']}": str(field.get("type") or "unknown")
        for field in graph["fields"]
    }
    schema_card = packet.get("schema_card", {})
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        for field in entity.get("fields", []):
            if isinstance(field, dict) and field.get("name"):
                field_ref = f"{entity['name']}.{field['name']}"
                card_type = _schema_card_field_type(field)
                if field_ref not in out or _field_type_kind(out[field_ref]) == "text":
                    out[field_ref] = card_type
    return out


def _packet_field_role_map(packet: JsonObject) -> dict[str, str]:
    graph = _packet_graph_rows(packet)
    out = {
        f"{field['entity']}.{field['field']}": _field_role(field)
        for field in graph["fields"]
    }
    schema_card = packet.get("schema_card", {})
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        for field in entity.get("fields", []):
            if isinstance(field, dict) and field.get("name"):
                field_ref = f"{entity['name']}.{field['name']}"
                out[field_ref] = str(field.get("role") or out.get(field_ref) or "unknown")
    return out


def _packet_status_fields(packet: JsonObject) -> set[str]:
    return {
        field
        for field, role in _packet_field_role_map(packet).items()
        if role == "status"
        or bool(_tokens(field.split(".", 1)[-1]) & {"status", "state", "active"})
    }


def _packet_status_value_terms(packet: JsonObject) -> set[str]:
    status_fields = _packet_status_fields(packet)
    terms: set[str] = set()
    schema_card = packet.get("schema_card", {})
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict) or not entity.get("name"):
            continue
        entity_name = str(entity["name"])
        for field in entity.get("fields", []):
            if not isinstance(field, dict) or not field.get("name"):
                continue
            field_ref = f"{entity_name}.{field['name']}"
            if field_ref not in status_fields:
                continue
            for value_entry in field.get("value_dictionary", []):
                if not isinstance(value_entry, dict):
                    continue
                terms.update(_tokens(str(value_entry.get("term") or "")))
                terms.update(_tokens(str(value_entry.get("raw_value") or "")))
    return terms


def _schema_card_field_type(field: JsonObject) -> str:
    role = str(field.get("role") or "").lower()
    if role == "date":
        return "date"
    raw_type = str(field.get("type") or "unknown")
    if role == "status" and _field_type_kind(raw_type) == "boolean":
        return raw_type
    return raw_type


def _filter_type_issue(
    field: str,
    operator: str,
    value: Any,
    field_type: str | None,
    path: str,
) -> JsonObject | None:
    if operator not in {
        "=",
        "!=",
        "<>",
        ">",
        ">=",
        "<",
        "<=",
        "LIKE",
        "IN",
        "NOT IN",
        "BETWEEN",
        "IS NULL",
        "IS NOT NULL",
    }:
        return {
            "level": "error",
            "code": "unsupported_filter_operator",
            "path": f"{path}.operator",
            "message": f"operator `{operator}` is not supported",
        }
    if operator in {"IS NULL", "IS NOT NULL"}:
        return None
    kind = _field_type_kind(field_type)
    if operator == "LIKE" and kind != "text":
        return {
            "level": "error",
            "code": "operator_type_mismatch",
            "path": f"{path}.operator",
            "message": f"LIKE requires a text field, got `{field}` type `{field_type}`",
        }
    if operator in {">", ">=", "<", "<=", "BETWEEN"} and kind not in {
        "number",
        "date",
        "datetime",
        "unknown",
    }:
        return {
            "level": "error",
            "code": "operator_type_mismatch",
            "path": f"{path}.operator",
            "message": f"{operator} requires a comparable field, got `{field}` type `{field_type}`",
        }
    if operator in {"IN", "NOT IN"}:
        if not isinstance(value, list) or len(value) == 0:
            return {
                "level": "error",
                "code": "invalid_filter_value",
                "path": f"{path}.value",
                "message": f"{operator} requires a non-empty value array",
            }
        if not all(_coerce_filter_scalar(item, kind) is not None for item in value):
            return {
                "level": "error",
                "code": "invalid_filter_value",
                "path": f"{path}.value",
                "message": f"{operator} values are incompatible with `{field}` type `{field_type}`",
            }
        return None
    if operator == "BETWEEN":
        if not isinstance(value, list) or len(value) != 2:
            return {
                "level": "error",
                "code": "invalid_filter_value",
                "path": f"{path}.value",
                "message": "BETWEEN requires exactly two values",
            }
        if not all(_coerce_filter_scalar(item, kind) is not None for item in value):
            return {
                "level": "error",
                "code": "invalid_filter_value",
                "path": f"{path}.value",
                "message": f"BETWEEN values are incompatible with `{field}` type `{field_type}`",
            }
        return None
    if isinstance(value, list):
        return {
            "level": "error",
            "code": "invalid_filter_value",
            "path": f"{path}.value",
            "message": f"{operator} requires a scalar value",
        }
    if _coerce_filter_scalar(value, kind) is None:
        return {
            "level": "error",
            "code": "invalid_filter_value",
            "path": f"{path}.value",
            "message": f"value is incompatible with `{field}` type `{field_type}`",
        }
    return None


def _conditional_rate_projection_issues(
    projection: JsonObject,
    path: str,
    allowed_fields: set[str],
    allowed_value_filters: set[tuple[str, str, str]],
    field_type_map: dict[str, str],
    field_role_map: dict[str, str],
) -> list[JsonObject]:
    issues: list[JsonObject] = []
    numerator_field = str(projection.get("numerator_field") or "")
    denominator_field = str(projection.get("denominator_field") or "")
    operator = _normalize_operator(str(projection.get("numerator_operator") or "="))
    value = _normalize_filter_value(operator, projection.get("numerator_value"))
    if numerator_field not in allowed_fields:
        _add_validation_issue(
            issues,
            "unknown_field",
            f"rate numerator field `{numerator_field}` is not in the packet",
            f"{path}.numerator_field",
        )
    else:
        type_issue = _filter_type_issue(
            numerator_field,
            operator,
            value,
            field_type_map.get(numerator_field),
            path,
        )
        if type_issue is not None:
            issues.append(type_issue)
    if denominator_field and denominator_field not in allowed_fields:
        _add_validation_issue(
            issues,
            "unknown_field",
            f"rate denominator field `{denominator_field}` is not in the packet",
            f"{path}.denominator_field",
        )
    evidence_issue = _filter_evidence_issue(
        numerator_field,
        operator,
        value,
        str(projection.get("numerator_value_kind") or ""),
        allowed_value_filters,
        field_type_map.get(numerator_field),
        field_role_map.get(numerator_field),
        path,
        label="rate value filter",
    )
    if evidence_issue is not None:
        issues.append(evidence_issue)
    scale = projection.get("scale", 1.0)
    if scale is None:
        scale = 1.0
    if isinstance(scale, bool) or not isinstance(scale, int | float) or scale <= 0:
        _add_validation_issue(
            issues,
            "invalid_rate_scale",
            "conditional_rate scale must be a positive number",
            f"{path}.scale",
        )
    return issues


def _filter_evidence_issue(
    field: str,
    operator: str,
    value: Any,
    value_kind: str,
    allowed_value_filters: set[tuple[str, str, str]],
    field_type: str | None,
    field_role: str | None,
    path: str,
    *,
    label: str = "value filter",
) -> JsonObject | None:
    normalized_operator = _normalize_operator(operator, value)
    if normalized_operator in {"IS NULL", "IS NOT NULL"}:
        return None
    if _value_filter_backed(field, normalized_operator, value, allowed_value_filters):
        return None
    kind = value_kind.lower()
    requires_explicit_evidence = kind in {"value_dictionary", "scope_predicate", "enum_value"}
    if not requires_explicit_evidence and not _literal_filter_requires_evidence(
        normalized_operator,
        field_type,
        field_role,
    ):
        return None
    return {
        "level": "error",
        "code": "unbacked_value_filter",
        "path": path,
        "message": f"{label} `{field} {normalized_operator} {value}` is not in packet evidence",
    }


def _proposal_route_safety_issues(
    packet: JsonObject,
    proposal: JsonObject,
    action: str,
) -> list[JsonObject]:
    if action != "route":
        return []
    issues: list[JsonObject] = []
    referenced_entities = _proposal_referenced_entities(proposal)
    sensitive_entities = _packet_sensitive_entities(packet)
    for entity in sorted(referenced_entities & sensitive_entities):
        _add_validation_issue(
            issues,
            "sensitive_entity_forbidden",
            f"route proposal references sensitive entity `{entity}`",
            "target_entities",
        )
    sensitive_fields = _packet_sensitive_fields(packet)
    referenced_fields = set(_proposal_field_refs(proposal)) | _proposal_join_field_refs(proposal)
    for field in sorted(referenced_fields & sensitive_fields):
        _add_validation_issue(
            issues,
            "sensitive_field_forbidden",
            f"route proposal references sensitive field `{field}`",
            "fields",
        )
    ambiguous_members = _packet_ambiguous_physical_shard_members(packet)
    ambiguous_touched = referenced_entities & ambiguous_members
    if ambiguous_touched:
        _add_validation_issue(
            issues,
            "ambiguous_shard_family_route",
            (
                "route proposal targets an ambiguous physical shard family: "
                f"{', '.join(sorted(ambiguous_touched))}"
            ),
            "target_entities",
        )
    return issues


def _proposal_referenced_entities(proposal: JsonObject) -> set[str]:
    entities = {
        str(entity)
        for entity in _as_list(proposal.get("target_entities"))
        if entity
    }
    for field in _proposal_field_refs(proposal):
        entity = _field_ref_entity(field)
        if entity:
            entities.add(entity)
    for join in _as_list(proposal.get("joins")):
        if not isinstance(join, dict):
            continue
        for key in ("from_entity", "to_entity"):
            entity = str(join.get(key) or "")
            if entity:
                entities.add(entity)
    return entities


def _proposal_join_field_refs(proposal: JsonObject) -> set[str]:
    fields: set[str] = set()
    for join in _as_list(proposal.get("joins")):
        if not isinstance(join, dict):
            continue
        from_entity = str(join.get("from_entity") or "")
        to_entity = str(join.get("to_entity") or "")
        from_field = _join_field_name(from_entity, str(join.get("from_field") or ""))
        to_field = _join_field_name(to_entity, str(join.get("to_field") or ""))
        if from_entity and from_field:
            fields.add(f"{from_entity}.{from_field}")
        if to_entity and to_field:
            fields.add(f"{to_entity}.{to_field}")
    return fields


def _packet_sensitive_entities(packet: JsonObject) -> set[str]:
    graph = _packet_graph_rows(packet)
    fields_by_entity: dict[str, list[JsonObject]] = defaultdict(list)
    for field in graph.get("fields", []):
        if isinstance(field, dict):
            fields_by_entity[str(field.get("entity") or "")].append(field)
    sensitive = {
        str(entity.get("canonical_name") or "")
        for entity in graph.get("entities", [])
        if isinstance(entity, dict)
        and entity.get("canonical_name")
        and _entity_is_sensitive(entity, fields_by_entity[str(entity["canonical_name"])])
    }
    schema_card = packet.get("schema_card", {})
    for entity in schema_card.get("entities", []):
        if isinstance(entity, dict) and entity.get("sensitive") and entity.get("name"):
            sensitive.add(str(entity["name"]))
    return sensitive


def _packet_sensitive_fields(packet: JsonObject) -> set[str]:
    sensitive_entities = _packet_sensitive_entities(packet)
    graph = _packet_graph_rows(packet)
    sensitive: set[str] = set()
    for field in graph.get("fields", []):
        if not isinstance(field, dict):
            continue
        entity = str(field.get("entity") or "")
        name = str(field.get("field") or "")
        if not entity or not name:
            continue
        field_ref = f"{entity}.{name}"
        field_tokens = _tokens(f"{name} {field.get('db_column') or ''}")
        if entity in sensitive_entities or field_tokens & SENSITIVE_FIELD_TOKENS:
            sensitive.add(field_ref)
    return sensitive


def _packet_ambiguous_physical_shard_members(packet: JsonObject) -> set[str]:
    members: set[str] = set()
    candidates = packet.get("local_candidates", {})
    for family in _as_list(candidates.get("ambiguous_physical_families_mentioned")):
        if not isinstance(family, dict):
            continue
        family_members = _physical_family_member_entities(family)
        if not family_members:
            family_members = _schema_card_physical_family_members_for_base(
                packet.get("schema_card", {}),
                str(family.get("base_table") or family.get("base") or ""),
            )
        members.update(family_members)
    if not members and str(packet.get("route_reason") or "").startswith(
        "not_routed_ambiguous_physical"
    ):
        schema_card = packet.get("schema_card", {})
        if isinstance(schema_card, dict):
            members.update(_schema_card_physical_family_members(schema_card))
    return members


def _schema_card_physical_family_members_for_base(
    schema_card: Any,
    base: str,
) -> list[str]:
    if not isinstance(schema_card, dict) or not base:
        return []
    for family in _schema_card_physical_families(schema_card):
        family_base = str(family.get("base_table") or family.get("base") or "")
        if family_base == base:
            return _physical_family_member_entities(family)
    return []


def _proposal_limit_issue(proposal: JsonObject) -> JsonObject | None:
    limit = proposal.get("limit")
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        return {
            "level": "error",
            "code": "invalid_limit",
            "path": "limit",
            "message": "limit must be a non-negative integer or null",
        }
    return None


def _proposal_limit_clause(proposal: JsonObject) -> tuple[str, JsonObject | None]:
    limit_issue = _proposal_limit_issue(proposal)
    if limit_issue is not None:
        return "", limit_issue
    limit = proposal.get("limit")
    if isinstance(limit, int) and limit > 0:
        capped = min(limit, MAX_FALLBACK_ROW_LIMIT)
        issue = None
        if limit > MAX_FALLBACK_ROW_LIMIT:
            issue = {
                "level": "warning",
                "code": "limit_capped",
                "path": "limit",
                "message": (
                    f"fallback row limit capped from {limit} "
                    f"to {MAX_FALLBACK_ROW_LIMIT}"
                ),
            }
        return f"LIMIT {capped}", issue
    if _proposal_needs_default_row_limit(proposal):
        return (
            f"LIMIT {DEFAULT_FALLBACK_ROW_LIMIT}",
            {
                "level": "warning",
                "code": "default_row_limit_applied",
                "path": "limit",
                "message": (
                    "fallback row-list route had no positive limit; local "
                    f"default {DEFAULT_FALLBACK_ROW_LIMIT} applied"
                ),
            },
        )
    return "", None


def _proposal_needs_default_row_limit(proposal: JsonObject) -> bool:
    if _as_list(proposal.get("group_by")):
        return False
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        if str(projection.get("kind") or "") in {"field", "all"}:
            return True
    return False


def _value_filter_backed(
    field: str,
    operator: str,
    value: Any,
    allowed_value_filters: set[tuple[str, str, str]],
) -> bool:
    if operator in {"IN", "NOT IN"} and isinstance(value, list):
        return all(
            (field, "=", _normalize_value(item)) in allowed_value_filters
            for item in value
        )
    return (field, operator, _normalize_value(value)) in allowed_value_filters


def _literal_filter_requires_evidence(
    operator: str,
    field_type: str | None,
    field_role: str | None,
) -> bool:
    if operator not in {"=", "<>", "IN", "NOT IN"}:
        return False
    if (field_role or "").lower() in {"date", "datetime"}:
        return False
    return _field_type_kind(field_type) == "text"


def _proposal_output_aliases(proposal: JsonObject) -> set[str]:
    aliases: set[str] = set()
    for projection in _as_list(proposal.get("projections")):
        if not isinstance(projection, dict):
            continue
        explicit = str(projection.get("alias") or "")
        if explicit:
            aliases.add(explicit)
            continue
        kind = str(projection.get("kind") or "")
        aggregate = str(projection.get("aggregate") or "").lower()
        if kind == "count":
            aliases.add("count")
        elif kind == "aggregate" and aggregate:
            aliases.add(aggregate)
        elif kind == "conditional_rate":
            aliases.add("rate")
    return aliases


def _alias_issue(alias: str, path: str) -> JsonObject | None:
    if not alias:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", alias):
        return {
            "level": "error",
            "code": "invalid_alias",
            "path": path,
            "message": f"alias `{alias}` must be a simple identifier",
        }
    return None


def _aggregate_order_issue(
    aggregate: str,
    field: str,
    path: str,
    *,
    has_alias: bool = False,
) -> JsonObject | None:
    if not aggregate:
        if not field and not has_alias:
            return {
                "level": "error",
                "code": "missing_order_expression",
                "path": path,
                "message": "order_by requires a field or aggregate",
            }
        return None
    if aggregate not in {"AVG", "COUNT", "MAX", "MIN", "SUM"}:
        return {
            "level": "error",
            "code": "unsupported_aggregate_order",
            "path": f"{path}.aggregate",
            "message": f"unsupported order aggregate `{aggregate}`",
        }
    if not field and aggregate != "COUNT":
        return {
            "level": "error",
            "code": "missing_aggregate_field",
            "path": f"{path}.field",
            "message": f"{aggregate} order requires a packet-backed field",
        }
    return None


def _field_type_kind(field_type: str | None) -> str:
    raw = (field_type or "unknown").lower()
    if any(token in raw for token in ("int", "real", "float", "double", "decimal", "numeric")):
        return "number"
    if "bool" in raw:
        return "boolean"
    if any(token in raw for token in ("timestamp", "datetime")):
        return "datetime"
    if re.search(r"\bdate\b", raw):
        return "date"
    if raw in {"unknown", ""}:
        return "unknown"
    return "text"


def _coerce_filter_scalar(value: Any, kind: str) -> Any | None:
    if value is None:
        return None
    if kind == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, int | float):
            return value
        text = str(value).strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            return text
        return None
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
        return None
    if kind in {"date", "datetime"}:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", text):
            return text
        return None
    if isinstance(value, list | dict):
        return None
    return str(value)


def _add_value_filter(
    out: set[tuple[str, str, str]],
    field: str,
    value_entry: JsonObject,
) -> None:
    out.add(
        (
            field,
            _normalize_operator(
                str(value_entry.get("operator") or "="),
                value_entry.get("raw_value"),
            ),
            _normalize_value(value_entry.get("raw_value")),
        )
    )


def _normalize_operator(operator: str, value: Any = None) -> str:
    normalized = operator.strip().upper()
    if value is None and normalized in {"IS", "IS NULL"}:
        return "IS NULL"
    if value is None and normalized in {"IS NOT", "NOT NULL", "IS NOT NULL"}:
        return "IS NOT NULL"
    if normalized == "!=":
        return "<>"
    if normalized == "NOT_IN":
        return "NOT IN"
    if normalized in {"RANGE", "DATE_RANGE", "DATE RANGE"}:
        return "BETWEEN"
    return normalized


def _normalize_filter_value(operator: str, value: Any) -> Any:
    if operator != "BETWEEN" or not isinstance(value, str):
        return value
    stripped = value.strip()
    if ".." in stripped:
        left, right = stripped.split("..", 1)
        return [left.strip(), right.strip()]
    match = re.fullmatch(r"(.+?)\s+to\s+(.+)", stripped, flags=re.IGNORECASE)
    if match is not None:
        return [match.group(1).strip(), match.group(2).strip()]
    return value


def _normalize_value(value: Any) -> str:
    return str(value).strip().lower()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _add_validation_issue(
    issues: list[JsonObject],
    code: str,
    message: str,
    path: str,
) -> None:
    issues.append(
        {
            "level": "error",
            "code": code,
            "path": path,
            "message": message,
        }
    )


def _proposal_sql_fragments(value: Any, path: str = "$") -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            fragments.extend(_proposal_sql_fragments(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            fragments.extend(_proposal_sql_fragments(child, f"{path}[{idx}]"))
    elif isinstance(value, str):
        match = re.search(
            r"\b(select\b.+\bfrom|insert\b.+\binto|update\b.+\bset|"
            r"delete\b.+\bfrom|drop\b.+\btable|alter\b.+\btable)\b",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match is not None:
            fragments.append((path, match.group(0)[:120]))
    return fragments


def _read_graph_rows(graph_path: Path) -> JsonObject:
    conn = sqlite3.connect(f"file:{graph_path.resolve()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        entities = [
            dict(row)
            for row in conn.execute(
                "SELECT canonical_name, db_table, singular_label, plural_label "
                "FROM entities ORDER BY canonical_name"
            ).fetchall()
        ]
        fields = [
            dict(row)
            for row in conn.execute(
                "SELECT entity, field, db_column, type, display_label "
                "FROM fields ORDER BY entity, field"
            ).fetchall()
        ]
        relationships = [
            dict(row)
            for row in conn.execute(
                "SELECT from_entity, from_field, to_entity, to_field, kind "
                "FROM relationships ORDER BY from_entity, to_entity, from_field"
            ).fetchall()
        ]
        sample_values = {
            str(row["field_canonical"]): json.loads(str(row["examples"]))
            for row in conn.execute(
                "SELECT field_canonical, examples FROM sample_values "
                "WHERE pii_redacted = 0 ORDER BY field_canonical"
            ).fetchall()
        }
        vocabulary = []
        if _table_exists(conn, "vocabulary"):
            vocabulary = [
                dict(row)
                for row in conn.execute(
                    "SELECT term, canonical_kind, canonical_value, confidence, source_layer "
                    "FROM vocabulary ORDER BY term, canonical_kind, canonical_value"
                ).fetchall()
            ]
        metric_definitions = _read_metric_definitions(conn)
    finally:
        conn.close()
    return {
        "entities": entities,
        "fields": fields,
        "relationships": relationships,
        "sample_values": sample_values,
        "vocabulary": vocabulary,
        "metric_definitions": metric_definitions,
    }


def _read_metric_definitions(conn: sqlite3.Connection) -> list[JsonObject]:
    if not _table_exists(conn, "metric_definitions"):
        return []
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(metric_definitions)").fetchall()
    }
    required = {
        "name",
        "metric_kind",
        "subject_entity",
        "numerator_field",
        "numerator_operator",
        "numerator_value",
        "denominator_field",
    }
    if not required <= columns:
        return []
    select_columns = [
        "name",
        "metric_kind",
        "subject_entity",
        "numerator_field",
        "numerator_operator",
        "numerator_value",
        "denominator_field",
    ]
    optional_defaults = {
        "display_label": "''",
        "numerator_value_kind": "'literal'",
        "scale": "100.0",
        "required_entities_json": "'[]'",
        "aliases_json": "'[]'",
        "measure_field": "NULL",
        "aggregate": "NULL",
        "distinct_measure": "0",
    }
    for column, default in optional_defaults.items():
        select_columns.append(column if column in columns else f"{default} AS {column}")
    rows = conn.execute(
        "SELECT "
        + ", ".join(select_columns)
        + " FROM metric_definitions ORDER BY name"
    ).fetchall()
    out: list[JsonObject] = []
    for row in rows:
        item = dict(row)
        aliases = _json_string_list(item.pop("aliases_json", "[]"))
        required_entities = _json_string_list(item.pop("required_entities_json", "[]"))
        item["aliases"] = aliases
        item["required_entities"] = required_entities
        item["scale"] = float(item.get("scale") or 100.0)
        item["distinct"] = _boolish(item.get("distinct_measure"))
        out.append(item)
    return out


def _json_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if str(item).strip()]


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _field_summary(
    field: JsonObject,
    sample_values: dict[str, list[Any]],
    max_sample_values: int,
    value_dictionary_by_field: dict[str, list[JsonObject]],
    max_value_dictionary_terms: int,
) -> JsonObject:
    canonical = f"{field['entity']}.{field['field']}"
    examples = sample_values.get(canonical, [])[:max_sample_values]
    value_dictionary = value_dictionary_by_field.get(canonical, [])[
        :max_value_dictionary_terms
    ]
    return {
        "name": field["field"],
        "db_column": field["db_column"],
        "type": field["type"],
        "display_label": field.get("display_label"),
        "role": _field_role(field),
        "samples": examples,
        "value_dictionary": value_dictionary,
    }


def _schema_card_entity_summary(
    entity: JsonObject,
    entity_fields: list[JsonObject],
    sample_values: dict[str, list[Any]],
    value_dictionary_by_field: dict[str, list[JsonObject]],
    *,
    max_fields_per_entity: int,
    max_sample_values: int,
    max_value_dictionary_terms: int,
    shard_entity_names: set[str],
    priority_field_refs: set[str],
    table_activity_hints: dict[str, JsonObject],
) -> JsonObject:
    name = str(entity["canonical_name"])
    selected_fields = _schema_card_selected_fields(
        name,
        entity_fields,
        max_fields_per_entity,
        priority_field_refs,
    )
    summarized_fields = [
        _field_summary(
            field,
            sample_values,
            max_sample_values,
            value_dictionary_by_field,
            max_value_dictionary_terms,
        )
        for field in selected_fields
    ]
    display_fields = [
        str(field["field"]) for field in entity_fields if _field_role(field) == "display"
    ][:8]
    id_fields = [str(field["field"]) for field in entity_fields if _field_role(field) == "id"][:8]
    date_fields = [
        str(field["field"]) for field in entity_fields if _field_role(field) == "date"
    ][:8]
    status_fields = [
        str(field["field"]) for field in entity_fields if _field_role(field) == "status"
    ][:8]
    numeric_fields = [
        str(field["field"]) for field in entity_fields if _field_role(field) == "numeric"
    ][:8]
    return {
        "name": name,
        "db_table": entity["db_table"],
        "labels": [
            label
            for label in (entity.get("singular_label"), entity.get("plural_label"))
            if label
        ],
        "field_count": len(entity_fields),
        "fields": summarized_fields,
        "truncated_fields": max(0, len(entity_fields) - len(summarized_fields)),
        "display_fields": display_fields,
        "id_fields": id_fields,
        "date_fields": date_fields,
        "status_fields": status_fields,
        "numeric_fields": numeric_fields,
        "sensitive": _entity_is_sensitive(entity, entity_fields),
        "physical_shard_member": name in shard_entity_names,
        "table_activity_hint": table_activity_hints.get(
            name,
            _empty_table_activity_hint(),
        ),
    }


def _schema_card_selected_fields(
    entity_name: str,
    entity_fields: list[JsonObject],
    max_fields_per_entity: int,
    priority_field_refs: set[str],
) -> list[JsonObject]:
    sorted_fields = sorted(entity_fields, key=lambda row: str(row.get("field") or ""))
    if not priority_field_refs:
        return sorted_fields[:max_fields_per_entity]
    selected: list[JsonObject] = []
    seen: set[str] = set()
    for prefer_priority in (True, False):
        for field in sorted_fields:
            field_name = str(field.get("field") or "")
            field_ref = f"{entity_name}.{field_name}"
            if field_ref in seen:
                continue
            if prefer_priority != (field_ref in priority_field_refs):
                continue
            selected.append(field)
            seen.add(field_ref)
            if len(selected) >= max_fields_per_entity:
                return selected
    return selected


def _schema_card_relationship_summary(row: JsonObject) -> JsonObject:
    return {
        "from": f"{row['from_entity']}.{row['from_field']}",
        "to": f"{row['to_entity']}.{row['to_field']}",
        "kind": row.get("kind", "relationship"),
    }


def _metric_definition_summaries(
    metric_definitions: list[JsonObject],
    *,
    max_definitions: int = 40,
) -> list[JsonObject]:
    out: list[JsonObject] = []
    for metric in metric_definitions[:max_definitions]:
        if not isinstance(metric, dict):
            continue
        out.append(
            {
                "name": str(metric.get("name") or ""),
                "display_label": str(metric.get("display_label") or ""),
                "metric_kind": str(metric.get("metric_kind") or ""),
                "subject_entity": str(metric.get("subject_entity") or ""),
                "numerator_field": str(metric.get("numerator_field") or ""),
                "numerator_operator": str(metric.get("numerator_operator") or "="),
                "numerator_value": metric.get("numerator_value"),
                "numerator_value_kind": str(
                    metric.get("numerator_value_kind") or "literal"
                ),
                "denominator_field": str(metric.get("denominator_field") or ""),
                "scale": float(metric.get("scale") or 100.0),
                "measure_field": (
                    str(metric.get("measure_field"))
                    if metric.get("measure_field") is not None
                    else None
                ),
                "aggregate": (
                    str(metric.get("aggregate")).upper()
                    if metric.get("aggregate") is not None
                    else None
                ),
                "distinct": _boolish(
                    metric.get("distinct") or metric.get("distinct_measure")
                ),
                "required_entities": [
                    str(entity)
                    for entity in _as_list(metric.get("required_entities"))
                    if str(entity).strip()
                ],
                "aliases": [
                    str(alias)
                    for alias in _as_list(metric.get("aliases"))
                    if str(alias).strip()
                ][:12],
            }
        )
    return out


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _scope_predicates_by_field(
    vocabulary: list[JsonObject],
    field_names: set[str],
    max_per_field: int,
) -> dict[str, list[JsonObject]]:
    allowed_operators = {"=", "!=", "<>", ">", ">=", "<", "<=", "LIKE", "IN"}
    out: dict[str, list[JsonObject]] = defaultdict(list)
    seen: set[tuple[str, str, str, str]] = set()
    for row in vocabulary:
        if row.get("canonical_kind") != "scope_predicate":
            continue
        term = str(row.get("term") or "").strip()
        if not term:
            continue
        try:
            payload = json.loads(str(row.get("canonical_value") or "{}"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        field = str(payload.get("field") or "")
        if field not in field_names:
            continue
        operator = str(payload.get("operator") or "=").upper()
        if operator not in allowed_operators:
            continue
        raw_value = payload.get("rawValue", payload.get("raw_value"))
        if raw_value is None:
            continue
        scope = str(payload.get("scope") or "")
        key = (field, term.lower(), operator, str(raw_value))
        if key in seen:
            continue
        seen.add(key)
        out[field].append(
            {
                "term": term,
                "operator": operator,
                "raw_value": raw_value,
                "scope": scope,
                "confidence": float(row.get("confidence") or 0.0),
                "source_layer": int(row.get("source_layer") or 0),
            }
        )
    for field, entries in out.items():
        entries.sort(
            key=lambda entry: (
                -float(entry["confidence"]),
                -len(str(entry["term"])),
                str(entry["term"]),
            )
        )
        out[field] = entries[:max_per_field]
    return dict(out)


def _field_role(field: JsonObject) -> str:
    name = f"{field['field']} {field['db_column']} {field.get('display_label') or ''}".lower()
    field_type = str(field["type"]).lower()
    if re.search(r"(^|_)id$|uuid|code|number", name):
        return "id"
    if _field_type_kind(field_type) in {"date", "datetime"}:
        return "date"
    if any(
        token in name
        for token in (
            "date",
            "time",
            "created",
            "updated",
            "sent",
            "read",
            "ordered",
            "joined",
            "posted",
            "scheduled",
            "expires",
            "expired",
            "due",
        )
    ):
        return "date"
    if any(token in name for token in ("status", "state", "active", "enabled", "deleted")):
        return "status"
    if any(token in field_type for token in ("int", "real", "float", "decimal", "numeric")):
        return "numeric"
    if any(token in name for token in ("name", "title", "subject", "email", "symbol")):
        return "display"
    return "attribute"


def _entity_is_sensitive(entity: JsonObject, fields: list[JsonObject]) -> bool:
    table_tokens = _tokens(str(entity["canonical_name"])) | _tokens(str(entity["db_table"]))
    if table_tokens & SENSITIVE_TABLE_TOKENS:
        return True
    return any(_tokens(f"{field['field']} {field['db_column']}") & SENSITIVE_FIELD_TOKENS for field in fields)


def _empty_table_activity_hint() -> JsonObject:
    return {
        "evidence_source": "graph_metadata_only",
        "evidence_score": 0,
        "evidence_level": "none",
        "sample_value_field_count": 0,
        "sample_value_count": 0,
        "value_dictionary_field_count": 0,
        "value_dictionary_term_count": 0,
        "relationship_count": 0,
        "display_field_count": 0,
        "date_field_count": 0,
        "status_field_count": 0,
        "numeric_field_count": 0,
        "physical_family_role": None,
        "evidence": [],
        "approx_rows": None,
        "row_count_source": None,
    }


def _table_activity_hints(
    entities: list[JsonObject],
    fields: list[JsonObject],
    relationships: list[JsonObject],
    sample_values: dict[str, list[Any]],
    value_dictionary_by_field: dict[str, list[JsonObject]],
    physical_table_families: list[JsonObject],
) -> dict[str, JsonObject]:
    fields_by_entity: dict[str, list[JsonObject]] = defaultdict(list)
    for field in fields:
        fields_by_entity[str(field.get("entity") or "")].append(field)

    sample_fields_by_entity: dict[str, int] = defaultdict(int)
    sample_values_by_entity: dict[str, int] = defaultdict(int)
    for field_ref, examples in sample_values.items():
        sample_entity = _field_ref_entity(str(field_ref))
        if not sample_entity:
            continue
        sample_fields_by_entity[sample_entity] += 1
        sample_values_by_entity[sample_entity] += len(_as_list(examples))

    dictionary_fields_by_entity: dict[str, int] = defaultdict(int)
    dictionary_terms_by_entity: dict[str, int] = defaultdict(int)
    for field_ref, entries in value_dictionary_by_field.items():
        dictionary_entity = _field_ref_entity(str(field_ref))
        if not dictionary_entity:
            continue
        dictionary_fields_by_entity[dictionary_entity] += 1
        dictionary_terms_by_entity[dictionary_entity] += len(entries)

    relationship_count_by_entity: dict[str, int] = defaultdict(int)
    for relationship in relationships:
        from_entity = str(relationship.get("from_entity") or "")
        to_entity = str(relationship.get("to_entity") or "")
        if from_entity:
            relationship_count_by_entity[from_entity] += 1
        if to_entity and to_entity != from_entity:
            relationship_count_by_entity[to_entity] += 1

    physical_roles = _physical_family_roles(physical_table_families)
    out: dict[str, JsonObject] = {}
    for graph_entity in entities:
        entity_name = str(graph_entity.get("canonical_name") or "")
        if not entity_name:
            continue
        role_counts = _table_activity_role_counts(fields_by_entity.get(entity_name, []))
        sample_field_count = sample_fields_by_entity[entity_name]
        sample_value_count = sample_values_by_entity[entity_name]
        dictionary_field_count = dictionary_fields_by_entity[entity_name]
        dictionary_term_count = dictionary_terms_by_entity[entity_name]
        relationship_count = relationship_count_by_entity[entity_name]
        physical_role = physical_roles.get(entity_name)
        score = (
            sample_field_count * 4
            + min(sample_value_count, 12)
            + dictionary_field_count * 3
            + min(dictionary_term_count, 12)
            + relationship_count * 2
            + role_counts["display"]
            + role_counts["date"]
            + role_counts["status"]
            + role_counts["numeric"]
            + (1 if physical_role == "base_table" else 0)
        )
        evidence = _table_activity_evidence(
            sample_field_count,
            sample_value_count,
            dictionary_field_count,
            dictionary_term_count,
            relationship_count,
            role_counts,
            physical_role,
        )
        out[entity_name] = {
            "evidence_source": "graph_metadata_only",
            "evidence_score": score,
            "evidence_level": _table_activity_evidence_level(score),
            "sample_value_field_count": sample_field_count,
            "sample_value_count": sample_value_count,
            "value_dictionary_field_count": dictionary_field_count,
            "value_dictionary_term_count": dictionary_term_count,
            "relationship_count": relationship_count,
            "display_field_count": role_counts["display"],
            "date_field_count": role_counts["date"],
            "status_field_count": role_counts["status"],
            "numeric_field_count": role_counts["numeric"],
            "physical_family_role": physical_role,
            "evidence": evidence,
            "approx_rows": None,
            "row_count_source": None,
        }
    return out


def _table_activity_role_counts(fields: list[JsonObject]) -> dict[str, int]:
    counts = {"display": 0, "date": 0, "status": 0, "numeric": 0}
    for field in fields:
        role = _field_role(field)
        if role in counts:
            counts[role] += 1
    return counts


def _table_activity_evidence(
    sample_field_count: int,
    sample_value_count: int,
    dictionary_field_count: int,
    dictionary_term_count: int,
    relationship_count: int,
    role_counts: dict[str, int],
    physical_role: str | None,
) -> list[str]:
    evidence = []
    if sample_field_count or sample_value_count:
        evidence.append(
            f"sample_values:{sample_field_count} fields/{sample_value_count} values"
        )
    if dictionary_field_count or dictionary_term_count:
        evidence.append(
            "value_dictionary:"
            f"{dictionary_field_count} fields/{dictionary_term_count} terms"
        )
    if relationship_count:
        evidence.append(f"relationships:{relationship_count}")
    role_bits = [
        f"{role}={count}"
        for role, count in role_counts.items()
        if count
    ]
    if role_bits:
        evidence.append(f"roles:{','.join(role_bits)}")
    if physical_role:
        evidence.append(f"physical_family:{physical_role}")
    return evidence


def _table_activity_evidence_level(score: int) -> str:
    if score <= 0:
        return "none"
    if score <= 4:
        return "weak"
    if score <= 12:
        return "moderate"
    return "strong"


def _physical_family_roles(
    physical_table_families: list[JsonObject],
) -> dict[str, str]:
    roles = {}
    for family in physical_table_families:
        for member in _as_list(family.get("members")):
            if not isinstance(member, dict):
                continue
            entity = str(member.get("entity") or "")
            role = str(member.get("role") or "")
            if entity and role:
                roles[entity] = role
    return roles


def _attach_table_activity_hints_to_physical_families(
    physical_table_families: list[JsonObject],
    table_activity_hints: dict[str, JsonObject],
) -> None:
    for family in physical_table_families:
        for member in _as_list(family.get("members")):
            if not isinstance(member, dict):
                continue
            entity = str(member.get("entity") or "")
            hint = table_activity_hints.get(entity)
            if hint is None:
                continue
            member["table_activity_hint"] = {
                "evidence_source": hint["evidence_source"],
                "evidence_score": hint["evidence_score"],
                "evidence_level": hint["evidence_level"],
                "sample_value_field_count": hint["sample_value_field_count"],
                "value_dictionary_field_count": hint["value_dictionary_field_count"],
                "relationship_count": hint["relationship_count"],
                "approx_rows": None,
                "row_count_source": None,
            }


def _detect_physical_table_families(entities: list[JsonObject]) -> list[JsonObject]:
    entity_by_canonical = {
        str(entity["canonical_name"]).lower(): entity
        for entity in entities
    }
    canonical_by_table = {
        str(entity["db_table"]).lower(): str(entity["canonical_name"]).lower()
        for entity in entities
    }
    canonical_names = {str(entity["canonical_name"]).lower() for entity in entities}
    by_family: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entity in entities:
        parsed = _parse_shard_table(str(entity["db_table"]))
        if parsed is None:
            continue
        base, anchor = parsed
        by_family[(base, anchor)].append(str(entity["canonical_name"]).lower())
    families = []
    for (base, anchor), members in sorted(by_family.items()):
        base_entity = canonical_by_table.get(base)
        if base_entity is None and base in canonical_names:
            base_entity = base
        if base_entity is not None and base_entity not in members:
            members.append(base_entity)
        if len(set(members)) < 2:
            continue
        member_cards = []
        for member_name in sorted(set(members)):
            graph_entity = entity_by_canonical.get(member_name)
            if graph_entity is None:
                continue
            role = (
                "base_table"
                if str(graph_entity["db_table"]).lower() == base
                else "physical_partition"
            )
            member_cards.append(
                {
                    "entity": str(graph_entity["canonical_name"]),
                    "db_table": str(graph_entity["db_table"]),
                    "role": role,
                }
            )
        member_cards.sort(
            key=lambda member: (
                0 if member["role"] == "base_table" else 1,
                str(member["db_table"]),
            )
        )
        families.append(
            {
                "base_table": base,
                "anchor": anchor,
                "member_count": len(member_cards),
                "members": member_cards,
                "requires_clarification": True,
                "resolution_hint": (
                    "multiple physical tables look like one logical table family; "
                    "choose only with app metadata, user clarification, or an explicit metric/table catalog"
                ),
            }
        )
    return families


def _legacy_shard_family_cards(physical_families: list[JsonObject]) -> list[JsonObject]:
    out = []
    for family in physical_families:
        members = [
            str(member.get("entity") or "")
            for member in _as_list(family.get("members"))
            if isinstance(member, dict) and member.get("entity")
        ]
        out.append(
            {
                "base": str(family.get("base_table") or ""),
                "anchor": str(family.get("anchor") or ""),
                "member_entities": members,
                "ambiguous_without_anchor": len(members) > 1,
            }
        )
    return out


def _parse_shard_table(table: str) -> tuple[str, str] | None:
    lower = table.lower()
    for anchor in DEFAULT_SHARD_ANCHORS:
        marker = f"_{anchor}_"
        if marker not in lower:
            continue
        base, suffix = lower.rsplit(marker, 1)
        if base and suffix.isdigit():
            return base, anchor
    return None


def _local_candidates(
    question: str,
    schema_card: JsonObject,
    *,
    graph_rows: JsonObject | None = None,
    include_samples: bool = False,
) -> JsonObject:
    question_tokens = _tokens(question)
    entity_hits = []
    field_hits = []
    value_dictionary_hits = []
    source_vocabulary_hits = (
        _source_vocabulary_hits(question, graph_rows) if graph_rows is not None else []
    )
    enum_value_hits = (
        _enum_value_hits(question, graph_rows) if graph_rows is not None else []
    )
    for entity_name, labels in _candidate_entities(schema_card, graph_rows):
        label_tokens = set().union(*(_tokens(str(label)) for label in labels if label))
        if question_tokens & label_tokens:
            entity_hits.append(
                {
                    "entity": entity_name,
                    "matched_tokens": sorted(question_tokens & label_tokens),
                }
            )
    for entity_name, field in _candidate_fields(schema_card, graph_rows):
        field_tokens = _tokens(
            f"{field['name']} {field['db_column']} {field.get('display_label') or ''}"
        )
        if question_tokens & field_tokens:
            field_hits.append(
                {
                    "field": f"{entity_name}.{field['name']}",
                    "role": field["role"],
                    "matched_tokens": sorted(question_tokens & field_tokens),
                }
            )
        for value_entry in field.get("value_dictionary", []):
            matched_tokens = _value_dictionary_match_tokens(
                question_tokens,
                str(value_entry.get("term") or ""),
            )
            if not matched_tokens:
                continue
            value_dictionary_hits.append(
                {
                    "field": f"{entity_name}.{field['name']}",
                    "term": value_entry["term"],
                    "operator": value_entry["operator"],
                    "raw_value": value_entry["raw_value"],
                    "scope": value_entry["scope"],
                    "matched_tokens": matched_tokens,
                    "confidence": value_entry["confidence"],
                }
            )
    ambiguous_families = _mentioned_physical_table_families(schema_card, question_tokens)
    seed_entities: set[str] = set()
    if graph_rows is not None:
        seed_entities, _seed_fields = _question_schema_seed_refs(question, graph_rows)
    date_window = _date_window_candidate(question, graph_rows, seed_entities)
    scope_path_candidates = _scope_path_candidates(question, graph_rows, seed_entities)
    sample_value_hits = (
        _sample_value_hits(question, graph_rows)
        if include_samples and graph_rows is not None
        else []
    )
    metric_formula_hits = (
        _metric_formula_hits(
            question,
            graph_rows,
            value_dictionary_hits,
            enum_value_hits,
            sample_value_hits,
        )
        if graph_rows is not None
        else []
    )
    metric_catalog_hits = (
        _metric_catalog_hits(question, graph_rows)
        if graph_rows is not None
        else []
    )
    return {
        "entity_hits": entity_hits[:20],
        "field_hits": field_hits[:40],
        "value_dictionary_hits": value_dictionary_hits[:40],
        "sample_value_hits": sample_value_hits,
        "source_vocabulary_hits": source_vocabulary_hits,
        "enum_value_hits": enum_value_hits,
        "metric_catalog_hits": metric_catalog_hits,
        "metric_catalog_ambiguous": len(metric_catalog_hits) > 1,
        "metric_formula_hits": metric_formula_hits,
        "metric_formula_ambiguous": len(metric_formula_hits) > 1,
        "ambiguous_physical_families_mentioned": ambiguous_families,
        "date_window": date_window,
        "scope_path_candidates": scope_path_candidates,
        "scope_path_ambiguous": len(scope_path_candidates) > 1,
    }


def _mentioned_physical_table_families(
    schema_card: JsonObject,
    question_tokens: set[str],
) -> list[JsonObject]:
    hits = []
    for family in _schema_card_physical_families(schema_card):
        base = str(family.get("base_table") or family.get("base") or "")
        base_tokens = _tokens(base)
        if not (question_tokens & base_tokens):
            continue
        member_entities = _physical_family_member_entities(family)
        if len(member_entities) <= 1:
            continue
        hit = {
            "base_table": base,
            "base": base,
            "anchor": str(family.get("anchor") or ""),
            "member_count": len(member_entities),
            "member_entities": member_entities,
            "matched_tokens": sorted(question_tokens & base_tokens),
            "requires_clarification": True,
            "resolution_hint": (
                "do not pick a physical partition from the base word alone; "
                "use app metadata, a table catalog, or ask which partition/scope is intended"
            ),
        }
        members = _as_list(family.get("members"))
        if members:
            hit["members"] = members
        hits.append(hit)
    return hits


def _schema_card_physical_families(schema_card: JsonObject) -> list[JsonObject]:
    physical = [
        family
        for family in _as_list(schema_card.get("physical_table_families"))
        if isinstance(family, dict)
    ]
    if physical:
        return physical
    return [
        family
        for family in _as_list(schema_card.get("shard_families"))
        if isinstance(family, dict)
    ]


def _physical_family_member_entities(family: JsonObject) -> list[str]:
    members = [
        str(member.get("entity") or "")
        for member in _as_list(family.get("members"))
        if isinstance(member, dict) and member.get("entity")
    ]
    if not members:
        members = [
            str(member)
            for member in _as_list(family.get("member_entities"))
            if member
        ]
    return sorted({member for member in members if member})


def _schema_card_physical_family_members(schema_card: JsonObject) -> set[str]:
    members: set[str] = set()
    for family in _schema_card_physical_families(schema_card):
        members.update(_physical_family_member_entities(family))
    return members


def _source_vocabulary_hits(
    question: str,
    graph_rows: JsonObject,
    *,
    max_hits: int = 60,
) -> list[JsonObject]:
    question_tokens = _tokens(question)
    field_names = {f"{field['entity']}.{field['field']}" for field in graph_rows["fields"]}
    entity_names = {str(entity["canonical_name"]) for entity in graph_rows["entities"]}
    candidates: list[JsonObject] = []
    for row in graph_rows.get("vocabulary", []):
        canonical_kind = str(row.get("canonical_kind") or "")
        if canonical_kind not in {"entity", "field"}:
            continue
        term = str(row.get("term") or "").strip()
        matched_tokens = _source_vocabulary_match_tokens(question_tokens, term)
        if not matched_tokens:
            continue
        canonical_value = str(row.get("canonical_value") or "")
        if canonical_kind == "field" and canonical_value not in field_names:
            continue
        if canonical_kind == "entity" and canonical_value not in entity_names:
            continue
        candidates.append(
            {
                "term": term,
                "canonical_kind": canonical_kind,
                "canonical_value": canonical_value,
                "matched_tokens": matched_tokens,
                "confidence": float(row.get("confidence") or 0.0),
                "source_layer": int(row.get("source_layer") or 0),
            }
        )
    return _mark_source_vocabulary_ambiguity(candidates)[:max_hits]


def _enum_value_hits(
    question: str,
    graph_rows: JsonObject,
    *,
    max_hits: int = 60,
) -> list[JsonObject]:
    question_tokens = _tokens(question)
    field_names = {f"{field['entity']}.{field['field']}" for field in graph_rows["fields"]}
    candidates: list[JsonObject] = []
    for row in graph_rows.get("vocabulary", []):
        if row.get("canonical_kind") != "enum_value":
            continue
        term = str(row.get("term") or "").strip()
        matched_tokens = _source_vocabulary_match_tokens(question_tokens, term)
        if not matched_tokens:
            continue
        field_ref, raw_value = _parse_enum_value_canonical(
            str(row.get("canonical_value") or "")
        )
        if not field_ref or field_ref not in field_names or raw_value is None:
            continue
        candidates.append(
            {
                "term": term,
                "field": field_ref,
                "operator": "=",
                "raw_value": raw_value,
                "value_kind": "enum_value",
                "matched_tokens": matched_tokens,
                "confidence": float(row.get("confidence") or 0.0),
                "source_layer": int(row.get("source_layer") or 0),
            }
        )
    return _mark_enum_value_ambiguity(candidates)[:max_hits]


def _source_vocabulary_match_tokens(
    question_tokens: set[str],
    term: str,
) -> list[str]:
    term_tokens = _tokens(term)
    if not term_tokens or not term_tokens <= question_tokens:
        return []
    return sorted(term_tokens)


def _mark_source_vocabulary_ambiguity(candidates: list[JsonObject]) -> list[JsonObject]:
    targets_by_term: dict[tuple[str, str], set[str]] = defaultdict(set)
    for candidate in candidates:
        key = (
            str(candidate.get("canonical_kind") or ""),
            _normalize_value(candidate.get("term")),
        )
        targets_by_term[key].add(str(candidate.get("canonical_value") or ""))
    for candidate in candidates:
        key = (
            str(candidate.get("canonical_kind") or ""),
            _normalize_value(candidate.get("term")),
        )
        if len(targets_by_term[key]) > 1:
            candidate["requires_clarification"] = True
    candidates.sort(
        key=lambda candidate: (
            bool(candidate.get("requires_clarification")),
            -int(candidate.get("source_layer") or 0),
            -float(candidate.get("confidence") or 0.0),
            str(candidate.get("term") or ""),
            str(candidate.get("canonical_value") or ""),
        )
    )
    return candidates


def _parse_enum_value_canonical(value: str) -> tuple[str, str | None]:
    field_ref, sep, raw_value = value.partition(":")
    if not sep or not field_ref:
        return "", None
    return field_ref, raw_value


def _mark_enum_value_ambiguity(candidates: list[JsonObject]) -> list[JsonObject]:
    targets_by_term: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for candidate in candidates:
        term = _normalize_value(candidate.get("term"))
        targets_by_term[term].add(
            (
                str(candidate.get("field") or ""),
                _normalize_value(candidate.get("raw_value")),
            )
        )
    for candidate in candidates:
        term = _normalize_value(candidate.get("term"))
        if len(targets_by_term[term]) > 1:
            candidate["requires_clarification"] = True
    candidates.sort(
        key=lambda candidate: (
            bool(candidate.get("requires_clarification")),
            -int(candidate.get("source_layer") or 0),
            -float(candidate.get("confidence") or 0.0),
            str(candidate.get("term") or ""),
            str(candidate.get("field") or ""),
        )
    )
    return candidates


def _sample_value_hits(
    question: str,
    graph_rows: JsonObject,
    *,
    max_hits: int = 40,
    max_examples_per_field: int = 25,
) -> list[JsonObject]:
    question_tokens = _tokens(question)
    candidates: list[JsonObject] = []
    seen: set[tuple[str, str]] = set()
    sample_values = graph_rows.get("sample_values", {})
    if not isinstance(sample_values, dict):
        return []
    for field_ref, examples in sample_values.items():
        if not isinstance(field_ref, str) or "." not in field_ref:
            continue
        for sample in _as_list(examples)[:max_examples_per_field]:
            raw_value = _sample_candidate_text(sample)
            if raw_value is None or not _sample_value_is_safe_candidate(raw_value):
                continue
            sample_match = _sample_value_match(
                question,
                question_tokens,
                raw_value,
            )
            if sample_match is None:
                continue
            matched_tokens, match_type = sample_match
            key = (field_ref, _normalize_value(raw_value))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "field": field_ref,
                    "operator": "=",
                    "raw_value": raw_value,
                    "value_kind": "sample_value",
                    "match_type": match_type,
                    "matched_tokens": matched_tokens,
                }
            )
    component_counts: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        if candidate["match_type"] != "component":
            continue
        for token in candidate["matched_tokens"]:
            component_counts[str(token)] += 1
    hits: list[JsonObject] = []
    for candidate in candidates:
        if candidate["match_type"] == "component":
            if all(
                component_counts.get(str(token), 0) == 1
                for token in candidate["matched_tokens"]
            ):
                candidate["match_type"] = "unique_component"
            else:
                candidate["match_type"] = "ambiguous_component"
                candidate["requires_clarification"] = True
        hits.append(candidate)
        if len(hits) >= max_hits:
            return hits
    return hits


def _metric_catalog_hits(
    question: str,
    graph_rows: JsonObject,
    *,
    max_hits: int = 20,
) -> list[JsonObject]:
    question_tokens = _tokens(question)
    if not question_tokens:
        return []
    field_names = {f"{field['entity']}.{field['field']}" for field in graph_rows["fields"]}
    entity_names = {str(entity["canonical_name"]) for entity in graph_rows["entities"]}
    hits: list[JsonObject] = []
    for metric in graph_rows.get("metric_definitions", []):
        if not isinstance(metric, dict):
            continue
        hit = _metric_catalog_hit(metric, question_tokens, field_names, entity_names)
        if hit is not None:
            hits.append(hit)
        if len(hits) >= max_hits:
            break
    hits.sort(
        key=lambda hit: (
            -len(_as_list(hit.get("matched_tokens"))),
            str(hit.get("name") or ""),
        )
    )
    return hits


def _metric_catalog_hit(
    metric: JsonObject,
    question_tokens: set[str],
    field_names: set[str],
    entity_names: set[str],
) -> JsonObject | None:
    metric_kind = str(metric.get("metric_kind") or "")
    if metric_kind not in {"conditional_rate", "aggregate"}:
        return None
    subject_entity = str(metric.get("subject_entity") or "")
    if subject_entity not in entity_names:
        return None
    labels = [
        str(metric.get("name") or ""),
        str(metric.get("display_label") or ""),
        *[str(alias) for alias in _as_list(metric.get("aliases")) if alias],
    ]
    matched_tokens = _best_metric_label_match(question_tokens, labels)
    if not matched_tokens:
        return None
    base_hit: JsonObject = {
        "metric_kind": metric_kind,
        "name": str(metric.get("name") or ""),
        "display_label": str(metric.get("display_label") or ""),
        "alias": str(metric.get("name") or "metric"),
        "subject_entity": subject_entity,
        "matched_tokens": matched_tokens,
        "source": "metric_definition",
    }
    if metric_kind == "conditional_rate":
        numerator_field = str(metric.get("numerator_field") or "")
        denominator_field = str(metric.get("denominator_field") or "")
        numerator_value = metric.get("numerator_value")
        if (
            numerator_field not in field_names
            or denominator_field not in field_names
            or numerator_value is None
        ):
            return None
        required_entities = {
            subject_entity,
            _field_ref_entity(numerator_field),
            _field_ref_entity(denominator_field),
            *[
                str(entity)
                for entity in _as_list(metric.get("required_entities"))
                if str(entity).strip()
            ],
        }
        required_entities.discard("")
        if not required_entities <= entity_names:
            return None
        return {
            **base_hit,
            "numerator_field": numerator_field,
            "numerator_operator": str(metric.get("numerator_operator") or "="),
            "numerator_value": numerator_value,
            "numerator_value_kind": str(metric.get("numerator_value_kind") or "literal"),
            "denominator_field": denominator_field,
            "scale": float(metric.get("scale") or 100.0),
            "required_entities": sorted(required_entities),
        }
    measure_field = str(metric.get("measure_field") or "")
    aggregate = str(metric.get("aggregate") or "").upper()
    if measure_field not in field_names or aggregate not in {"AVG", "COUNT", "MAX", "MIN", "SUM"}:
        return None
    distinct = _boolish(metric.get("distinct") or metric.get("distinct_measure"))
    if distinct and aggregate != "COUNT":
        return None
    required_entities = {
        subject_entity,
        _field_ref_entity(measure_field),
        *[
            str(entity)
            for entity in _as_list(metric.get("required_entities"))
            if str(entity).strip()
        ],
    }
    required_entities.discard("")
    if not required_entities <= entity_names:
        return None
    return {
        **base_hit,
        "measure_field": measure_field,
        "aggregate": aggregate,
        "distinct": distinct,
        "scale": float(metric.get("scale") or 1.0),
        "required_entities": sorted(required_entities),
    }


def _best_metric_label_match(
    question_tokens: set[str],
    labels: list[str],
) -> list[str]:
    matches: list[set[str]] = []
    for label in labels:
        tokens = _tokens(label)
        meaningful = tokens - GENERIC_SCHEMA_MATCH_TOKENS - {
            "metric",
            "rate",
            "percent",
            "percentage",
            "pct",
        }
        if not tokens or not meaningful or not tokens <= question_tokens:
            continue
        matches.append(tokens)
    if not matches:
        return []
    best = max(matches, key=lambda tokens: (len(tokens), sorted(tokens)))
    return sorted(best)


def _metric_formula_hits(
    question: str,
    graph_rows: JsonObject,
    value_dictionary_hits: list[JsonObject],
    enum_value_hits: list[JsonObject],
    sample_value_hits: list[JsonObject],
    *,
    max_hits: int = 20,
) -> list[JsonObject]:
    question_tokens = _tokens(question)
    if not _rate_metric_requested(question_tokens):
        return []
    value_hits = [
        *[
            {**hit, "value_kind": "value_dictionary"}
            for hit in value_dictionary_hits
            if isinstance(hit, dict)
        ],
        *[
            {**hit, "value_kind": "enum_value"}
            for hit in enum_value_hits
            if isinstance(hit, dict) and not hit.get("requires_clarification")
        ],
        *[
            {**hit, "value_kind": "sample_value"}
            for hit in sample_value_hits
            if isinstance(hit, dict)
            and str(hit.get("match_type") or "exact") != "ambiguous_component"
        ],
    ]
    hits: list[JsonObject] = []
    seen: set[tuple[str, str, str]] = set()
    for value_hit in value_hits:
        field_ref = str(value_hit.get("field") or "")
        raw_value = value_hit.get("raw_value")
        if "." not in field_ref or raw_value is None:
            continue
        operator = _normalize_operator(str(value_hit.get("operator") or "="), raw_value)
        if operator != "=":
            continue
        entity = field_ref.split(".", 1)[0]
        denominator_field = _graph_entity_id_field(graph_rows, entity)
        if denominator_field is None:
            continue
        alias = _metric_formula_alias(value_hit, question_tokens)
        key = (field_ref, operator, _normalize_value(raw_value))
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            {
                "metric_kind": "conditional_rate",
                "alias": alias,
                "numerator_field": field_ref,
                "numerator_operator": operator,
                "numerator_value": raw_value,
                "numerator_value_kind": str(value_hit.get("value_kind") or "literal"),
                "denominator_field": denominator_field,
                "scale": 100.0,
                "matched_tokens": sorted(
                    set(value_hit.get("matched_tokens") or []) | (question_tokens & {"rate", "percent", "percentage"})
                ),
                "source": "packet_value_formula",
            }
        )
        if len(hits) >= max_hits:
            break
    hits.sort(
        key=lambda hit: (
            str(hit.get("alias") or ""),
            str(hit.get("numerator_field") or ""),
            str(hit.get("numerator_value") or ""),
        )
    )
    return hits


def _rate_metric_requested(question_tokens: set[str]) -> bool:
    return bool(question_tokens & {"rate", "percent", "percentage", "pct"})


def _graph_entity_id_field(graph_rows: JsonObject, entity_name: str) -> str | None:
    fields = [
        field
        for field in graph_rows.get("fields", [])
        if isinstance(field, dict) and str(field.get("entity") or "") == entity_name
    ]
    for field in fields:
        if str(field.get("field") or "").lower() == "id":
            return f"{entity_name}.{field['field']}"
    for field in fields:
        if _field_role(field) == "id":
            return f"{entity_name}.{field['field']}"
    return None


def _metric_formula_alias(value_hit: JsonObject, question_tokens: set[str]) -> str:
    raw_value = value_hit.get("raw_value")
    term = str(value_hit.get("term") or raw_value or "metric")
    tokens = [
        token
        for token in _tokens(term)
        if token not in GENERIC_SCHEMA_MATCH_TOKENS and len(token) >= 2
    ]
    if not tokens:
        field = str(value_hit.get("field") or "metric")
        tokens = [token for token in _tokens(field.split(".")[-1]) if token not in {"status", "state"}]
    prefix = "_".join(tokens[:4]) or "metric"
    suffix = "pct" if question_tokens & {"percent", "percentage", "pct"} else "rate"
    return f"{prefix}_{suffix}"


def _sample_candidate_text(value: Any) -> str | None:
    if value is None or isinstance(value, list | dict):
        return None
    text = str(value).strip()
    return text or None


def _sample_value_is_safe_candidate(value: str) -> bool:
    if len(value) > 80 or "\n" in value or "\r" in value:
        return False
    lowered = value.lower()
    if "@" in lowered or re.search(r"\b(?:https?://|www\.)", lowered):
        return False
    raw_tokens = re.findall(r"[a-z0-9]+", lowered)
    if len(raw_tokens) > 6:
        return False
    if (
        len(raw_tokens) == 1
        and len(raw_tokens[0]) >= 24
        and re.search(r"[a-z]", raw_tokens[0])
        and re.search(r"\d", raw_tokens[0])
    ):
        return False
    return True


def _sample_value_match(
    question: str,
    question_tokens: set[str],
    value: str,
) -> tuple[list[str], str] | None:
    value_tokens = _tokens(value)
    raw_value_tokens = re.findall(r"[a-z0-9]+", value.lower())
    if not raw_value_tokens:
        return None
    requires_surface_match = (
        any(len(token) < 2 for token in raw_value_tokens)
        or bool(re.search(r"[^a-z0-9\s]", value.lower()))
        or all(token.isdigit() for token in raw_value_tokens)
    )
    if requires_surface_match:
        question_compact = "".join(re.findall(r"[a-z0-9]+", question.lower()))
        value_compact = "".join(raw_value_tokens)
        if value_compact and value_compact in question_compact:
            return sorted(value_tokens) if value_tokens else raw_value_tokens, "exact"
    if value_tokens and value_tokens <= question_tokens:
        return sorted(value_tokens), "exact"
    component_tokens = sorted(
        token
        for token in (value_tokens & question_tokens)
        if token not in GENERIC_SCHEMA_MATCH_TOKENS and len(token) >= 3
    )
    if len(raw_value_tokens) > 1 and component_tokens:
        return component_tokens, "component"
    if len(value.strip()) == 1:
        return [value.strip().lower()], "exact"
    return None


def _token_variants(token: str) -> set[str]:
    variants = {token}
    if token.endswith("ies") and len(token) > 3:
        variants.add(token[:-3] + "y")
    elif (
        token.endswith("s")
        and len(token) > 3
        and not token.endswith(("ss", "us", "is"))
    ):
        variants.add(token[:-1])
    if token.endswith("ed") and len(token) > 4 and not token.endswith("eed"):
        variants.add(token[:-2])
        variants.add(token[:-1])
    if token.endswith("ing") and len(token) > 5:
        stem = token[:-3]
        variants.add(stem)
        variants.add(f"{stem}e")
        if len(stem) > 2 and stem[-1] == stem[-2]:
            variants.add(stem[:-1])
    return {variant for variant in variants if len(variant) >= 2}


def _candidate_entities(
    schema_card: JsonObject,
    graph_rows: JsonObject | None,
) -> list[tuple[str, list[str]]]:
    if graph_rows is None:
        return [
            (
                str(entity["name"]),
                [
                    str(entity["name"]),
                    str(entity["db_table"]),
                    *[str(label) for label in entity.get("labels", [])],
                ],
            )
            for entity in schema_card["entities"]
        ]
    return [
        (
            str(entity["canonical_name"]),
            [
                str(entity["canonical_name"]),
                str(entity["db_table"]),
                str(entity.get("singular_label") or ""),
                str(entity.get("plural_label") or ""),
            ],
        )
        for entity in graph_rows["entities"]
    ]


def _question_schema_seed_refs(
    question: str,
    graph_rows: JsonObject,
    *,
    max_entities: int = 32,
    max_fields: int = 96,
) -> tuple[set[str], set[str]]:
    question_tokens = _tokens(question)
    entity_scores: list[tuple[int, str]] = []
    for entity in graph_rows["entities"]:
        labels = [
            str(entity["canonical_name"]),
            str(entity["db_table"]),
            str(entity.get("singular_label") or ""),
            str(entity.get("plural_label") or ""),
        ]
        label_token_sets = [_tokens(label) for label in labels if label]
        label_tokens = set().union(*label_token_sets)
        matched = question_tokens & label_tokens
        if not matched:
            continue
        if not any(tokens and tokens <= question_tokens for tokens in label_token_sets):
            continue
        score = (len(matched) * 4) + sum(1 for token in matched if len(token) >= 5)
        canonical_tokens = _tokens(str(entity["canonical_name"]))
        if canonical_tokens and canonical_tokens <= question_tokens:
            score += 6
        entity_scores.append((score, str(entity["canonical_name"])))
    entity_scores.sort(key=lambda item: (-item[0], item[1]))
    seed_entities = {entity for _, entity in entity_scores[:max_entities]}

    field_scores: list[tuple[int, str]] = []
    for field in graph_rows["fields"]:
        entity_name = str(field["entity"])
        field_ref = f"{entity_name}.{field['field']}"
        field_tokens = _tokens(
            f"{field['field']} {field['db_column']} {field.get('display_label') or ''}"
        )
        matched = question_tokens & field_tokens
        if not matched:
            continue
        non_generic = matched - GENERIC_SCHEMA_MATCH_TOKENS
        entity_boost = 8 if entity_name in seed_entities else 0
        if not entity_boost and not non_generic:
            continue
        score = (
            entity_boost
            + (len(matched) * 3)
            + (len(non_generic) * 4)
            + sum(1 for token in non_generic if len(token) >= 5)
        )
        if _field_role(field) in {"numeric", "status", "display", "date"}:
            score += 1
        field_scores.append((score, field_ref))
    field_scores.sort(key=lambda item: (-item[0], item[1]))
    seed_fields = {field for _, field in field_scores[:max_fields]}
    for field_ref in list(seed_fields):
        entity = _field_ref_entity(field_ref)
        if entity:
            seed_entities.add(entity)
    return seed_entities, seed_fields


def _date_window_seed_refs(
    question: str,
    graph_rows: JsonObject,
    seed_entities: set[str],
) -> tuple[set[str], set[str]]:
    candidate = _date_window_candidate(question, graph_rows, seed_entities)
    if not isinstance(candidate, dict):
        return set(), set()
    preferred_anchor = candidate.get("preferred_anchor")
    if isinstance(preferred_anchor, dict) and candidate.get("ambiguous") is False:
        field_ref = str(preferred_anchor.get("field") or "")
        entity = str(preferred_anchor.get("entity") or "")
        if field_ref and entity:
            return {entity}, {field_ref}
    anchor_candidates = _as_list(candidate.get("anchor_candidates"))
    if len(anchor_candidates) != 1:
        return set(), set()
    anchor = anchor_candidates[0]
    if not isinstance(anchor, dict):
        return set(), set()
    field_ref = str(anchor.get("field") or "")
    entity = str(anchor.get("entity") or "")
    if not field_ref or not entity:
        return set(), set()
    return {entity}, {field_ref}


def _scope_path_seed_refs(candidates: JsonObject) -> tuple[set[str], set[str]]:
    seed_entities: set[str] = set()
    seed_fields: set[str] = set()
    for candidate in _as_list(candidates.get("scope_path_candidates")):
        if not isinstance(candidate, dict):
            continue
        for key in ("subject_entity", "scope_entity"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                seed_entities.add(value)
        for relationship in _as_list(candidate.get("relationships")):
            if not isinstance(relationship, dict):
                continue
            for key in ("from", "to"):
                field_ref = str(relationship.get(key) or "")
                if "." not in field_ref:
                    continue
                seed_fields.add(field_ref)
                entity = _field_ref_entity(field_ref)
                if entity:
                    seed_entities.add(entity)
    return seed_entities, seed_fields


def _scope_path_candidates(
    question: str,
    graph_rows: JsonObject | None,
    seed_entities: set[str],
    *,
    max_path_len: int = 3,
    max_candidates: int = 20,
) -> list[JsonObject]:
    if graph_rows is None or not seed_entities:
        return []
    question_tokens = _tokens(question)
    scope_terms = question_tokens & SCOPE_ENTITY_TOKENS
    if not scope_terms:
        return []
    relationships = [
        relationship
        for relationship in graph_rows.get("relationships", [])
        if isinstance(relationship, dict)
    ]
    scope_entities = {
        str(entity.get("canonical_name") or "")
        for entity in graph_rows.get("entities", [])
        if isinstance(entity, dict)
        and _entity_matches_scope_terms(entity, scope_terms, question_tokens)
    }
    scope_entities &= seed_entities
    if not scope_entities:
        return []
    subject_entities = sorted(seed_entities - scope_entities)
    if not subject_entities:
        return []
    candidates: list[JsonObject] = []
    seen: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
    for subject in subject_entities:
        for scope_entity in sorted(scope_entities):
            paths = _direct_relationship_paths(subject, scope_entity, relationships)
            if not paths:
                path = _relationship_path({subject}, scope_entity, relationships)
                paths = [path] if path else []
            for path in paths:
                if not path or len(path) > max_path_len:
                    continue
                cards = [_relationship_card_from_join_step(step) for step in path]
                key = (
                    subject,
                    scope_entity,
                    tuple((str(card["from"]), str(card["to"])) for card in cards),
                )
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "subject_entity": subject,
                        "scope_entity": scope_entity,
                        "matched_scope_terms": sorted(scope_terms),
                        "relationships": cards,
                        "path_length": len(cards),
                    }
                )
    candidates.sort(
        key=lambda candidate: (
            int(candidate["path_length"]),
            str(candidate["subject_entity"]),
            str(candidate["scope_entity"]),
            json.dumps(candidate["relationships"], sort_keys=True),
        )
    )
    return candidates[:max_candidates]


def _entity_matches_scope_terms(
    entity: JsonObject,
    scope_terms: set[str],
    question_tokens: set[str],
) -> bool:
    labels = [
        str(entity.get("canonical_name") or ""),
        str(entity.get("db_table") or ""),
        str(entity.get("singular_label") or ""),
        str(entity.get("plural_label") or ""),
    ]
    label_token_sets = [_tokens(label) for label in labels if label]
    if not any(tokens and tokens <= question_tokens for tokens in label_token_sets):
        return False
    return bool(set().union(*label_token_sets) & scope_terms)


def _direct_relationship_paths(
    from_entity: str,
    to_entity: str,
    relationships: list[JsonObject],
) -> list[list[JsonObject]]:
    paths: list[list[JsonObject]] = []
    for relationship in relationships:
        if relationship.get("from_entity") == from_entity and relationship.get("to_entity") == to_entity:
            paths.append([_normalize_join_with_kind(relationship)])
        elif relationship.get("to_entity") == from_entity and relationship.get("from_entity") == to_entity:
            reversed_join = {
                "from_entity": relationship["to_entity"],
                "from_field": relationship["to_field"],
                "to_entity": relationship["from_entity"],
                "to_field": relationship["from_field"],
                "kind": relationship.get("kind", "relationship"),
            }
            paths.append([_normalize_join_with_kind(reversed_join)])
    return paths


def _normalize_join_with_kind(join: JsonObject) -> JsonObject:
    normalized = _normalize_join(join)
    normalized["kind"] = join.get("kind", "relationship")
    return normalized


def _relationship_card_from_join_step(step: JsonObject) -> JsonObject:
    return {
        "from": f"{step['from_entity']}.{step['from_field']}",
        "to": f"{step['to_entity']}.{step['to_field']}",
        "kind": step.get("kind", "relationship_path"),
    }


def _field_source_vocabulary_terms(graph_rows: JsonObject) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for row in graph_rows.get("vocabulary", []):
        if not isinstance(row, dict) or row.get("canonical_kind") != "field":
            continue
        field_ref = str(row.get("canonical_value") or "")
        term = str(row.get("term") or "").strip()
        if "." in field_ref and term:
            out[field_ref].append(term)
    return out


def _date_role_match(
    question: str,
    field: JsonObject,
    source_terms: list[str],
) -> tuple[int, list[str]]:
    field_text = " ".join(
        [
            str(field.get("field") or ""),
            str(field.get("db_column") or ""),
            str(field.get("display_label") or ""),
            *source_terms,
        ]
    )
    field_tokens = _tokens(field_text)
    role_tokens = _date_role_signal_tokens()
    raw_question_tokens = re.findall(r"[a-z0-9]+", question.lower())
    matched_tokens = sorted(
        {
            token
            for token in raw_question_tokens
            if _token_variants(token) & field_tokens & role_tokens
        }
    )
    if not matched_tokens:
        return 0, []
    source_term_tokens = (
        set().union(*(_tokens(term) for term in source_terms)) if source_terms else set()
    )
    score = len(matched_tokens) * 10
    score += sum(
        3 for token in matched_tokens if _token_variants(token) & source_term_tokens
    )
    return score, matched_tokens


def _date_role_signal_tokens() -> set[str]:
    seeds = {
        "activated",
        "active",
        "approved",
        "approval",
        "captured",
        "capture",
        "canceled",
        "cancelled",
        "closed",
        "closing",
        "converted",
        "created",
        "due",
        "ended",
        "expired",
        "issued",
        "joined",
        "launched",
        "opened",
        "ordered",
        "paid",
        "posted",
        "qualified",
        "renewal",
        "renewed",
        "resolved",
        "scheduled",
        "sent",
        "signed",
        "started",
        "submitted",
        "updated",
    }
    out: set[str] = set()
    for seed in seeds:
        out.update(_token_variants(seed))
    return out


def _date_window_candidate(
    question: str,
    graph_rows: JsonObject | None,
    seed_entities: set[str],
) -> JsonObject | None:
    window = _question_named_date_window(question)
    if window is None:
        return None
    start, end, mention = window
    anchor_candidates: list[JsonObject] = []
    preferred_anchor: JsonObject | None = None
    if graph_rows is not None and seed_entities:
        fields_by_entity: dict[str, list[JsonObject]] = defaultdict(list)
        field_vocab_terms = _field_source_vocabulary_terms(graph_rows)
        for field in graph_rows.get("fields", []):
            if not isinstance(field, dict):
                continue
            entity = str(field.get("entity") or "")
            if entity not in seed_entities:
                continue
            if _field_role(field) != "date":
                continue
            field_ref = f"{entity}.{field['field']}"
            score, matched_tokens = _date_role_match(
                question,
                field,
                field_vocab_terms.get(field_ref, []),
            )
            fields_by_entity[entity].append(
                {
                    "entity": entity,
                    "field": field_ref,
                    "score": score,
                    "matched_tokens": matched_tokens,
                }
            )
        for entity in sorted(fields_by_entity):
            refs = sorted(fields_by_entity[entity], key=lambda item: str(item["field"]))
            unique_refs: list[JsonObject] = []
            seen_refs: set[str] = set()
            for ref in refs:
                field_ref = str(ref["field"])
                if field_ref in seen_refs:
                    continue
                seen_refs.add(field_ref)
                unique_refs.append(ref)
            reason = (
                "sole_date_field_on_mentioned_entity"
                if len(unique_refs) == 1
                else "one_of_multiple_date_fields_on_mentioned_entity"
            )
            best_score = max((int(ref["score"]) for ref in unique_refs), default=0)
            best_refs = [ref for ref in unique_refs if int(ref["score"]) == best_score]
            has_unique_role_match = len(unique_refs) > 1 and best_score > 0 and len(best_refs) == 1
            for ref in unique_refs:
                candidate_reason = (
                    "date_role_match_on_mentioned_entity"
                    if has_unique_role_match and ref is best_refs[0]
                    else reason
                )
                candidate = {
                    "entity": entity,
                    "field": ref["field"],
                    "reason": candidate_reason,
                }
                if ref["matched_tokens"]:
                    candidate["matched_tokens"] = ref["matched_tokens"]
                    candidate["score"] = ref["score"]
                anchor_candidates.append(candidate)
                if candidate_reason == "date_role_match_on_mentioned_entity":
                    preferred_anchor = candidate
    ambiguous = len(anchor_candidates) != 1
    if preferred_anchor is not None:
        ambiguous = False
    return {
        "mention": mention,
        "start": start,
        "end": end,
        "anchor_candidates": anchor_candidates[:40],
        "ambiguous": ambiguous,
        **({"preferred_anchor": preferred_anchor} if preferred_anchor is not None else {}),
    }


def _schema_card_question_seed_refs(
    question: str,
    schema_card: JsonObject,
    *,
    max_entities: int = 24,
    max_fields: int = 80,
) -> tuple[set[str], set[str]]:
    question_tokens = _tokens(question)
    entity_scores: list[tuple[int, str]] = []
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict):
            continue
        labels = [
            str(entity.get("name") or ""),
            str(entity.get("db_table") or ""),
            *[str(label) for label in entity.get("labels", []) if label],
        ]
        label_token_sets = [_tokens(label) for label in labels if label]
        label_tokens = set().union(*label_token_sets)
        matched = question_tokens & label_tokens
        if not matched:
            continue
        if not any(tokens and tokens <= question_tokens for tokens in label_token_sets):
            continue
        score = (len(matched) * 4) + sum(1 for token in matched if len(token) >= 5)
        name_tokens = _tokens(str(entity.get("name") or ""))
        if name_tokens and name_tokens <= question_tokens:
            score += 6
        entity_scores.append((score, str(entity.get("name") or "")))
    entity_scores.sort(key=lambda item: (-item[0], item[1]))
    seed_entities = {entity for _, entity in entity_scores[:max_entities] if entity}

    field_scores: list[tuple[int, str]] = []
    for entity in schema_card.get("entities", []):
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("name") or "")
        if not entity_name:
            continue
        for field in entity.get("fields", []):
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or field.get("field") or "")
            if not field_name:
                continue
            field_tokens = _tokens(
                f"{field_name} {field.get('db_column') or ''} "
                f"{field.get('display_label') or ''}"
            )
            matched = question_tokens & field_tokens
            if not matched:
                continue
            non_generic = matched - GENERIC_SCHEMA_MATCH_TOKENS
            entity_boost = 8 if entity_name in seed_entities else 0
            if not entity_boost and not non_generic:
                continue
            score = (
                entity_boost
                + (len(matched) * 3)
                + (len(non_generic) * 4)
                + sum(1 for token in non_generic if len(token) >= 5)
            )
            if str(field.get("role") or "") in {"numeric", "status", "display", "date"}:
                score += 1
            field_scores.append((score, f"{entity_name}.{field_name}"))
    field_scores.sort(key=lambda item: (-item[0], item[1]))
    seed_fields = {field for _, field in field_scores[:max_fields]}
    for field_ref in list(seed_fields):
        entity = _field_ref_entity(field_ref)
        if entity:
            seed_entities.add(entity)
    return seed_entities, seed_fields


def _candidate_fields(
    schema_card: JsonObject,
    graph_rows: JsonObject | None,
) -> list[tuple[str, JsonObject]]:
    if graph_rows is None:
        return [
            (str(entity["name"]), field)
            for entity in schema_card["entities"]
            for field in entity["fields"]
        ]
    fields = graph_rows["fields"]
    field_names = {f"{field['entity']}.{field['field']}" for field in fields}
    value_dictionary_by_field = _scope_predicates_by_field(
        graph_rows["vocabulary"],
        field_names,
        12,
    )
    return [
        (
            str(field["entity"]),
            _field_summary(field, {}, 0, value_dictionary_by_field, 12),
        )
        for field in fields
    ]


def _value_dictionary_match_tokens(question_tokens: set[str], term: str) -> list[str]:
    term_tokens = _tokens(term)
    if not term_tokens or not term_tokens <= question_tokens:
        return []
    return sorted(term_tokens)


def _tokens(text: str) -> set[str]:
    out = set(re.findall(r"[a-z0-9]+", text.lower()))
    expanded: set[str] = set()
    for token in out:
        expanded.update(_token_variants(token))
    return {token for token in expanded if len(token) >= 2}


def _extract_response_text(response: JsonObject) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                return text
            output = content.get("output_text")
            if isinstance(output, str):
                return output
    raise RuntimeError("OpenAI response did not contain output text")


def _extract_chat_completion_text(response: JsonObject) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("chat completion response did not contain choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError("chat completion choice was not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("chat completion choice did not contain a message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "".join(parts)
    raise RuntimeError("chat completion message did not contain text content")


def _loads_provider_json_text(text: str) -> JsonObject:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise RuntimeError("provider response JSON must be an object")
    return payload


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if not normalized.startswith("https://"):
        raise RuntimeError("provider base URL must use https://")
    if normalized.endswith("/chat/completions"):
        return normalized
    return normalized + "/chat/completions"


def _redact_openai_request(body: JsonObject) -> JsonObject:
    return _redact_provider_request(body)


def _redact_provider_request(body: JsonObject) -> JsonObject:
    redacted = dict(body)
    if "input" in redacted:
        redacted["input"] = "<packet redacted from retained provider result>"
    messages = redacted.get("messages")
    if isinstance(messages, list):
        redacted_messages: list[object] = []
        for message in messages:
            if not isinstance(message, dict):
                redacted_messages.append(message)
                continue
            copied = dict(message)
            if copied.get("role") == "user":
                copied["content"] = "<packet redacted from retained provider result>"
            redacted_messages.append(copied)
        redacted["messages"] = redacted_messages
    return redacted
