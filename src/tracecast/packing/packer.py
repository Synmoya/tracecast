"""Deterministic chunk packing and render-contract generation.

Packing is a thin, LLM-free program layer. It walks
``SelectionPlan.selected_sequence`` in order, groups accepted nodes into
chunks under the :class:`ChunkPolicy` budgets, and projects each chunk into a
``RenderContract`` -- the renderer's single source of truth. It never reorders
the sequence, never borrows materials across nodes, never re-introduces omitted
materials, and never invents facts, summaries or expression hints.

Two failure stances:

* **Structural inconsistency** (a selected node with no ``NodeEditResult``, a
  kept material missing from ``edited_materials``, mismatched upstream plan ids)
  raises :class:`PackingError`. The stage cannot produce a faithful artifact, so
  it fails locally instead of emitting a corrupted one.
* **Single-node overload** (one node alone exceeds a hard cap) is *reported*,
  never silently split: the node becomes its own chunk, flagged on the chunk and
  surfaced as an :class:`~tracecast.domain.AuditReportItem`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from tracecast.domain import (
    NODE_ALIASES,
    RENDER_ALIASES,
    AliasManifest,
    AuditReportItem,
    CandidateGraph,
    CandidateNode,
    ChunkPlan,
    ChunkPlanChunk,
    MaterialAtom,
    NodeEditOperation,
    NodeEditPlan,
    NodeEditResult,
    RenderContract,
    RenderContractMaterial,
    RenderContractNode,
    SelectionDecision,
    SelectionPlan,
    SelectionStatus,
    chunk_id,
    chunk_plan_id,
    contract_id,
    estimate_tokens,
    issue_id,
)

from .policy import DEFAULT_CHUNK_POLICY, ChunkPolicy

_STAGE = "packing"
_PACKING_PROTOCOL = "deterministic_chunk_packing@v2"
_OVERLOAD_KIND = "single_node_overload"
_MATERIAL_LOAD = "sum(estimate_tokens(material.content))"


class PackingError(Exception):
    """A structural inconsistency in Packing's inputs (fail-locally, no repair)."""


@dataclass(frozen=True)
class _VisibleMaterial:
    """One render-visible material (``keep`` original or ``condense`` result)."""

    atom: MaterialAtom
    operation: NodeEditOperation  # keep or condense only


@dataclass(frozen=True)
class _NodePayload:
    """A selected node resolved to its render-visible materials and load."""

    candidate_id: str
    decision: SelectionDecision
    materials: tuple[_VisibleMaterial, ...]
    material_chars: int
    material_tokens: int
    overload_caps: tuple[str, ...]  # hard caps this node alone exceeds


@dataclass
class _ChunkGroup:
    """A mutable accumulator for one chunk's nodes during packing."""

    order_index: int
    nodes: list[_NodePayload]
    material_chars: int
    material_tokens: int
    material_count: int

    @property
    def overload_caps(self) -> tuple[str, ...]:
        # An overloaded node is always isolated into its own single-node group.
        if len(self.nodes) == 1:
            return self.nodes[0].overload_caps
        return ()


@dataclass(frozen=True)
class PackingResult:
    """What :func:`pack` returns: the plan, its contracts, and stage reports.

    ``contracts`` is 1:1 with ``chunk_plan.chunks`` and in the same order.
    ``reports`` carries packing issues (v1: single-node overloads) for Validation
    to aggregate; it never gates the main chain.
    """

    chunk_plan: ChunkPlan
    contracts: list[RenderContract]
    reports: list[AuditReportItem]


# --- material / load helpers ----------------------------------------------


def _materials_by_id(materials: Iterable[MaterialAtom]) -> dict[str, MaterialAtom]:
    return {atom.id: atom for atom in materials}


