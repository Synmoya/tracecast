"""Artifact, lineage, and relevance queries for the Console stage trace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tracecast.domain import LlmCallRecord
from tracecast.storage import (
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    EDITED_MATERIALS,
    LLM_CALL_RECORDS,
    MATERIAL_ATOMS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SELECTION_PLAN,
    SOURCE_PIECES,
    SOURCE_WINDOWS,
)

from . import inventory
from .context import ConsoleContext
from .errors import NotFound
from .trace_common import TRACE_STAGES

TRACE_ARTIFACT_SPECS = (
    SOURCE_PIECES,
    SOURCE_WINDOWS,
    MATERIAL_ATOMS,
    CANDIDATE_GRAPH,
    SELECTION_PLAN,
    NODE_EDIT_PLAN,
    EDITED_MATERIALS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    CHUNK_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    CHUNK_ARTIFACTS,
    AUDIT_REPORT,
)


def artifacts(ctx: ConsoleContext, run_id: str) -> dict[str, Any]:
    stores = inventory.resolve_artifact_sources(
        ctx.runs_root, run_id, TRACE_ARTIFACT_SPECS, required=False
    )
    values: dict[str, Any] = {
        "origins": {path: store.run_id for path, store in stores.items()}
    }
    for spec in TRACE_ARTIFACT_SPECS:
        store = stores.get(spec.relpath)
        if store is None:
            continue
        if spec.format.value == "json_model":
            values[spec.relpath] = store.read_model(spec)
        else:
            values[spec.relpath] = store.read_records(spec)
    return values


def stage_diagnostics(
    artifact_values: dict[str, Any],
    stage: str,
    *,
    call_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json")
        for item in artifact_values.get(AUDIT_REPORT.relpath, [])
        if item.stage.replace("-", "_") == stage
        and (call_id is None or item.call_id in {None, call_id})
    ]


def lineage_stores(root: Path, run_id: str) -> list[Any]:
    stores: list[Any] = []
    seen: set[str] = set()
    current = run_id
    while current not in seen:
        seen.add(current)
        store = inventory.open_run(root, current)
        stores.append(store)
        manifest = inventory.read_console_manifest(store) or {}
        parent = manifest.get("base_run_id")
        if not isinstance(parent, str) or not parent:
            break
        current = parent
    return stores


def call_records(root: Path, run_id: str) -> list[tuple[LlmCallRecord, str]]:
    records: list[tuple[LlmCallRecord, str]] = []
    for store in lineage_stores(root, run_id):
        if store.exists(LLM_CALL_RECORDS):
            records.extend(
                (record, store.run_id) for record in store.read_records(LLM_CALL_RECORDS)
            )
    return records


def trace_stage(record: LlmCallRecord) -> str:
    if record.work_unit is not None:
        kind = record.work_unit.kind
        if kind.startswith("node_edit"):
            return "node_edit"
        if kind.startswith("name_"):
            return "name_resolution"
        if kind.startswith("planning"):
            return "planning"
        if kind.startswith("selection"):
            return "selection"
        if kind.startswith("ingestion"):
            return "ingestion"
        if kind.startswith("render"):
            return "rendering"
    return record.stage.replace("-", "_")


def relevant_units(
    artifact_values: dict[str, Any], *, chunk_id: str | None
) -> dict[str, set[str]]:
    relevant: dict[str, set[str]] = {stage: set() for stage in TRACE_STAGES}
    contracts = artifact_values.get(RENDER_CONTRACTS.relpath, [])
    chunks = artifact_values.get(CHUNK_ARTIFACTS.relpath, [])
    plan = artifact_values.get(CHUNK_PLAN.relpath)
    if chunk_id is None:
        relevant["packing"].update(chunk.chunk_id for chunk in (plan.chunks if plan else []))
        return relevant
    contract = next((item for item in contracts if item.chunk_id == chunk_id), None)
    chunk = next((item for item in chunks if item.chunk_id == chunk_id), None)
    if contract is None and chunk is None:
        raise NotFound(f"chunk {chunk_id!r} not found in effective run lineage")
    relevant["rendering"].update({chunk_id, "fact-context"})
    relevant["name_resolution"].add(chunk_id)
    relevant["packing"].add(chunk_id)
    if contract is None:
        return relevant

    node_ids = {node.source_node_id for node in contract.nodes}
    material_ids = {material.source_material_id for material in contract.materials}
    edited = {
        atom.id: atom for atom in artifact_values.get(EDITED_MATERIALS.relpath, [])
    }
    expanded_materials: set[str] = set()
    for material_id in material_ids:
        atom = edited.get(material_id)
        if atom is not None and atom.source_material_ids:
            expanded_materials.update(atom.source_material_ids)
        else:
            expanded_materials.add(material_id)
    relevant["_material_refs"] = expanded_materials

    node_edit = artifact_values.get(NODE_EDIT_PLAN.relpath)
    if node_edit is not None:
        for result in node_edit.node_edits:
            if result.candidate_id in node_ids:
                producer = result.metadata.get("producer_unit_id")
                if isinstance(producer, str):
                    relevant["node_edit"].add(producer)

    graph = artifact_values.get(CANDIDATE_GRAPH.relpath)
    if graph is not None:
        for node in graph.nodes:
            if node.candidate_id not in node_ids:
                continue
            producer = node.metadata.get("producer_unit_id")
            if isinstance(producer, str):
                relevant["planning"].add(producer)
            relevant["planning"].update(
                unit
                for unit in node.metadata.get("compression_lineage_units", [])
                if isinstance(unit, str)
            )
        relevant["planning"].add("global-edge")
        relevant["selection"].update(
            {
                graph.graph_id,
                f"{graph.graph_id}:directives",
            }
        )

    atoms = {
        atom.id: atom for atom in artifact_values.get(MATERIAL_ATOMS.relpath, [])
    }
    piece_ids = {
        piece_id
        for material_id in expanded_materials
        if material_id in atoms
        for piece_id in atoms[material_id].provenance.piece_refs
    }
    for window in artifact_values.get(SOURCE_WINDOWS.relpath, []):
        if piece_ids.intersection(window.piece_ids):
            relevant["ingestion"].update({window.window_id, f"{window.window_id}:repair"})
    return relevant


def chunk_order(artifact_values: dict[str, Any], chunk_id: str) -> int | None:
    plan = artifact_values.get(CHUNK_PLAN.relpath)
    if plan is None:
        return None
    return next(
        (chunk.order_index for chunk in plan.chunks if chunk.chunk_id == chunk_id), None
    )
