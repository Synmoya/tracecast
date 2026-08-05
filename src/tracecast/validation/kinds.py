"""Controlled vocabulary for audit reports.

``AuditReportItem.stage`` / ``kind`` / ``level`` are open strings in the domain
backbone. These enums keep emitted error-mode names stable and typo-proof without
freezing the record schema.
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    """Which pipeline stage a finding pertains to (where it was discovered)."""

    ingestion = "ingestion"
    planning = "planning"
    selection = "selection"
    node_edit = "node_edit"
    packing = "packing"
    name_resolution = "name_resolution"
    rendering = "rendering"
    validation = "validation"
    llm = "llm"
    assembly = "assembly"


class Level(StrEnum):
    """Reading priority only -- it never gates the main chain."""

    error = "error"
    warning = "warning"
    info = "info"


class AuditKind(StrEnum):
    """Controlled error-mode names emitted by the report-only auditor."""

    # DSL / projection
    dsl_parse_error = "dsl_parse_error"
    unknown_alias = "unknown_alias"
    duplicate_alias_ref = "duplicate_alias_ref"
    partition_gap = "partition_gap"
    partition_overlap = "partition_overlap"
    controlled_value_unknown = "controlled_value_unknown"
    field_count_mismatch = "field_count_mismatch"
    # traceability
    missing_source_snapshot = "missing_source_snapshot"
    missing_piece_ref = "missing_piece_ref"
    material_not_traceable = "material_not_traceable"
    chunk_not_traceable = "chunk_not_traceable"
    condense_lineage_missing = "condense_lineage_missing"
    raw_response_missing = "raw_response_missing"
    # contract consistency
    dangling_reference = "dangling_reference"
    unexpected_omitted_material = "unexpected_omitted_material"
    unexpected_unselected_node = "unexpected_unselected_node"
    chunk_budget_exceeded = "chunk_budget_exceeded"
    single_node_overload = "single_node_overload"
    projection_contains_audit_field = "projection_contains_audit_field"
    # rendered text
    internal_id_leak = "internal_id_leak"
    json_or_schema_leak = "json_or_schema_leak"
    forbidden_claim = "forbidden_claim"
    text_changed_after_render = "text_changed_after_render"
    # llm call audit
    prompt_version_missing = "prompt_version_missing"
    call_ledger_missing = "call_ledger_missing"
