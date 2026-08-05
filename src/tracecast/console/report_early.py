"""Console reports for Ingestion through Node Edit."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from tracecast.domain import estimate_tokens
from tracecast.storage import (
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    EDITED_MATERIALS,
    MATERIAL_ATOMS,
    NODE_EDIT_PLAN,
    SELECTION_PLAN,
    SOURCE_PIECES,
    SOURCE_WINDOWS,
)

from .report_common import (
    distribution,
    excerpt,
    origins,
    ratio,
    sample_even,
    sources,
    stage_diagnostics,
    unavailable,
)


def ingestion(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(
        root, run_id, SOURCE_PIECES, SOURCE_WINDOWS, MATERIAL_ATOMS, AUDIT_REPORT
    )
    required = [SOURCE_PIECES.relpath, MATERIAL_ATOMS.relpath]
    if any(path not in resolved for path in required):
        return unavailable(run_id, "ingestion", required)
    pieces = resolved[SOURCE_PIECES.relpath].read_records(SOURCE_PIECES)
    atoms = resolved[MATERIAL_ATOMS.relpath].read_records(MATERIAL_ATOMS)
    windows = (
        resolved[SOURCE_WINDOWS.relpath].read_records(SOURCE_WINDOWS)
        if SOURCE_WINDOWS.relpath in resolved
        else []
    )
    atom_by_piece = {
        piece_id: atom for atom in atoms for piece_id in atom.provenance.piece_refs
    }
    under: list[dict[str, Any]] = []
    tiled_pieces: set[str] = set()
    for window in windows:
        tiled_pieces.update(window.piece_ids)
        unique_atoms = {
            atom_by_piece[piece_id].id
            for piece_id in window.piece_ids
            if piece_id in atom_by_piece
        }
        one_to_one = ratio(len(unique_atoms), len(window.piece_ids))
        if len(window.piece_ids) >= 8 and one_to_one >= 0.85:
            under.append(
                {
                    "window_id": window.window_id,
                    "source_id": window.source_id,
                    "pieces": len(window.piece_ids),
                    "atoms": len(unique_atoms),
                    "one_to_one_ratio": one_to_one,
                }
            )
    piece_ids = {piece.piece_id for piece in pieces}
    kind_counts = Counter(atom.content_kind.value for atom in atoms)
    diagnostics = stage_diagnostics(resolved, "ingestion")
    return {
        "origins": origins(resolved),
        "summary": {
            "sources": len({piece.source_id for piece in pieces}),
            "pieces": len(pieces),
            "windows": len(windows),
            "atoms": len(atoms),
            "material_tokens": sum(estimate_tokens(atom.content) for atom in atoms),
            "aggregation_factor": ratio(len(pieces), len(atoms)),
            "tiling_coverage": ratio(len(piece_ids & tiled_pieces), len(piece_ids)),
            "underaggregated_windows": len(under),
            "diagnostic_count": len(diagnostics),
            "content_kinds": dict(sorted(kind_counts.items())),
            "piece_tokens": distribution([estimate_tokens(piece.text) for piece in pieces]),
            "atom_tokens": distribution([estimate_tokens(atom.content) for atom in atoms]),
            "window_tokens": distribution(
                [window.estimated_input_tokens for window in windows]
            ),
        },
        "underaggregated_windows": under[:12],
        "samples": [
            {
                "source_id": atom.provenance.source_ref,
                "source_order": atom.source_order,
                "content_kind": atom.content_kind.value,
                "piece_count": len(atom.provenance.piece_refs),
                "content": excerpt(atom.content),
            }
            for atom in sample_even(atoms, 12)
        ],
        "diagnostics": diagnostics,
    }


def planning(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(root, run_id, CANDIDATE_GRAPH, MATERIAL_ATOMS, AUDIT_REPORT)
    if CANDIDATE_GRAPH.relpath not in resolved:
        return unavailable(run_id, "planning", [CANDIDATE_GRAPH.relpath])
    graph = resolved[CANDIDATE_GRAPH.relpath].read_model(CANDIDATE_GRAPH)
    diagnostics = stage_diagnostics(resolved, "planning")
    edge_counts = Counter(edge.edge_type for edge in graph.edges)
    material_counts = [len(node.material_ids) for node in graph.nodes]
    material_token_rows = [
        node.material_estimated_tokens
        for node in graph.nodes
        if node.material_estimated_tokens is not None
    ]
    if len(material_token_rows) == len(graph.nodes):
        material_tokens = sum(material_token_rows)
    elif MATERIAL_ATOMS.relpath in resolved:
        material_ids = {material_id for node in graph.nodes for material_id in node.material_ids}
        material_tokens = sum(
            estimate_tokens(atom.content)
            for atom in resolved[MATERIAL_ATOMS.relpath].read_records(MATERIAL_ATOMS)
            if atom.id in material_ids
        )
    else:
        material_tokens = None
    summary_tokens = sum(estimate_tokens(node.summary) for node in graph.nodes)
    compression = graph.metadata.get("node_compression") or {}
    compression_rounds = compression.get("rounds") or []
    initial_nodes = (compression.get("initial") or {}).get("nodes")
    if initial_nodes is None:
        initial_nodes = len(graph.nodes)
    return {
        "origins": origins(resolved),
        "summary": {
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "coverage": graph.metadata.get("coverage") or {},
            "planning_windows": graph.metadata.get("planning_windows"),
            "node_material_density": distribution(material_counts),
            "material_tokens": material_tokens,
            "summary_tokens": summary_tokens,
            "summary_retention_ratio": (
                ratio(summary_tokens, material_tokens) if material_tokens is not None else None
            ),
            "summary_compression_factor": (
                round(material_tokens / summary_tokens, 2)
                if material_tokens is not None and summary_tokens
                else None
            ),
            "initial_nodes": initial_nodes,
            "final_nodes": len(graph.nodes),
            "target_nodes": compression.get("target_node_count"),
            "target_met": compression.get("target_met"),
            "seed_batches": graph.metadata.get("planning_windows"),
            "compression_calls": sum(
                int(item.get("call_count") or 0)
                for item in compression_rounds
                if isinstance(item, dict)
            ),
            "compression": compression,
            "edge_types": dict(sorted(edge_counts.items())),
            "diagnostic_count": len(diagnostics),
        },
        "samples": [
            {
                "summary": node.summary,
                "narrative_function": node.narrative_function,
                "materials": len(node.material_ids),
                "source_window": node.source_window,
                "compression_round": node.metadata.get("compression_round"),
            }
            for node in sample_even(graph.nodes, 12)
        ],
        "diagnostics": diagnostics,
    }


def selection(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(
        root, run_id, SELECTION_PLAN, CANDIDATE_GRAPH, MATERIAL_ATOMS, AUDIT_REPORT
    )
    required = [SELECTION_PLAN.relpath, CANDIDATE_GRAPH.relpath]
    if any(path not in resolved for path in required):
        return unavailable(run_id, "selection", required)
    plan = resolved[SELECTION_PLAN.relpath].read_model(SELECTION_PLAN)
    graph = resolved[CANDIDATE_GRAPH.relpath].read_model(CANDIDATE_GRAPH)
    node_by_id = {node.candidate_id: node for node in graph.nodes}
    atom_by_id = (
        {atom.id: atom for atom in resolved[MATERIAL_ATOMS.relpath].read_records(MATERIAL_ATOMS)}
        if MATERIAL_ATOMS.relpath in resolved
        else {}
    )
    accepted = [decision for decision in plan.decisions if decision.status.value == "accepted"]
    selected_materials = {
        material_id
        for candidate_id in set(plan.selected_sequence)
        for material_id in node_by_id.get(candidate_id, _EMPTY_NODE).material_ids
    }
    all_materials = {material_id for node in graph.nodes for material_id in node.material_ids}
    selected_tokens = sum(
        estimate_tokens(atom_by_id[mid].content)
        for mid in selected_materials
        if mid in atom_by_id
    )
    all_tokens = sum(
        estimate_tokens(atom_by_id[mid].content) for mid in all_materials if mid in atom_by_id
    )
    roles = Counter(decision.role.value for decision in accepted if decision.role is not None)
    treatments = Counter(
        decision.treatment.value for decision in accepted if decision.treatment is not None
    )
    source_counts = Counter(
        str(node_by_id[candidate_id].metadata.get("source_ref", "unknown"))
        for candidate_id in plan.selected_sequence
        if candidate_id in node_by_id
    )
    diagnostics = stage_diagnostics(resolved, "selection")
    return {
        "origins": origins(resolved),
        "summary": {
            "candidates": len(graph.nodes),
            "selected": len(plan.selected_sequence),
            "selection_rate": ratio(len(plan.selected_sequence), len(graph.nodes)),
            "material_retention": ratio(len(selected_materials), len(all_materials)),
            "token_retention": ratio(selected_tokens, all_tokens),
            "roles": dict(sorted(roles.items())),
            "treatments": dict(sorted(treatments.items())),
            "sources": dict(sorted(source_counts.items())),
            "diagnostic_count": len(diagnostics),
        },
        "samples": [
            {
                "order_index": index,
                "summary": node_by_id[candidate_id].summary,
                "role": next(
                    (d.role.value for d in accepted if d.candidate_id == candidate_id), None
                ),
                "treatment": next(
                    (d.treatment.value for d in accepted if d.candidate_id == candidate_id),
                    None,
                ),
                "directive": next(
                    (d.directive for d in accepted if d.candidate_id == candidate_id),
                    None,
                ),
            }
            for index, candidate_id in enumerate(plan.selected_sequence)
            if candidate_id in node_by_id
        ],
        "diagnostics": diagnostics,
    }


class _EmptyNode:
    material_ids: list[str] = []


_EMPTY_NODE = _EmptyNode()


def node_edit(root: Path, run_id: str) -> dict[str, Any]:
    resolved = sources(
        root, run_id, NODE_EDIT_PLAN, MATERIAL_ATOMS, EDITED_MATERIALS, AUDIT_REPORT
    )
    if NODE_EDIT_PLAN.relpath not in resolved:
        return unavailable(run_id, "node_edit", [NODE_EDIT_PLAN.relpath])
    plan = resolved[NODE_EDIT_PLAN.relpath].read_model(NODE_EDIT_PLAN)
    source_atoms = (
        {atom.id: atom for atom in resolved[MATERIAL_ATOMS.relpath].read_records(MATERIAL_ATOMS)}
        if MATERIAL_ATOMS.relpath in resolved
        else {}
    )
    counts: Counter[str] = Counter()
    source_tokens: Counter[str] = Counter()
    output_tokens: Counter[str] = Counter()
    condensed_ratios: list[float] = []
    fallback = repair_nodes = repair_recovered = lineage_rows = 0
    samples: list[dict[str, Any]] = []
    for result in plan.node_edits:
        if result.metadata.get("fallback") or result.metadata.get("fallback_reason"):
            fallback += 1
        producer_unit_id = result.metadata.get("producer_unit_id")
        if isinstance(producer_unit_id, str) and producer_unit_id.startswith("repair:"):
            repair_nodes += 1
            if not (result.metadata.get("fallback") or result.metadata.get("fallback_reason")):
                repair_recovered += 1
        local = Counter()
        for edit in result.edits:
            operation = edit.operation.value
            counts[operation] += 1
            local[operation] += 1
            before = sum(
                estimate_tokens(source_atoms[mid].content)
                for mid in edit.source_material_ids
                if mid in source_atoms
            )
            source_tokens[operation] += before
            if edit.result_material is not None:
                after = estimate_tokens(edit.result_material.content)
                output_tokens[operation] += after
                lineage_rows += bool(edit.result_material.source_material_ids)
                if before:
                    condensed_ratios.append(after / before)
            else:
                output_tokens[operation] += before
        counts["omit"] += len(result.omitted)
        local["omit"] += len(result.omitted)
        source_tokens["omit"] += sum(
            estimate_tokens(source_atoms[item.material_id].content)
            for item in result.omitted
            if item.material_id in source_atoms
        )
        if len(samples) < 16:
            samples.append(
                {
                    "role": result.selection.role.value if result.selection.role else None,
                    "treatment": (
                        result.selection.treatment.value if result.selection.treatment else None
                    ),
                    "operations": dict(local),
                    "fallback": bool(
                        result.metadata.get("fallback")
                        or result.metadata.get("fallback_reason")
                    ),
                }
            )
    diagnostics = stage_diagnostics(resolved, "node_edit")
    total_source_tokens = sum(source_tokens.values())
    total_output_tokens = sum(output_tokens.values())
    condense_count = counts.get("condense", 0)
    return {
        "origins": origins(resolved),
        "summary": {
            "nodes": len(plan.node_edits),
            "operations": dict(counts),
            "source_tokens": dict(source_tokens),
            "source_token_total": total_source_tokens,
            "source_token_ratios": {
                operation: ratio(tokens, total_source_tokens)
                for operation, tokens in sorted(source_tokens.items())
            },
            "output_tokens": dict(output_tokens),
            "output_token_total": total_output_tokens,
            "token_retention": ratio(total_output_tokens, total_source_tokens),
            "condense_compression_ratio": (
                round(sum(condensed_ratios) / len(condensed_ratios), 3)
                if condensed_ratios
                else None
            ),
            "fallback_nodes": fallback,
            "repair_nodes": repair_nodes,
            "repair_recovered": repair_recovered,
            "repair_exhausted": repair_nodes - repair_recovered,
            "condensed_with_lineage": lineage_rows,
            "condensed_lineage_rate": ratio(lineage_rows, condense_count),
            "diagnostic_count": len(diagnostics),
        },
        "samples": samples,
        "diagnostics": diagnostics,
    }
