"""Semantic compiled-output projections for Console stage trace."""

from __future__ import annotations

from typing import Any

from tracecast.domain import LlmCallRecord
from tracecast.storage import (
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    MATERIAL_ATOMS,
    NAME_RESOLUTION_PLAN,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SELECTION_PLAN,
    sha256_json,
)

from .errors import NotFound
from .trace_data import stage_diagnostics


def compiled_output(
    stage: str,
    record: LlmCallRecord,
    artifact_values: dict[str, Any],
    unit_id: str,
) -> tuple[Any, list[Any]]:
    work = record.work_unit
    input_refs = set(work.input_refs if work else [])
    if stage == "ingestion":
        atoms = [
            atom
            for atom in artifact_values.get(MATERIAL_ATOMS.relpath, [])
            if input_refs.intersection(atom.provenance.piece_refs)
        ]
        return (
            [
                {
                    "source_order": atom.source_order,
                    "content_kind": atom.content_kind.value,
                    "tags": atom.tags,
                    "content": atom.content,
                }
                for atom in atoms
            ],
            [],
        )
    if stage == "planning":
        graph = artifact_values.get(CANDIDATE_GRAPH.relpath)
        if graph is None:
            return [], []
        if work and work.kind == "planning_global_edge":
            node_summaries = {node.candidate_id: node.summary for node in graph.nodes}
            rows = [
                {
                    "edge_type": edge.edge_type,
                    "endpoints": {
                        role: node_summaries.get(candidate_id, "（端点未解析）")
                        for role, candidate_id in edge.endpoints.items()
                    },
                    "reason": edge.reason,
                }
                for edge in graph.edges
                if edge.metadata.get("producer_unit_id") == unit_id
            ]
        else:
            rows = []
            seen: set[str] = set()
            for node in graph.nodes:
                for entry in node.metadata.get("producer_lineage", []):
                    if not isinstance(entry, dict) or entry.get("unit_id") != unit_id:
                        continue
                    readable = {
                        key: value
                        for key, value in entry.items()
                        if key not in {"unit_id", "call_id"}
                    }
                    fingerprint = sha256_json(readable)
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        rows.append(readable)
            if not rows:
                rows = [
                    {
                        "summary": node.summary,
                        "narrative_function": node.narrative_function,
                        "material_count": len(node.material_ids),
                        "source_span": (
                            node.source_span.model_dump() if node.source_span is not None else None
                        ),
                    }
                    for node in graph.nodes
                    if node.metadata.get("producer_unit_id") == unit_id
                ]
        return rows, stage_diagnostics(
            artifact_values, "planning", call_id=record.call_id
        )
    if stage == "selection":
        plan = artifact_values.get(SELECTION_PLAN.relpath)
        if plan is None:
            return [], []
        rows = [
            {
                "order_index": (
                    plan.selected_sequence.index(decision.candidate_id)
                    if decision.candidate_id in plan.selected_sequence
                    else None
                ),
                "status": decision.status.value,
                "role": decision.role.value if decision.role else None,
                "treatment": decision.treatment.value if decision.treatment else None,
                "directive": decision.directive,
            }
            for decision in plan.decisions
        ]
        return rows, stage_diagnostics(
            artifact_values, "selection", call_id=record.call_id
        )
    if stage == "name_resolution":
        plan = artifact_values.get(NAME_RESOLUTION_PLAN.relpath)
        if plan is None:
            return [], []
        rules = plan.rules
        if work and work.kind == "name_table":
            rules = [
                rule
                for rule in rules
                if record.call_id in rule.metadata.get("model_call_ids", [])
            ]
        rows = [
            {
                "rule_id": rule.rule_id,
                "stable_name": rule.stable_name,
                "aliases": rule.aliases,
                "evidence_chunk_ids": rule.evidence_chunk_ids,
                "source": rule.metadata.get("source"),
            }
            for rule in rules
        ]
        return rows, stage_diagnostics(
            artifact_values, "name_resolution", call_id=record.call_id
        )
    if stage == "node_edit":
        plan = artifact_values.get(NODE_EDIT_PLAN.relpath)
        atoms = {
            atom.id: atom for atom in artifact_values.get(MATERIAL_ATOMS.relpath, [])
        }
        if plan is None:
            return [], []
        rows: list[dict[str, Any]] = []
        for result in plan.node_edits:
            if result.metadata.get("producer_unit_id") != unit_id:
                continue
            operations: list[dict[str, Any]] = []
            for edit in result.edits:
                operations.append(
                    {
                        "operation": edit.operation.value,
                        "source_contents": [
                            atoms[mid].content for mid in edit.source_material_ids if mid in atoms
                        ],
                        "result_content": (
                            edit.result_material.content if edit.result_material else None
                        ),
                        "reason": edit.reason,
                    }
                )
            operations.extend(
                {
                    "operation": "omit",
                    "source_contents": (
                        [atoms[item.material_id].content]
                        if item.material_id in atoms
                        else []
                    ),
                    "reason": item.reason,
                }
                for item in result.omitted
            )
            rows.append(
                {
                    "role": result.selection.role.value if result.selection.role else None,
                    "treatment": (
                        result.selection.treatment.value if result.selection.treatment else None
                    ),
                    "directive": result.selection.directive,
                    "operations": operations,
                    "fallback": result.metadata.get("fallback_reason"),
                }
            )
        return rows, stage_diagnostics(
            artifact_values, "node_edit", call_id=record.call_id
        )
    if stage == "rendering":
        if work and work.kind == "render_fact_context":
            contexts = artifact_values.get(RENDER_FACT_CONTEXTS.relpath, [])
            return (
                [
                    {
                        "order_index": context.order_index,
                        "node_hints": [
                            {
                                "source_node_id": hint.source_node_id,
                                "established": hint.established,
                            }
                            for hint in context.node_hints
                        ],
                        "semantics": context.metadata.get("semantics"),
                    }
                    for context in contexts
                    if context.metadata.get("call_id") == record.call_id
                ],
                stage_diagnostics(
                    artifact_values, "rendering", call_id=record.call_id
                ),
            )
        chunk = next(
            (
                item
                for item in artifact_values.get(CHUNK_ARTIFACTS.relpath, [])
                if item.chunk_id == unit_id
            ),
            None,
        )
        if chunk is None:
            return {}, []
        return (
            {"order_index": chunk.order_index, "text": chunk.text},
            stage_diagnostics(artifact_values, "rendering", call_id=record.call_id),
        )
    return {}, []


