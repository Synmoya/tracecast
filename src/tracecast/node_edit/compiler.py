"""Program-owned compilation for Node Edit blocks and fallback results.

This module owns the mechanical K/C/O boundary: alias expansion, complete
partition validation, condensed-material construction, and the conservative
all-keep fallback.  It does not load prompts, dispatch calls, or decide when a
repair wave runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.domain import (
    AliasManifest,
    ContentKind,
    MaterialAtom,
    MaterialEdit,
    MaterialOmission,
    NodeEditOperation,
    NodeEditResult,
    Provenance,
    TrustTier,
    atom_id,
)

from .dsl import RawNodeBlock, RawOp
from .projection import BatchNode

_TRUST_RANK = {TrustTier.canonical: 2, TrustTier.high: 1, TrustTier.low: 0}


@dataclass(frozen=True)
class NodeEditFailure:
    """Program-owned failure kept separate from model-facing repair guidance."""

    code: str
    detail: str


@dataclass(frozen=True)
class CompiledNode:
    """One validated or conservatively recovered node and its visible materials."""

    result: NodeEditResult
    edited_materials: tuple[MaterialAtom, ...]


def failure(code: str, detail: str) -> NodeEditFailure:
    """Build a typed Node Edit compilation failure."""
    return NodeEditFailure(code=code, detail=detail)


def select_node_block(
    blocks_for_alias: Sequence[RawNodeBlock],
) -> tuple[RawNodeBlock | None, NodeEditFailure | None]:
    """Pick the single complete block for a candidate alias."""
    if not blocks_for_alias:
        return None, failure("missing_node_block", "no NODE block emitted for this candidate")
    if len(blocks_for_alias) > 1:
        return None, failure(
            "duplicate_node_block",
            f"{len(blocks_for_alias)} NODE blocks emitted for this candidate",
        )
    block = blocks_for_alias[0]
    if block.malformed:
        return None, failure(
            "malformed_node_block",
            "malformed NODE block: " + "; ".join(block.issues),
        )
    return block, None


def compile_all_keep(
    batch_node: BatchNode,
    *,
    protocol: str,
    fallback_reason: str | None,
) -> CompiledNode:
    """Conservatively recover one node by retaining its entire material pool."""
    edits = [_keep_edit(atom.id, None) for atom in batch_node.pool]
    metadata: dict[str, Any] = {"node_edit_protocol": protocol}
    if fallback_reason is not None:
        metadata["fallback"] = "all_keep"
        metadata["fallback_reason"] = fallback_reason
    result = NodeEditResult(
        candidate_id=batch_node.candidate_id,
        selection=batch_node.decision,
        edits=edits,
        omitted=[],
        metadata=metadata,
    )
    return CompiledNode(result=result, edited_materials=tuple(batch_node.pool))


def compile_node_block(
    batch_node: BatchNode,
    block: RawNodeBlock,
    material_manifest: AliasManifest,
    atoms_by_id: Mapping[str, MaterialAtom],
    *,
    protocol: str,
) -> CompiledNode | NodeEditFailure:
    """Validate and compile one parsed block against its node-local pool."""
    pool_set = {atom.id for atom in batch_node.pool}
    assigned: dict[str, str] = {}
    edits: list[MaterialEdit] = []
    omissions: list[MaterialOmission] = []
    edited: list[MaterialAtom] = []

    for op in block.ops:
        resolved = _resolve_operation_atoms(
            op,
            material_manifest,
            pool_set,
            assigned,
            atoms_by_id,
        )
        if isinstance(resolved, NodeEditFailure):
            return resolved
        operation_failure = _compile_operation(
            op,
            resolved,
            edits=edits,
            omissions=omissions,
            edited=edited,
            protocol=protocol,
        )
        if operation_failure is not None:
            return operation_failure

    uncovered = pool_set - set(assigned)
    if uncovered:
        return failure(
            "incomplete_partition",
            f"{len(uncovered)} material(s) left unpartitioned",
        )

    result = NodeEditResult(
        candidate_id=batch_node.candidate_id,
        selection=batch_node.decision,
        edits=edits,
        omitted=omissions,
        metadata={"node_edit_protocol": protocol},
    )
    return CompiledNode(result=result, edited_materials=tuple(edited))


def _resolve_operation_atoms(
    op: RawOp,
    material_manifest: AliasManifest,
    pool_set: set[str],
    assigned: dict[str, str],
    atoms_by_id: Mapping[str, MaterialAtom],
) -> list[MaterialAtom] | NodeEditFailure:
    """Expand one operation's aliases and enforce node-local single assignment."""
    atoms: list[MaterialAtom] = []
    for alias in op.aliases:
        try:
            resolved = material_manifest.resolve(alias)
        except (KeyError, ValueError):
            return failure(
                "unknown_material_alias",
                f"material alias {alias!r} does not resolve to a pool material",
            )
        if resolved not in pool_set:
            return failure(
                "cross_node_material_alias",
                f"material alias {alias!r} points outside this node's pool",
            )
        if resolved in assigned:
            return failure(
                "duplicate_material_assignment",
                f"material {alias!r} assigned twice ({assigned[resolved]} and {op.op})",
            )
        assigned[resolved] = op.op
        atoms.append(atoms_by_id[resolved])
    return atoms


