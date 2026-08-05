"""Report-only audits: traceability, contract consistency, text hygiene, LLM ledger.

Every check here only describes what it found -- nothing mutates an artifact, gates
the chain, or auto-repairs. Findings are emitted as ``AuditReportItem``s, and a
finding for a missing reference is
only raised when the referenced artifact class was actually supplied, so auditing a
partial slice does not cascade false positives.
"""

from __future__ import annotations

from collections import Counter

from tracecast.domain import (
    AuditReportItem,
    MaterialAtom,
    NodeEditOperation,
    ProviderStatus,
)

from ._common import make_item
from .context import (
    CallAuditSummary,
    TraceabilitySummary,
    ValidationContext,
    ValidationReport,
)
from .kinds import AuditKind, Level, Stage
from .rendered_text import audit_rendered_text

# Audit-only field names that a model-facing projection must not expose.
_AUDIT_ONLY_KEYS = frozenset(
    {
        "input_hash",
        "output_hash",
        "content_hash",
        "source_content_hash",
        "raw_request_path",
        "raw_response_path",
        "extractor_version",
        "storage_path",
        "path",
    }
)


# --- helpers ---------------------------------------------------------------


def _known_atom_ids(ctx: ValidationContext) -> dict[str, MaterialAtom]:
    """All material ids the run can resolve: ingestion atoms + run-scoped condensed."""
    index: dict[str, MaterialAtom] = {atom.id: atom for atom in ctx.atoms}
    for atom in ctx.edited_materials:
        index.setdefault(atom.id, atom)
    if ctx.node_edit_plan is not None:
        for result in ctx.node_edit_plan.node_edits:
            for edit in result.edits:
                if edit.result_material is not None:
                    index.setdefault(edit.result_material.id, edit.result_material)
    return index


# --- traceability / fact source -------------------------------------------


def audit_traceability(ctx: ValidationContext) -> list[AuditReportItem]:
    """Audit the evidence-grounding chain.

    The only fact-bearing payload the renderer sees is ``RenderContract.materials[]
    .content``; ``summary``/``directive``/``reason``/instructions are not
    traced here, which *is* the structural enforcement that they are never fact
    sources. Findings: ``missing_source_snapshot``, ``missing_piece_ref``,
    ``material_not_traceable``, ``chunk_not_traceable``, ``condense_lineage_missing``.
    """
    items: list[AuditReportItem] = []
    snapshot_ids = {snap.source_id for snap in ctx.snapshots}
    piece_ids = {piece.piece_id for piece in ctx.pieces}
    atoms = _known_atom_ids(ctx)
    contract_ids = {contract.contract_id for contract in ctx.contracts}

    # atom -> piece -> snapshot
    for atom in atoms.values():
        prov = atom.provenance
        if snapshot_ids and prov.source_ref not in snapshot_ids:
            items.append(
                make_item(
                    Stage.ingestion,
                    AuditKind.missing_source_snapshot,
                    f"MaterialAtom {atom.id} provenance source_ref "
                    f"{prov.source_ref!r} has no SourceSnapshot.",
                    artifact_ref=atom.id,
                    trace_refs=[f"material:{atom.id}", f"source:{prov.source_ref}"],
                )
            )
        if piece_ids:
            for piece_ref in prov.piece_refs:
                if piece_ref not in piece_ids:
                    items.append(
                        make_item(
                            Stage.ingestion,
                            AuditKind.missing_piece_ref,
                            f"MaterialAtom {atom.id} references missing "
                            f"SourcePiece {piece_ref!r}.",
                            artifact_ref=atom.id,
                            trace_refs=[f"material:{atom.id}", f"piece:{piece_ref}"],
                        )
                    )

    # piece -> snapshot
    if snapshot_ids:
        for piece in ctx.pieces:
            if piece.source_id not in snapshot_ids:
                items.append(
                    make_item(
                        Stage.ingestion,
                        AuditKind.missing_source_snapshot,
                        f"SourcePiece {piece.piece_id} references missing "
                        f"SourceSnapshot {piece.source_id!r}.",
                        artifact_ref=piece.piece_id,
                        trace_refs=[f"piece:{piece.piece_id}", f"source:{piece.source_id}"],
                    )
                )

    # chunk -> contract
    if ctx.contracts:
        for chunk in ctx.chunks:
            if chunk.contract_id not in contract_ids:
                items.append(
                    make_item(
                        Stage.rendering,
                        AuditKind.chunk_not_traceable,
                        f"ChunkArtifact {chunk.chunk_id} references missing "
                        f"RenderContract {chunk.contract_id!r}.",
                        artifact_ref=chunk.chunk_id,
                        trace_refs=[f"chunk:{chunk.chunk_id}", f"contract:{chunk.contract_id}"],
                    )
                )

    # contract material -> atom (+ condense lineage -> atom[])
    for contract in ctx.contracts:
        local_refs = {material.material_ref for material in contract.materials}
        for material in contract.materials:
            base_refs = [
                f"contract:{contract.contract_id}",
                f"material_ref:{material.material_ref}",
                f"material:{material.source_material_id}",
            ]
            if atoms and material.source_material_id not in atoms:
                items.append(
                    make_item(
                        Stage.rendering,
                        AuditKind.material_not_traceable,
                        f"RenderContract {contract.contract_id} material "
                        f"{material.material_ref} ({material.source_material_id}) "
                        "does not resolve to a MaterialAtom.",
                        artifact_ref=contract.contract_id,
                        trace_refs=base_refs,
                    )
                )
            if material.operation is NodeEditOperation.condense:
                if not material.source_material_refs:
                    items.append(
                        make_item(
                            Stage.node_edit,
                            AuditKind.condense_lineage_missing,
                            f"Condensed material {material.material_ref} in contract "
                            f"{contract.contract_id} carries no source_material_refs.",
                            artifact_ref=contract.contract_id,
                            trace_refs=base_refs,
                        )
                    )
                elif atoms:
                    for lineage_ref in material.source_material_refs:
                        if lineage_ref not in atoms and lineage_ref not in local_refs:
                            items.append(
                                make_item(
                                    Stage.node_edit,
                                    AuditKind.material_not_traceable,
                                    f"Condensed material {material.material_ref} lineage "
                                    f"ref {lineage_ref!r} does not resolve to a "
                                    "MaterialAtom.",
                                    artifact_ref=contract.contract_id,
                                    trace_refs=[*base_refs, f"lineage:{lineage_ref}"],
                                )
                            )
    return items