def packing_unit(
    run_id: str, artifact_values: dict[str, Any], unit_id: str
) -> dict[str, Any]:
    plan = artifact_values.get(CHUNK_PLAN.relpath)
    contracts = artifact_values.get(RENDER_CONTRACTS.relpath, [])
    if plan is None:
        raise NotFound("packing artifact is unavailable")
    chunk = next((item for item in plan.chunks if item.chunk_id == unit_id), None)
    contract = next((item for item in contracts if item.chunk_id == unit_id), None)
    if chunk is None:
        raise NotFound(f"packing unit {unit_id!r} not found")
    return {
        "run_id": run_id,
        "stage": "packing",
        "unit_id": unit_id,
        "kind": "packing_chunk",
        "origin_run_id": artifact_values["origins"].get(CHUNK_PLAN.relpath),
        "input_blocks": {
            "policy": _plain(plan.policy),
            "nodes": "\n\n".join(
                "\n".join(
                    line
                    for line in (
                        f"[{node.order_in_chunk + 1}] role={node.role.value} "
                        f"treatment={node.treatment.value}",
                        f"SUM {node.summary}" if node.summary else "",
                        f"directive: {node.directive}" if node.directive else "",
                        (
                            f"narrative_function: {node.narrative_function}"
                            if node.narrative_function
                            else ""
                        ),
                    )
                    if line
                )
                for node in contract.nodes
            )
            if contract
            else "",
            "materials": "\n\n".join(
                material.content for material in contract.materials
            )
            if contract
            else "",
        },
        "compiled_output": {
            "order_index": chunk.order_index,
            "node_count": len(chunk.node_ids),
            "material_count": len(chunk.material_ids),
            "estimated_material_tokens": chunk.estimated_material_tokens,
            "overloaded_caps": chunk.metadata.get("overloaded_caps", []),
        },
        "diagnostics": (
            [{"kind": "overloaded_chunk", "caps": chunk.metadata["overloaded_caps"]}]
            if chunk.metadata.get("overloaded_caps")
            else []
        ),
        "call_links": [],
        "technical": {"input_refs": [*chunk.node_ids, *chunk.material_ids]},
    }


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}: {item}" for key, item in value.items())
    return str(value)