def _visible_materials(
    result: NodeEditResult,
    materials_by_id: dict[str, MaterialAtom],
) -> list[_VisibleMaterial]:
    """Resolve a node's ``keep``/``condense`` edits to concrete materials.

    ``keep`` references an original atom by id (resolved from ``edited_materials``);
    ``condense`` carries its run-scoped result atom inline. ``omit`` never appears
    in ``edits`` (enforced by ``node_edit.MaterialEdit``), so it is dropped here.
    """
    visible: list[_VisibleMaterial] = []
    for edit in result.edits:
        if edit.operation is NodeEditOperation.keep:
            source_id = edit.source_material_ids[0]
            atom = materials_by_id.get(source_id)
            if atom is None:
                raise PackingError(
                    f"kept material {source_id!r} for node "
                    f"{result.candidate_id!r} is missing from edited_materials"
                )
            visible.append(_VisibleMaterial(atom, NodeEditOperation.keep))
        elif edit.operation is NodeEditOperation.condense:
            # MaterialEdit guarantees result_material is present with lineage.
            visible.append(_VisibleMaterial(edit.result_material, NodeEditOperation.condense))
    return visible


def _node_material_chars(materials: tuple[_VisibleMaterial, ...]) -> int:
    return sum(len(vm.atom.content) for vm in materials)


def _node_material_tokens(materials: tuple[_VisibleMaterial, ...]) -> int:
    return sum(estimate_tokens(vm.atom.content) for vm in materials)


def _overload_caps(
    material_tokens: int,
    material_count: int,
    policy: ChunkPolicy,
) -> tuple[str, ...]:
    caps: list[str] = []
    if material_tokens > policy.max_material_tokens:
        caps.append("max_material_tokens")
    if material_count > policy.max_materials_per_chunk:
        caps.append("max_materials_per_chunk")
    return tuple(caps)


def _build_payloads(
    selection_plan: SelectionPlan,
    node_edit_plan: NodeEditPlan,
    materials_by_id: dict[str, MaterialAtom],
    policy: ChunkPolicy,
) -> list[_NodePayload]:
    edits_by_candidate: dict[str, NodeEditResult] = {
        result.candidate_id: result for result in node_edit_plan.node_edits
    }

    payloads: list[_NodePayload] = []
    for cand in selection_plan.selected_sequence:
        result = edits_by_candidate.get(cand)
        if result is None:
            raise PackingError(f"selected node {cand!r} has no NodeEditResult")
        if result.selection.status is not SelectionStatus.accepted:
            raise PackingError(
                f"selected node {cand!r} is not 'accepted' in its NodeEditResult"
            )
        materials = tuple(_visible_materials(result, materials_by_id))
        material_chars = _node_material_chars(materials)
        material_tokens = _node_material_tokens(materials)
        payloads.append(
            _NodePayload(
                candidate_id=cand,
                decision=result.selection,
                materials=materials,
                material_chars=material_chars,
                material_tokens=material_tokens,
                overload_caps=_overload_caps(material_tokens, len(materials), policy),
            )
        )
    return payloads


# --- chunk boundary packing -----------------------------------------------


def _would_exceed(group: _ChunkGroup, node: _NodePayload, policy: ChunkPolicy) -> bool:
    if group.material_tokens + node.material_tokens > policy.max_material_tokens:
        return True
    if group.material_count + len(node.materials) > policy.max_materials_per_chunk:
        return True
    if (
        policy.max_nodes_per_chunk is not None
        and len(group.nodes) + 1 > policy.max_nodes_per_chunk
    ):
        return True
    return False