def summarize_traceability(
    ctx: ValidationContext, items: list[AuditReportItem]
) -> TraceabilitySummary:
    atoms = _known_atom_ids(ctx)
    contract_ids = {contract.contract_id for contract in ctx.contracts}
    chunks_traceable = sum(
        1 for chunk in ctx.chunks if not ctx.contracts or chunk.contract_id in contract_ids
    )
    materials_total = sum(len(contract.materials) for contract in ctx.contracts)
    materials_traceable = sum(
        1
        for contract in ctx.contracts
        for material in contract.materials
        if not atoms or material.source_material_id in atoms
    )
    broken_kinds = {
        AuditKind.chunk_not_traceable,
        AuditKind.material_not_traceable,
        AuditKind.missing_piece_ref,
        AuditKind.missing_source_snapshot,
        AuditKind.condense_lineage_missing,
    }
    broken = tuple(item.issue_id for item in items if item.kind in {str(k) for k in broken_kinds})
    return TraceabilitySummary(
        chunks_total=len(ctx.chunks),
        chunks_traceable=chunks_traceable,
        materials_total=materials_total,
        materials_traceable=materials_traceable,
        broken_issue_ids=broken,
    )


# --- contract consistency --------------------------------------------------


def audit_contract_consistency(ctx: ValidationContext) -> list[AuditReportItem]:
    """Findings: ``unexpected_omitted_material``, ``unexpected_unselected_node``,
    ``dangling_reference``, ``projection_contains_audit_field``."""
    items: list[AuditReportItem] = []

    omitted_ids: set[str] = set()
    if ctx.node_edit_plan is not None:
        for result in ctx.node_edit_plan.node_edits:
            omitted_ids.update(omission.material_id for omission in result.omitted)

    accepted_nodes: set[str] = set()
    if ctx.selection_plan is not None:
        accepted_nodes.update(ctx.selection_plan.selected_sequence)
        accepted_nodes.update(
            decision.candidate_id
            for decision in ctx.selection_plan.decisions
            if str(decision.status) == "accepted"
        )

    # omitted material must not re-enter the render-visible path
    if omitted_ids and ctx.chunk_plan is not None:
        for chunk in ctx.chunk_plan.chunks:
            for material_id in chunk.material_ids:
                if material_id in omitted_ids:
                    items.append(
                        make_item(
                            Stage.packing,
                            AuditKind.unexpected_omitted_material,
                            f"ChunkPlan chunk {chunk.chunk_id} includes omitted "
                            f"material {material_id}.",
                            artifact_ref=chunk.chunk_id,
                            trace_refs=[f"chunk:{chunk.chunk_id}", f"material:{material_id}"],
                        )
                    )
    if omitted_ids:
        for contract in ctx.contracts:
            for material in contract.materials:
                if material.source_material_id in omitted_ids:
                    items.append(
                        make_item(
                            Stage.packing,
                            AuditKind.unexpected_omitted_material,
                            f"RenderContract {contract.contract_id} includes omitted "
                            f"material {material.source_material_id}.",
                            artifact_ref=contract.contract_id,
                            trace_refs=[
                                f"contract:{contract.contract_id}",
                                f"material:{material.source_material_id}",
                            ],
                        )
                    )

    # only accepted nodes may flow downstream
    if accepted_nodes and ctx.chunk_plan is not None:
        for chunk in ctx.chunk_plan.chunks:
            for node_id in chunk.node_ids:
                if node_id not in accepted_nodes:
                    items.append(
                        make_item(
                            Stage.packing,
                            AuditKind.unexpected_unselected_node,
                            f"ChunkPlan chunk {chunk.chunk_id} includes unselected "
                            f"node {node_id}.",
                            artifact_ref=chunk.chunk_id,
                            trace_refs=[f"chunk:{chunk.chunk_id}", f"node:{node_id}"],
                        )
                    )
    if accepted_nodes:
        for contract in ctx.contracts:
            for node in contract.nodes:
                if node.source_node_id not in accepted_nodes:
                    items.append(
                        make_item(
                            Stage.packing,
                            AuditKind.unexpected_unselected_node,
                            f"RenderContract {contract.contract_id} includes unselected "
                            f"node {node.source_node_id}.",
                            artifact_ref=contract.contract_id,
                            trace_refs=[
                                f"contract:{contract.contract_id}",
                                f"node:{node.source_node_id}",
                            ],
                        )
                    )

    items.extend(_audit_dangling_refs(ctx))
    items.extend(_audit_projection_fields(ctx))
    return items


