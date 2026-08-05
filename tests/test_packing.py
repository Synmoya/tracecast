"""Independent spec-conformance tests for ``tracecast.packing``.

Assertions cover the public Packing contract: deterministic order, pure-material
budgets, faithful ``ChunkPlan``/``RenderContract`` projection, local structural
failure, and report-only single-node overloads.

Scenarios are constructed from ``tracecast.domain`` records, honouring their
validators (an ``accepted`` decision needs role+treatment; a ``condense`` edit
needs a lineage-bearing result material; ``omit`` lives in ``omitted``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import pytest

from tracecast.domain import (
    CandidateGraph,
    CandidateNode,
    ContentKind,
    MaterialAtom,
    MaterialEdit,
    MaterialOmission,
    NodeEditOperation,
    NodeEditPlan,
    NodeEditResult,
    Provenance,
    Role,
    SelectionDecision,
    SelectionPlan,
    SelectionStatus,
    Treatment,
    TrustTier,
)
from tracecast.packing import (
    DEFAULT_CHUNK_POLICY,
    ChunkPolicy,
    PackingError,
    chunk_policy_from_budgets,
    pack,
)
from tracecast.storage import RuntimeConfig

PROJECT = "fixture-project"
PERSONA = "persona_alex"
GRAPH_ID = "graph_test"
SEL_PLAN_ID = "selplan_test"
NE_PLAN_ID = "nedit_test"


# --- builders --------------------------------------------------------------


def _prov() -> Provenance:
    return Provenance(
        source_ref="src:000000",
        source_type="script_transcript",
        source_class="script_transcript",
        source_content_hash="h0",
    )


def _atom(atom_id: str, content: str, lineage: Sequence[str] = ()) -> MaterialAtom:
    return MaterialAtom(
        id=atom_id,
        content=content,
        language="zh",
        content_kind=ContentKind.assertion,
        source_order=0,
        trust_tier=TrustTier.canonical,
        provenance=_prov(),
        source_material_ids=list(lineage),
    )


def _accepted(cand: str, directive: str | None = None) -> SelectionDecision:
    return SelectionDecision(
        candidate_id=cand,
        status=SelectionStatus.accepted,
        role=Role.core,
        treatment=Treatment.quote,
        directive=directive or "按当前节点功能推进叙事。",
    )


def _keep_edit(atom_id: str) -> MaterialEdit:
    return MaterialEdit(operation=NodeEditOperation.keep, source_material_ids=[atom_id])


def _condense_edit(result: MaterialAtom) -> MaterialEdit:
    # The lineage on the result atom is what makes a condense edit valid and what
    # Packing must mirror into the contract's ``source_material_refs``.
    return MaterialEdit(
        operation=NodeEditOperation.condense,
        source_material_ids=list(result.source_material_ids),
        result_material=result,
    )


def _omission(material_id: str) -> MaterialOmission:
    return MaterialOmission(material_id=material_id, reason="dropped for brevity")


def _node_result(
    cand: str,
    edits: Iterable[MaterialEdit],
    omitted: Iterable[MaterialOmission] = (),
    directive: str | None = None,
) -> NodeEditResult:
    return NodeEditResult(
        candidate_id=cand,
        selection=_accepted(cand, directive),
        edits=list(edits),
        omitted=list(omitted),
    )


def _make_sel(
    seq: Sequence[str],
    *,
    plan_id: str = SEL_PLAN_ID,
    project: str = PROJECT,
    persona: str = PERSONA,
    decisions: list[SelectionDecision] | None = None,
) -> SelectionPlan:
    return SelectionPlan(
        plan_id=plan_id,
        project_id=project,
        persona_id=persona,
        selected_sequence=list(seq),
        decisions=decisions if decisions is not None else [_accepted(c) for c in seq],
    )


def _make_ne(
    results: Iterable[NodeEditResult],
    *,
    plan_id: str = NE_PLAN_ID,
    project: str = PROJECT,
    graph: str = GRAPH_ID,
    sel_id: str = SEL_PLAN_ID,
) -> NodeEditPlan:
    return NodeEditPlan(
        plan_id=plan_id,
        project_id=project,
        candidate_graph_id=graph,
        selection_plan_id=sel_id,
        node_edits=list(results),
    )


def _plans(results: list[NodeEditResult]) -> tuple[SelectionPlan, NodeEditPlan]:
    seq = [r.candidate_id for r in results]
    sel = _make_sel(seq, decisions=[r.selection for r in results])
    ne = _make_ne(results)
    return sel, ne


def _single_keep_scenario(
    specs: Sequence[tuple[str, str]],
) -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom]]:
    """One accepted node per spec, each with a single ``keep`` atom of given content."""
    results: list[NodeEditResult] = []
    edited: list[MaterialAtom] = []
    for cand, content in specs:
        atom_id = f"atom_{cand}"
        edited.append(_atom(atom_id, content))
        results.append(_node_result(cand, [_keep_edit(atom_id)]))
    sel, ne = _plans(results)
    return sel, ne, edited


def _multi_chunk_scenario() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom], ChunkPolicy]:
    """Four one-material nodes under a pure-material token budget forcing two chunks.

    ``estimate_tokens('x' * 50)`` is 13, so the 27-token hard cap fits at most
    two nodes and the 25-token target closes after the second.
    """
    specs = [(f"cand_{i}", "x" * 50) for i in range(4)]
    sel, ne, edited = _single_keep_scenario(specs)
    policy = ChunkPolicy(target_material_tokens=25, max_material_tokens=27)
    return sel, ne, edited, policy


def _rich_scenario() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom], dict]:
    """3 nodes exercising multi-keep, condense (+lineage) and omit in one chunk."""
    k1a = _atom("atom_1a", "第一段证据")
    k1b = _atom("atom_1b", "第二段证据")
    k2 = _atom("atom_2k", "保留材料")
    cond = _atom("atom_2c", "压缩后的材料", lineage=["atom_src_x", "atom_src_y"])
    k3 = _atom("atom_3k", "最后一段")
    edited = [k1a, k1b, k2, cond, k3]
    n1 = _node_result("cand_1", [_keep_edit("atom_1a"), _keep_edit("atom_1b")], directive="d1")
    n2 = _node_result(
        "cand_2",
        [_keep_edit("atom_2k"), _condense_edit(cond)],
        omitted=[_omission("atom_2omit")],
        directive="d2",
    )
    n3 = _node_result("cand_3", [_keep_edit("atom_3k")])
    sel, ne = _plans([n1, n2, n3])
    facts = {
        "omit": "atom_2omit",
        "condense_id": "atom_2c",
        "condense_lineage": {"atom_src_x", "atom_src_y"},
        "keep_ids": {"atom_1a", "atom_1b", "atom_2k", "atom_3k"},
        "known_ids": {"atom_1a", "atom_1b", "atom_2k", "atom_2c", "atom_3k"},
    }
    return sel, ne, edited, facts


def _flatten_nodes(chunks) -> list[str]:
    return [nid for chunk in chunks for nid in chunk.node_ids]


# === 1. Determinism + order preservation ===================================


def test_pack_is_deterministic() -> None:
    sel, ne, edited, _ = _rich_scenario()
    a = pack(sel, ne, edited)
    b = pack(sel, ne, edited)
    # Pydantic value equality covers chunk ids, contract ids, refs, units, etc.
    assert a.chunk_plan == b.chunk_plan
    assert a.contracts == b.contracts
    assert a.reports == b.reports
    # A well-formed pack with no overload has nothing to report.
    assert a.reports == []


def test_chunk_and_contract_ids_are_unique_within_a_plan() -> None:
    sel, ne, edited, policy = _multi_chunk_scenario()
    result = pack(sel, ne, edited, policy)
    chunk_ids = [c.chunk_id for c in result.chunk_plan.chunks]
    contract_ids = [c.contract_id for c in result.contracts]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert len(contract_ids) == len(set(contract_ids))
    assert result.chunk_plan.plan_id  # non-empty stable id


def test_chunks_preserve_selected_sequence_order() -> None:
    # Packing preserves selected_sequence order, and every accepted node enters
    # exactly one chunk.
    sel, ne, edited, policy = _multi_chunk_scenario()
    result = pack(sel, ne, edited, policy)
    chunks = result.chunk_plan.chunks
    assert len(chunks) >= 2  # the cap forces a split across node boundaries
    assert _flatten_nodes(chunks) == sel.selected_sequence
    assert [c.order_index for c in chunks] == list(range(len(chunks)))


def test_pipeline_wires_max_nodes_per_chunk_into_packing() -> None:
    sel, ne, edited = _single_keep_scenario(
        [("cand_1", "短材料一"), ("cand_2", "短材料二"), ("cand_3", "短材料三")]
    )
    config = RuntimeConfig.model_validate(
        {
            "stage_budgets": {
                "rendering": {
                    "target_material_tokens": 10000,
                    "max_material_tokens": 12000,
                    "max_nodes_per_chunk": 1,
                    "prefer_node_boundary": True,
                }
            }
        }
    )
    policy = chunk_policy_from_budgets(config.stage_budgets["rendering"])
    assert policy.max_nodes_per_chunk == 1
    result = pack(sel, ne, edited, policy)
    assert [len(chunk.node_ids) for chunk in result.chunk_plan.chunks] == [1, 1, 1]


# === 2. Budget enforcement =================================================


def test_no_chunk_exceeds_hard_caps_when_no_single_node_overloads() -> None:
    # Packing's primary hard cap measures only render-visible material content.
    sel, ne, edited, policy = _multi_chunk_scenario()
    result = pack(sel, ne, edited, policy)
    assert result.reports == []  # no node overloads in this scenario
    for chunk in result.chunk_plan.chunks:
        assert len(chunk.material_ids) <= policy.max_materials_per_chunk
        assert chunk.estimated_material_tokens <= policy.max_material_tokens
        assert chunk.node_ids  # ChunkPlanChunk requires >=1 node


def test_policy_is_persisted_with_the_chunk_plan() -> None:
    # The effective packing policy is persisted with the artifact.
    policy = ChunkPolicy(target_material_tokens=300, max_material_tokens=400)
    sel, ne, edited = _single_keep_scenario([("cand_0", "x" * 20)])
    result = pack(sel, ne, edited, policy)
    assert result.chunk_plan.policy == policy.to_policy_dict()


# === 3. NO SILENT SPLIT: single-node overload is isolated AND reported ======


def test_single_node_overload_is_isolated_not_split() -> None:
    # A node over max_material_tokens occupies its own chunk with all materials;
    # Packing reports the overload rather than silently splitting it.
    overload = "x" * 20000  # ~5000 tokens > default max_material_tokens (4500)
    specs = [("cand_a", "x" * 50), ("cand_big", overload), ("cand_c", "x" * 50)]
    sel, ne, edited = _single_keep_scenario(specs)
    result = pack(sel, ne, edited)  # default policy

    # order across the whole plan is still preserved
    assert _flatten_nodes(result.chunk_plan.chunks) == sel.selected_sequence

    big_chunks = [c for c in result.chunk_plan.chunks if "cand_big" in c.node_ids]
    assert len(big_chunks) == 1  # the node appears in exactly one chunk
    big = big_chunks[0]
    assert big.node_ids == ["cand_big"]  # isolated: own single-node chunk
    assert big.material_ids == ["atom_cand_big"]  # all its materials kept together
    assert big.estimated_material_tokens > DEFAULT_CHUNK_POLICY.max_material_tokens


def test_single_node_overload_emits_a_traceable_report_item() -> None:
    overload = "x" * 20000
    specs = [("cand_a", "x" * 50), ("cand_big", overload), ("cand_c", "x" * 50)]
    sel, ne, edited = _single_keep_scenario(specs)
    result = pack(sel, ne, edited)

    # Exactly one overloaded node -> exactly one report item (no item for the
    # well-behaved neighbours).
    assert len(result.reports) == 1
    item = result.reports[0]
    assert item.stage == "packing"
    # Report-only / not a gate: the chunk is still emitted, so it stays usable.
    assert item.usable is True
    # The issue must be traceable back to the offending node and its chunk.
    big = next(c for c in result.chunk_plan.chunks if c.node_ids == ["cand_big"])
    assert "cand_big" in item.trace_refs
    assert item.artifact_ref == big.chunk_id


# === 4. RenderContract conformance =========================================


def test_contracts_are_one_to_one_with_chunks_and_mirror_them() -> None:
    # RenderContract generation is one-to-one with ordered chunks: its nodes equal
    # the chunk's nodes in the same order, and its materials equal
    # the chunk's render-visible materials (same order).
    sel, ne, edited, policy = _multi_chunk_scenario()
    result = pack(sel, ne, edited, policy)
    chunks = result.chunk_plan.chunks
    contracts = result.contracts
    assert len(contracts) == len(chunks)
    for chunk, contract in zip(chunks, contracts, strict=True):
        assert contract.chunk_id == chunk.chunk_id
        assert contract.order_index == chunk.order_index
        assert [n.source_node_id for n in contract.nodes] == chunk.node_ids
        assert [m.source_material_id for m in contract.materials] == chunk.material_ids
        assert [n.order_in_chunk for n in contract.nodes] == list(range(len(contract.nodes)))


def test_candidate_summaries_are_copied_into_contracts_without_becoming_materials() -> None:
    sel, ne, edited = _single_keep_scenario([("cand_0", "正文材料")])
    graph = CandidateGraph(
        graph_id=GRAPH_ID,
        project_id=PROJECT,
        nodes=[
            CandidateNode(
                candidate_id="cand_0",
                source_window="window_0",
                summary="节点 SUM",
                narrative_function="setup",
                material_ids=["atom_cand_0"],
            )
        ],
    )
    result = pack(sel, ne, edited, candidate_graph=graph)
    node = result.contracts[0].nodes[0]
    assert node.summary == "节点 SUM"
    assert node.narrative_function == "setup"
    assert [material.content for material in result.contracts[0].materials] == ["正文材料"]


def test_contract_excludes_omit_keeps_keep_and_carries_condense_lineage() -> None:
    sel, ne, edited, facts = _rich_scenario()
    result = pack(sel, ne, edited)
    assert len(result.chunk_plan.chunks) == 1  # tiny content -> single chunk
    chunk = result.chunk_plan.chunks[0]
    contract = result.contracts[0]

    visible_ids = [m.source_material_id for m in contract.materials]
    # Omitted material is absent from both the chunk and the contract.
    assert facts["omit"] not in chunk.material_ids
    assert facts["omit"] not in visible_ids

    # every kept material is present and labelled keep.
    keep_ids = {m.source_material_id for m in contract.materials
                if m.operation is NodeEditOperation.keep}
    assert facts["keep_ids"] <= keep_ids

    # Condensed material carries non-empty lineage to its source material ids.
    condense_mats = [m for m in contract.materials
                     if m.operation is NodeEditOperation.condense]
    assert len(condense_mats) == 1
    cm = condense_mats[0]
    assert cm.source_material_id == facts["condense_id"]
    assert cm.source_material_refs  # non-empty
    assert set(cm.source_material_refs) == facts["condense_lineage"]


# === 5. Fail locally: structural inconsistency -> PackingError (no artifact) ==


def _bad_missing_node_result() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom]]:
    res = _node_result("cand_a", [_keep_edit("atom_a")])
    sel = _make_sel(["cand_a", "cand_ghost"])  # cand_ghost has no NodeEditResult
    ne = _make_ne([res])
    return sel, ne, [_atom("atom_a", "xx")]


def _bad_missing_keep_material() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom]]:
    res = _node_result("cand_a", [_keep_edit("atom_a")])
    sel, ne = _plans([res])
    return sel, ne, []  # kept atom_a not supplied in edited_materials


def _bad_selplan_id_mismatch() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom]]:
    res = _node_result("cand_a", [_keep_edit("atom_a")])
    sel = _make_sel(["cand_a"])
    ne = _make_ne([res], sel_id="WRONG")  # does not match sel.plan_id
    return sel, ne, [_atom("atom_a", "xx")]


def _bad_project_id_mismatch() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom]]:
    res = _node_result("cand_a", [_keep_edit("atom_a")])
    sel = _make_sel(["cand_a"])
    ne = _make_ne([res], project="OTHER_PROJECT")
    return sel, ne, [_atom("atom_a", "xx")]


def _bad_node_not_accepted() -> tuple[SelectionPlan, NodeEditPlan, list[MaterialAtom]]:
    res = NodeEditResult(
        candidate_id="cand_a",
        selection=SelectionDecision(candidate_id="cand_a", status=SelectionStatus.not_selected),
        edits=[],
    )
    sel = _make_sel(["cand_a"])
    ne = _make_ne([res])
    return sel, ne, []


@pytest.mark.parametrize(
    "factory",
    [
        _bad_missing_node_result,
        _bad_missing_keep_material,
        _bad_selplan_id_mismatch,
        _bad_project_id_mismatch,
        _bad_node_not_accepted,
    ],
    ids=[
        "missing_node_result",
        "missing_keep_material",
        "selplan_id_mismatch",
        "project_id_mismatch",
        "node_not_accepted",
    ],
)
def test_structural_inconsistency_raises_packing_error(factory) -> None:
    # A structural inconsistency fails locally rather than producing a corrupt artifact.
    sel, ne, edited = factory()
    with pytest.raises(PackingError):
        pack(sel, ne, edited)


# === 6. Traceability: every contract material resolves to a real material ===


def test_every_contract_material_traces_to_a_real_material() -> None:
    # Packing invents no material. Every projected material must resolve to a
    # supplied keep/condense material and must
    # attach to a node that exists in the same contract.
    sel, ne, edited, facts = _rich_scenario()
    result = pack(sel, ne, edited)
    for contract in result.contracts:
        node_refs = {n.node_ref for n in contract.nodes}
        owned_refs: list[str] = []
        for node in contract.nodes:
            owned_refs.extend(node.material_refs)
        material_refs = {m.material_ref for m in contract.materials}
        # every node-owned ref is a real material, and vice versa (exact partition)
        assert set(owned_refs) == material_refs
        assert len(owned_refs) == len(material_refs)  # each material owned once
        for material in contract.materials:
            assert material.source_material_id in facts["known_ids"]
            assert material.node_ref in node_refs