def _pack_groups(payloads: list[_NodePayload], policy: ChunkPolicy) -> list[_ChunkGroup]:
    """Greedy node-boundary packing, in ``selected_sequence`` order.

    Add nodes to the current chunk until adding the next would breach a hard cap
    (then close and start fresh) or until the soft ``target_material_tokens`` is
    reached (then close). A node that alone exceeds a hard cap is isolated into
    its own chunk -- never split.
    """
    groups: list[_ChunkGroup] = []
    current: _ChunkGroup | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            groups.append(current)
            current = None

    for node in payloads:
        if node.overload_caps:
            flush()
            current = _ChunkGroup(
                order_index=len(groups),
                nodes=[node],
                material_chars=node.material_chars,
                material_tokens=node.material_tokens,
                material_count=len(node.materials),
            )
            flush()
            continue

        if current is not None and _would_exceed(current, node, policy):
            flush()
        if current is None:
            current = _ChunkGroup(
                order_index=len(groups),
                nodes=[],
                material_chars=0,
                material_tokens=0,
                material_count=0,
            )
        current.nodes.append(node)
        current.material_chars += node.material_chars
        current.material_tokens += node.material_tokens
        current.material_count += len(node.materials)

        if current.material_tokens >= policy.target_material_tokens:
            flush()

    flush()
    return groups


# --- artifact construction ------------------------------------------------


def _build_chunk(group: _ChunkGroup, project_id: str) -> ChunkPlanChunk:
    node_ids = [node.candidate_id for node in group.nodes]
    material_ids = [vm.atom.id for node in group.nodes for vm in node.materials]
    metadata: dict[str, object] = {}
    if group.overload_caps:
        metadata = {"overloaded_caps": list(group.overload_caps)}
    return ChunkPlanChunk(
        chunk_id=chunk_id(project_id, group.order_index, node_ids),
        order_index=group.order_index,
        node_ids=node_ids,
        material_ids=material_ids,
        estimated_material_tokens=group.material_tokens,
        metadata=metadata,
    )


def _build_contract(
    group: _ChunkGroup,
    chunk: ChunkPlanChunk,
    project_id: str,
    persona_id: str,
    candidate_nodes: dict[str, CandidateNode],
) -> RenderContract:
    node_aliases = AliasManifest(NODE_ALIASES)
    material_aliases = AliasManifest(RENDER_ALIASES)
    contract_nodes: list[RenderContractNode] = []
    contract_materials: list[RenderContractMaterial] = []

    for order_in_chunk, node in enumerate(group.nodes):
        candidate = candidate_nodes.get(node.candidate_id)
        node_ref = node_aliases.mint(node.candidate_id)
        material_refs: list[str] = []
        for vm in node.materials:
            material_ref = material_aliases.mint(vm.atom.id)
            material_refs.append(material_ref)
            lineage = (
                list(vm.atom.source_material_ids)
                if vm.operation is NodeEditOperation.condense
                else []
            )
            contract_materials.append(
                RenderContractMaterial(
                    material_ref=material_ref,
                    source_material_id=vm.atom.id,
                    node_ref=node_ref,
                    operation=vm.operation,
                    content=vm.atom.content,
                    language=vm.atom.language,
                    content_kind=vm.atom.content_kind,
                    tags=list(vm.atom.tags),
                    source_position=vm.atom.source_order,
                    source_material_refs=lineage,
                )
            )
        contract_nodes.append(
            RenderContractNode(
                node_ref=node_ref,
                source_node_id=node.candidate_id,
                order_in_chunk=order_in_chunk,
                role=node.decision.role,
                treatment=node.decision.treatment,
                directive=node.decision.directive,
                summary=candidate.summary if candidate is not None else None,
                narrative_function=(
                    candidate.narrative_function if candidate is not None else None
                ),
                material_refs=material_refs,
            )
        )

    return RenderContract(
        contract_id=contract_id(project_id, chunk.chunk_id),
        chunk_id=chunk.chunk_id,
        order_index=group.order_index,
        project_id=project_id,
        persona_id=persona_id,
        nodes=contract_nodes,
        materials=contract_materials,
        forbidden_claims=[],  # Packing introduces no new claims/constraints
        metadata={},
    )