def _audit_dangling_refs(ctx: ValidationContext) -> list[AuditReportItem]:
    items: list[AuditReportItem] = []
    plan = ctx.selection_plan
    node_plan = ctx.node_edit_plan
    chunk_plan = ctx.chunk_plan

    if node_plan is not None and plan is not None and node_plan.selection_plan_id != plan.plan_id:
        items.append(
            make_item(
                Stage.node_edit,
                AuditKind.dangling_reference,
                f"NodeEditPlan {node_plan.plan_id} selection_plan_id "
                f"{node_plan.selection_plan_id!r} != SelectionPlan {plan.plan_id}.",
                artifact_ref=node_plan.plan_id,
            )
        )
    if (
        node_plan is not None
        and ctx.candidate_graph is not None
        and node_plan.candidate_graph_id != ctx.candidate_graph.graph_id
    ):
        items.append(
            make_item(
                Stage.node_edit,
                AuditKind.dangling_reference,
                f"NodeEditPlan {node_plan.plan_id} candidate_graph_id "
                f"{node_plan.candidate_graph_id!r} != CandidateGraph "
                f"{ctx.candidate_graph.graph_id}.",
                artifact_ref=node_plan.plan_id,
            )
        )
    if chunk_plan is not None:
        if plan is not None and chunk_plan.selection_plan_id != plan.plan_id:
            items.append(
                make_item(
                    Stage.packing,
                    AuditKind.dangling_reference,
                    f"ChunkPlan {chunk_plan.plan_id} selection_plan_id "
                    f"{chunk_plan.selection_plan_id!r} != SelectionPlan {plan.plan_id}.",
                    artifact_ref=chunk_plan.plan_id,
                )
            )
        if node_plan is not None and chunk_plan.node_edit_plan_id != node_plan.plan_id:
            items.append(
                make_item(
                    Stage.packing,
                    AuditKind.dangling_reference,
                    f"ChunkPlan {chunk_plan.plan_id} node_edit_plan_id "
                    f"{chunk_plan.node_edit_plan_id!r} != NodeEditPlan {node_plan.plan_id}.",
                    artifact_ref=chunk_plan.plan_id,
                )
            )
        chunk_ids = {chunk.chunk_id for chunk in chunk_plan.chunks}
        for contract in ctx.contracts:
            if contract.chunk_id not in chunk_ids:
                items.append(
                    make_item(
                        Stage.packing,
                        AuditKind.dangling_reference,
                        f"RenderContract {contract.contract_id} references chunk "
                        f"{contract.chunk_id!r} absent from ChunkPlan.",
                        artifact_ref=contract.contract_id,
                        trace_refs=[
                            f"contract:{contract.contract_id}",
                            f"chunk:{contract.chunk_id}",
                        ],
                    )
                )
    return items