def _compile_operation(
    op: RawOp,
    atoms: list[MaterialAtom],
    *,
    edits: list[MaterialEdit],
    omissions: list[MaterialOmission],
    edited: list[MaterialAtom],
    protocol: str,
) -> NodeEditFailure | None:
    """Compile one already-resolved K/C/O operation into its node accumulators."""
    if op.op == "K":
        for atom in atoms:
            edits.append(_keep_edit(atom.id, op.reason))
            edited.append(atom)
    elif op.op == "O":
        if not op.reason:
            return failure("missing_omit_reason", "omit row has no audit reason")
        for atom in atoms:
            omissions.append(MaterialOmission(material_id=atom.id, reason=op.reason))
    elif op.op == "C":
        condense_error = _validate_condense(atoms, op.content)
        if condense_error is not None:
            return condense_error
        condensed = _build_condensed_atom(
            atoms,
            op.content or "",
            protocol=protocol,
        )
        edits.append(
            MaterialEdit(
                operation=NodeEditOperation.condense,
                source_material_ids=[atom.id for atom in atoms],
                result_material=condensed,
                reason=op.reason,
            )
        )
        edited.append(condensed)
    return None


def with_producer_metadata(
    compiled: CompiledNode,
    *,
    unit_id: str,
    call_id: str | None,
) -> CompiledNode:
    """Attach call lineage without changing a node's semantic edit identity."""
    metadata = dict(compiled.result.metadata)
    metadata.update({"producer_unit_id": unit_id, "producer_call_id": call_id})
    return CompiledNode(
        result=compiled.result.model_copy(update={"metadata": metadata}),
        edited_materials=compiled.edited_materials,
    )


def _keep_edit(atom_id_: str, reason: str | None) -> MaterialEdit:
    return MaterialEdit(
        operation=NodeEditOperation.keep,
        source_material_ids=[atom_id_],
        reason=reason,
    )


def _validate_condense(
    src_atoms: list[MaterialAtom],
    op_content: str | None,
) -> NodeEditFailure | None:
    """Check non-empty content, source locality, and source-order continuity."""
    if not op_content or not op_content.strip():
        return failure("empty_condense_content", "condense produced empty TXT content")
    if len({atom.provenance.source_ref for atom in src_atoms}) > 1:
        return failure(
            "mixed_source_condense",
            "condense merges materials from more than one source",
        )
    orders = sorted(atom.source_order for atom in src_atoms)
    if orders and orders[-1] - orders[0] != len(orders) - 1:
        return failure(
            "noncontiguous_condense",
            "condense materials are not contiguous in source order",
        )
    return None


def _build_condensed_atom(
    src_atoms: list[MaterialAtom],
    content: str,
    *,
    protocol: str,
) -> MaterialAtom:
    """Materialize a run-scoped condensed atom from one valid source run."""
    lineage = [atom.id for atom in src_atoms]
    base = src_atoms[0].provenance
    source_ref = base.source_ref
    piece_refs: list[str] = []
    for atom in src_atoms:
        for piece in atom.provenance.piece_refs:
            if piece not in piece_refs:
                piece_refs.append(piece)
    span_starts = [
        atom.provenance.span_start for atom in src_atoms if atom.provenance.span_start is not None
    ]
    span_ends = [
        atom.provenance.span_end for atom in src_atoms if atom.provenance.span_end is not None
    ]
    provenance = Provenance(
        source_ref=source_ref,
        source_type=base.source_type,
        source_class=base.source_class,
        source_content_hash=base.source_content_hash,
        fetched_at=base.fetched_at,
        span_start=min(span_starts) if span_starts else None,
        span_end=max(span_ends) if span_ends else None,
        piece_refs=piece_refs,
        extractor_version=protocol,
    )
    kinds = {atom.content_kind for atom in src_atoms}
    content_kind = next(iter(kinds)) if len(kinds) == 1 else ContentKind.narration
    trust_tier = min(
        src_atoms,
        key=lambda atom: _TRUST_RANK[atom.trust_tier],
    ).trust_tier
    tags = sorted({tag for atom in src_atoms for tag in atom.tags})
    return MaterialAtom(
        id=atom_id(source_ref, piece_refs, lineage),
        content=content,
        language=src_atoms[0].language,
        content_kind=content_kind,
        source_order=min(atom.source_order for atom in src_atoms),
        trust_tier=trust_tier,
        provenance=provenance,
        tags=tags,
        source_material_ids=lineage,
    )