def _overload_issue(
    group: _ChunkGroup,
    chunk: ChunkPlanChunk,
    policy: ChunkPolicy,
) -> AuditReportItem:
    node = group.nodes[0]
    caps = list(group.overload_caps)
    measured = (
        f"material_tokens={node.material_tokens}/{policy.max_material_tokens}, "
        f"materials={len(node.materials)}/{policy.max_materials_per_chunk}"
    )
    message = (
        f"node {node.candidate_id} alone exceeds {', '.join(caps)} and was placed "
        f"in its own chunk without splitting ({measured})"
    )
    return AuditReportItem(
        issue_id=issue_id(_STAGE, _OVERLOAD_KIND, chunk.chunk_id, message),
        stage=_STAGE,
        kind=_OVERLOAD_KIND,
        level="error",
        message=message,
        usable=True,
        artifact_ref=chunk.chunk_id,
        trace_refs=[node.candidate_id],
        metadata={"overload_caps": caps},
    )


# --- public entry point ----------------------------------------------------


def pack(
    selection_plan: SelectionPlan,
    node_edit_plan: NodeEditPlan,
    edited_materials: Iterable[MaterialAtom],
    policy: ChunkPolicy = DEFAULT_CHUNK_POLICY,
    *,
    candidate_graph: CandidateGraph | None = None,
) -> PackingResult:
    """Pack a node-edit plan into a ``ChunkPlan`` plus its ``RenderContract`` list.

    ``edited_materials`` supplies the original atoms referenced by ``keep`` edits
    (``condense`` results travel inline on the edit). ``policy`` is injected by
    the driver -- Packing reads no config or files itself.

    Returns a :class:`PackingResult` (plan, per-chunk contracts in order, and a
    stage-local reports). Raises :class:`PackingError` on structural input
    inconsistencies; single-node overloads are reported, not raised.
    """
    if not policy.prefer_node_boundary:
        raise PackingError(
            "prefer_node_boundary=False is not supported in v1: Packing only "
            "splits at node boundaries (there is no intra-node split)"
        )
    if selection_plan.project_id != node_edit_plan.project_id:
        raise PackingError("selection_plan.project_id and node_edit_plan.project_id differ")
    if node_edit_plan.selection_plan_id != selection_plan.plan_id:
        raise PackingError(
            "node_edit_plan.selection_plan_id does not match selection_plan.plan_id"
        )
    if candidate_graph is not None:
        if candidate_graph.graph_id != node_edit_plan.candidate_graph_id:
            raise PackingError(
                "candidate_graph.graph_id does not match node_edit_plan.candidate_graph_id"
            )
        missing_nodes = sorted(
            set(selection_plan.selected_sequence)
            - {node.candidate_id for node in candidate_graph.nodes}
        )
        if missing_nodes:
            raise PackingError(
                "selected nodes missing from candidate_graph: " + ", ".join(missing_nodes)
            )

    project_id = selection_plan.project_id
    persona_id = selection_plan.persona_id

    materials_by_id = _materials_by_id(edited_materials)
    candidate_nodes = (
        {node.candidate_id: node for node in candidate_graph.nodes}
        if candidate_graph is not None
        else {}
    )
    payloads = _build_payloads(selection_plan, node_edit_plan, materials_by_id, policy)
    groups = _pack_groups(payloads, policy)

    chunks: list[ChunkPlanChunk] = []
    contracts: list[RenderContract] = []
    reports: list[AuditReportItem] = []
    for group in groups:
        chunk = _build_chunk(group, project_id)
        chunks.append(chunk)
        contracts.append(
            _build_contract(group, chunk, project_id, persona_id, candidate_nodes)
        )
        if group.overload_caps:
            reports.append(_overload_issue(group, chunk, policy))

    plan = ChunkPlan(
        plan_id=chunk_plan_id(project_id, selection_plan.plan_id, node_edit_plan.plan_id),
        project_id=project_id,
        selection_plan_id=selection_plan.plan_id,
        node_edit_plan_id=node_edit_plan.plan_id,
        policy=policy.to_policy_dict(),
        chunks=chunks,
        metadata={
            "packing_protocol": _PACKING_PROTOCOL,
            "material_load": _MATERIAL_LOAD,
            "estimated_material_tokens_semantics": "pure material content tokens",
        },
    )
    return PackingResult(chunk_plan=plan, contracts=contracts, reports=reports)