def _audit_projection_fields(ctx: ValidationContext) -> list[AuditReportItem]:
    items: list[AuditReportItem] = []
    for contract in ctx.contracts:
        leaked = sorted(_AUDIT_ONLY_KEYS.intersection(contract.metadata))
        if leaked:
            items.append(
                make_item(
                    Stage.packing,
                    AuditKind.projection_contains_audit_field,
                    f"RenderContract {contract.contract_id} metadata exposes "
                    f"audit-only field(s): {', '.join(leaked)}.",
                    level=Level.warning,
                    artifact_ref=contract.contract_id,
                    trace_refs=[f"contract:{contract.contract_id}"],
                    metadata={"fields": leaked},
                )
            )
    return items


# --- LLM call ledger -------------------------------------------------------


def audit_llm_calls(ctx: ValidationContext) -> list[AuditReportItem]:
    """Findings: ``prompt_version_missing``, ``raw_response_missing``,
    ``call_ledger_missing``. Cost is a configured estimate, never an invoice."""
    items: list[AuditReportItem] = []
    available = ctx.available_response_paths

    for record in ctx.call_records:
        if record.prompt_version is None:
            items.append(
                make_item(
                    Stage.llm,
                    AuditKind.prompt_version_missing,
                    f"LlmCallRecord {record.call_id} has no prompt_version.",
                    level=Level.warning,
                    artifact_ref=record.call_id,
                    call_id=record.call_id,
                )
            )
        produced = record.provider_status is ProviderStatus.success
        if available is not None:
            missing = not record.raw_response_path or record.raw_response_path not in available
        else:
            missing = produced and not record.raw_response_path
        if missing and (produced or record.raw_response_path):
            items.append(
                make_item(
                    Stage.llm,
                    AuditKind.raw_response_missing,
                    f"LlmCallRecord {record.call_id} has no resolvable raw response.",
                    level=Level.warning,
                    artifact_ref=record.call_id,
                    call_id=record.call_id,
                    raw_response_path=record.raw_response_path,
                )
            )

    items.extend(_audit_missing_call_records(ctx))
    return items


def _audit_missing_call_records(ctx: ValidationContext) -> list[AuditReportItem]:
    items: list[AuditReportItem] = []
    known = {record.call_id for record in ctx.call_records}
    referenced: set[str] = set()
    for chunk in ctx.chunks:
        call_id = chunk.metadata.get("call_id")
        if isinstance(call_id, str):
            referenced.add(call_id)
    for contract in ctx.contracts:
        call_id = contract.metadata.get("call_id")
        if isinstance(call_id, str):
            referenced.add(call_id)
    for call_id in sorted(referenced - known):
        items.append(
            make_item(
                Stage.llm,
                AuditKind.call_ledger_missing,
                f"Artifact references call_id {call_id} absent from the call ledger.",
                level=Level.warning,
                call_id=call_id,
            )
        )
    return items


def summarize_calls(ctx: ValidationContext) -> CallAuditSummary:
    records = list(ctx.call_records)
    by_stage = Counter(record.stage for record in records)
    available = ctx.available_response_paths
    missing_raw = 0
    for record in records:
        produced = record.provider_status is ProviderStatus.success
        if available is not None:
            if (not record.raw_response_path or record.raw_response_path not in available) and (
                produced or record.raw_response_path
            ):
                missing_raw += 1
        elif produced and not record.raw_response_path:
            missing_raw += 1
    return CallAuditSummary(
        calls_total=len(records),
        calls_by_stage=dict(by_stage),
        total_estimated_cost=sum(record.cost or 0.0 for record in records),
        missing_raw_response=missing_raw,
        missing_prompt_version=sum(1 for record in records if record.prompt_version is None),
    )


# --- orchestrator ----------------------------------------------------------


def run_validation(ctx: ValidationContext) -> ValidationReport:
    """Run every report-only audit and aggregate the result.

    Report-only: this never mutates an input record, never gates the chain, and
    never auto-repairs. Pre-existing ``stage_reports`` are aggregated as-is.
    """
    items: list[AuditReportItem] = list(ctx.stage_reports)
    items.extend(audit_traceability(ctx))
    items.extend(audit_contract_consistency(ctx))
    items.extend(audit_rendered_text(ctx))
    items.extend(audit_llm_calls(ctx))

    traceability = summarize_traceability(ctx, items)
    call_audit = summarize_calls(ctx)

    deduped: list[AuditReportItem] = []
    seen: set[str] = set()
    for item in items:
        if item.issue_id in seen:
            continue
        seen.add(item.issue_id)
        deduped.append(item)

    return ValidationReport(
        items=tuple(deduped),
        traceability=traceability,
        call_audit=call_audit,
    )
