"""Console reports for Packing through Assembly."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from tracecast.domain import RenderFactContext, estimate_tokens
from tracecast.storage import (
    ASSEMBLY_REPORT,
    AUDIT_REPORT,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SCRIPT,
)

from .report_common import (
    distribution,
    excerpt,
    origins,
    ratio,
    sources,
    stage_diagnostics,
    unavailable,
)


def packing(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(root, run_id, CHUNK_PLAN, RENDER_CONTRACTS)
    if CHUNK_PLAN.relpath not in resolved:
        return unavailable(run_id, "packing", [CHUNK_PLAN.relpath])
    plan = resolved[CHUNK_PLAN.relpath].read_model(CHUNK_PLAN)
    contracts = (
        resolved[RENDER_CONTRACTS.relpath].read_records(RENDER_CONTRACTS)
        if RENDER_CONTRACTS.relpath in resolved
        else []
    )
    overload = [chunk for chunk in plan.chunks if chunk.metadata.get("overloaded_caps")]
    return {
        "origins": origins(resolved),
        "summary": {
            "policy": plan.policy,
            "chunks": len(plan.chunks),
            "nodes": sum(len(chunk.node_ids) for chunk in plan.chunks),
            "materials": sum(len(chunk.material_ids) for chunk in plan.chunks),
            "node_distribution": distribution([len(chunk.node_ids) for chunk in plan.chunks]),
            "material_distribution": distribution(
                [len(chunk.material_ids) for chunk in plan.chunks]
            ),
            "token_distribution": distribution(
                [chunk.estimated_material_tokens for chunk in plan.chunks]
            ),
            "overloaded_chunks": len(overload),
        },
        "samples": [
            {
                "chunk_id": chunk.chunk_id,
                "order_index": chunk.order_index,
                "nodes": len(chunk.node_ids),
                "materials": len(chunk.material_ids),
                "estimated_material_tokens": chunk.estimated_material_tokens,
                "overloaded_caps": chunk.metadata.get("overloaded_caps", []),
            }
            for chunk in plan.chunks[:16]
        ],
        "diagnostics": [
            {
                "kind": "overloaded_chunk",
                "chunk_id": chunk.chunk_id,
                "caps": chunk.metadata.get("overloaded_caps"),
            }
            for chunk in overload
        ],
        "contract_count": len(contracts),
    }


def name_resolution(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(
        root, run_id, NAME_RESOLUTION_PLAN, NAME_RESOLUTION_REPORT, AUDIT_REPORT
    )
    persisted = (
        resolved[NAME_RESOLUTION_REPORT.relpath].read_records(NAME_RESOLUTION_REPORT)
        if NAME_RESOLUTION_REPORT.relpath in resolved
        else []
    )
    audit = stage_diagnostics(resolved, "name_resolution")
    diagnostic_rows = [*[item.model_dump(mode="json") for item in persisted], *audit]
    diagnostics = list(
        {
            str(item.get("issue_id") or index): item
            for index, item in enumerate(diagnostic_rows)
        }.values()
    )
    if NAME_RESOLUTION_PLAN.relpath not in resolved:
        if not diagnostics:
            return unavailable(
                run_id,
                "name_resolution",
                [NAME_RESOLUTION_PLAN.relpath, NAME_RESOLUTION_REPORT.relpath],
            )
        return {
            "origins": origins(resolved),
            "summary": {"status": "failed", "diagnostic_count": len(diagnostics)},
            "samples": [],
            "diagnostics": diagnostics,
        }
    plan = resolved[NAME_RESOLUTION_PLAN.relpath].read_model(NAME_RESOLUTION_PLAN)
    aliases = [alias for rule in plan.rules for alias in rule.aliases]
    operator_count = sum(
        1
        for rule in plan.rules
        if "operator" in str(rule.metadata.get("source") or "")
    )
    return {
        "origins": origins(resolved),
        "summary": {
            "status": "complete",
            "output_language": plan.output_language,
            "contracts": len(plan.contract_ids),
            "rules": len(plan.rules),
            "aliases": len(aliases),
            "operator_rules": operator_count,
            "evidence_chunks": len(
                {chunk_id for rule in plan.rules for chunk_id in rule.evidence_chunk_ids}
            ),
            "table_calls": int(plan.metadata.get("table_calls") or 0),
            "proposal_entries": int(plan.metadata.get("proposal_entries") or 0),
            "accepted_model_rules": int(
                plan.metadata.get("accepted_model_rules") or 0
            ),
            "protected_surfaces": len(plan.protected_surfaces),
            "diagnostic_count": len(diagnostics),
        },
        "samples": [
            {
                "rule_id": rule.rule_id,
                "stable_name": rule.stable_name,
                "aliases": rule.aliases,
                "evidence_chunk_ids": rule.evidence_chunk_ids,
            }
            for rule in plan.rules[:16]
        ],
        "diagnostics": diagnostics,
    }


def rendering(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(
        root,
        run_id,
        CHUNK_ARTIFACTS,
        RENDER_CONTRACTS,
        RENDER_FACT_CONTEXTS,
        AUDIT_REPORT,
    )
    if CHUNK_ARTIFACTS.relpath not in resolved:
        return unavailable(run_id, "rendering", [CHUNK_ARTIFACTS.relpath])
    chunks = resolved[CHUNK_ARTIFACTS.relpath].read_records(CHUNK_ARTIFACTS)
    contexts = (
        resolved[RENDER_FACT_CONTEXTS.relpath].read_records(RENDER_FACT_CONTEXTS)
        if RENDER_FACT_CONTEXTS.relpath in resolved
        else []
    )
    context_by_chunk = {context.chunk_id: context for context in contexts}
    diagnostics = stage_diagnostics(resolved, "rendering")
    budget_diagnostics = sum(
        item.get("kind") == "render_over_budget" for item in diagnostics
    )
    used_context = sum(_planned_context_used(context) for context in contexts)
    hint_count = sum(len(context.node_hints) for context in contexts)
    length_warnings = sum(
        item.get("kind") == "fact_context_length_warning" for item in diagnostics
    )
    body_texts = [chunk.text for chunk in chunks]
    return {
        "origins": origins(resolved),
        "summary": {
            "chunks": len(chunks),
            "body_chars": sum(len(text) for text in body_texts),
            "body_tokens": sum(estimate_tokens(text) for text in body_texts),
            "body_char_distribution": distribution([len(text) for text in body_texts]),
            "fact_context_cards": len(contexts),
            "fact_context_used": used_context,
            "fact_context_usage_rate": ratio(used_context, len(contexts)),
            "fact_context_hints": hint_count,
            "fact_context_length_warnings": length_warnings,
            "budget_diagnostics": budget_diagnostics,
            "diagnostic_count": len(diagnostics),
        },
        "samples": [
            {
                "chunk_id": chunk.chunk_id,
                "order_index": chunk.order_index,
                "chars": len(chunk.text),
                "fact_context_used": bool(
                    context_by_chunk.get(chunk.chunk_id)
                    and _planned_context_used(context_by_chunk[chunk.chunk_id])
                ),
                "text": excerpt(chunk.text, 260),
            }
            for chunk in sorted(chunks, key=lambda item: item.order_index)[:12]
        ],
        "diagnostics": diagnostics,
    }


def _planned_context_used(context: RenderFactContext) -> bool:
    return bool(context.node_hints)


def validation(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(root, run_id, AUDIT_REPORT)
    if not resolved:
        return unavailable(run_id, "validation", [AUDIT_REPORT.relpath])
    issues = (
        resolved[AUDIT_REPORT.relpath].read_records(AUDIT_REPORT)
        if AUDIT_REPORT.relpath in resolved
        else []
    )
    stage_counts = Counter(item.stage for item in issues)
    kind_counts = Counter(item.kind for item in issues)
    level_counts = Counter(item.level for item in issues)
    usable_counts = Counter(str(item.usable).lower() for item in issues)
    traceability = sum(bool(item.trace_refs or item.artifact_ref) for item in issues)
    return {
        "origins": origins(resolved),
        "summary": {
            "issues": len(issues),
            "by_stage": dict(sorted(stage_counts.items())),
            "by_kind": dict(sorted(kind_counts.items())),
            "by_level": dict(sorted(level_counts.items())) ,
            "by_usable": dict(sorted(usable_counts.items())),
            "traceability_rate": ratio(traceability, len(issues)),
        },
        "samples": [
            {
                "stage": item.stage,
                "kind": item.kind,
                "level": item.level,
                "usable": item.usable,
                "message": item.message,
            }
            for item in issues[:20]
        ],
        "diagnostics": [item.model_dump(mode="json") for item in issues],
    }


def assembly(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(root, run_id, ASSEMBLY_REPORT, SCRIPT, CHUNK_ARTIFACTS)
    if ASSEMBLY_REPORT.relpath not in resolved:
        return unavailable(run_id, "assembly", [ASSEMBLY_REPORT.relpath])
    report = resolved[ASSEMBLY_REPORT.relpath].read_model(ASSEMBLY_REPORT)
    script = resolved[SCRIPT.relpath].read_text(SCRIPT) if SCRIPT.relpath in resolved else ""
    return {
        "origins": origins(resolved),
        "summary": {
            "assembled_chunks": len(report.assembled_chunk_ids),
            "expected_chunks": len(report.chunk_order),
            "missing_chunks": len(report.missing_chunk_ids),
            "duplicate_chunks": len(report.duplicate_chunk_ids),
            "script_chars": len(script),
            "script_tokens": estimate_tokens(script),
            "assembled_with_issues": report.assembled_with_issues,
        },
        "samples": [{"text": excerpt(script, 500)}] if script else [],
        "diagnostics": {
            "missing_chunk_ids": report.missing_chunk_ids,
            "duplicate_chunk_ids": report.duplicate_chunk_ids,
            "validation_report_ref": report.validation_report_ref,
        },
    }
