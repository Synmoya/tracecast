"""Node Edit stage orchestration for selected-node material editing.

The public façade preserves selected order while moving one deterministic input
preparation phase through a primary batch wave, one bounded single-node repair
wave, and final artifact assembly.  Mechanical K/C/O compilation lives in
``compiler.py``; provider dispatch and prompt projection retain their existing
module boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    AuditReportItem,
    CandidateGraph,
    LlmCallRecord,
    MaterialAtom,
    NodeEditPlan,
    NodeEditResult,
    SelectionPlan,
    SelectionStatus,
    node_edit_plan_id,
)
from tracecast.llm import call_llm
from tracecast.prompts import (
    load_system_prompt,
    load_user_prompt,
    substitute_prompt_blocks,
)
from tracecast.storage import RunStore, RuntimeConfig

from .batching import batch_nodes
from .compiler import (
    CompiledNode,
    NodeEditFailure,
    compile_all_keep,
    compile_node_block,
    failure,
    select_node_block,
    with_producer_metadata,
)
from .dispatch import (
    NodeEditBatchCall,
    NodeEditBatchResult,
    NodeEditRepairCall,
    NodeEditRepairResult,
    run_node_edit_batch_calls,
    run_node_edit_repair_calls,
)
from .dsl import RawNodeBlock, parse_node_edit_dsl
from .projection import (
    BatchNode,
    node_edit_field_notes_version,
    project_batch,
    render_node_edit_user_prompt,
)
from .results import NodeEditBuildOutput
from .vocab import (
    BUDGET_BATCH_MAX_INPUT_TOKENS,
    BUDGET_MAX_NODES_PER_BATCH,
    NODE_EDIT_REPAIR_ROUTE,
    NODE_EDIT_ROUTE,
    STAGE_NODE_EDIT,
    default_batch_max_input_tokens,
    default_max_nodes_per_batch,
    report_item,
)

#: Patchable provider-call seam (signature-compatible with ``llm.call_llm``).
LlmCall = Callable[..., tuple[str, LlmCallRecord]]


_REPAIR_GUIDANCE = {
    "missing_node_block": "为当前单节点输出一个完整 NODE 块。",
    "duplicate_node_block": "只输出一个完整 NODE 块，不拆分或重复当前节点。",
    "malformed_node_block": "按本次输出协议从头重建完整 NODE 块。",
    "unknown_material_alias": "只使用本次单节点投影中列出的材料 alias，从头重建完整分区。",
    "cross_node_material_alias": "只处理本次单节点投影中的材料，不引用其他节点或旧调用的 alias。",
    "duplicate_material_assignment": "重新分区，确保每条材料只进入 K、C、O 之一一次。",
    "missing_omit_reason": "为每条 O 操作提供简短审计理由。",
    "empty_condense_content": "每条 C 操作后紧跟非空 TXT；否则改用 K 或 O。",
    "mixed_source_condense": "只压缩同一来源内的材料；不满足时拆开处理。",
    "noncontiguous_condense": "C 只覆盖来源顺序连续的材料；不满足时拆开处理。",
    "incomplete_partition": "重新检查全部材料，确保每条恰好进入 K、C、O 之一。",
    "repair_transport": "本次调用未获得可解析输出。",
}
_REPAIR_GUIDANCE_EN = {
    "missing_node_block": "Output one complete NODE block for this single node.",
    "duplicate_node_block": (
        "Output exactly one complete NODE block; do not split or repeat the node."
    ),
    "malformed_node_block": "Rebuild the complete NODE block from the current output protocol.",
    "unknown_material_alias": (
        "Use only material aliases in this single-node projection and rebuild the full partition."
    ),
    "cross_node_material_alias": (
        "Process only this projection's materials; do not reference another node "
        "or an earlier call's aliases."
    ),
    "duplicate_material_assignment": (
        "Repartition so every material enters exactly one of K, C, or O once."
    ),
    "missing_omit_reason": "Give every O operation a short audit reason.",
    "empty_condense_content": (
        "Follow every C operation with a non-empty TXT; otherwise use K or O."
    ),
    "mixed_source_condense": (
        "Condense only materials from one source; split the operation otherwise."
    ),
    "noncontiguous_condense": (
        "C may cover only source-contiguous materials; split the operation otherwise."
    ),
    "incomplete_partition": ("Check all materials and put each into exactly one of K, C, or O."),
    "repair_transport": "The call returned no parseable output.",
}


@dataclass(frozen=True)
class _PromptBundle:
    """Loaded prompts and their exact audited protocol strings."""

    system_prompt: str
    user_prompt_template: str
    protocol: str
    repair_system_prompt: str
    repair_user_template: str
    repair_protocol: str


@dataclass(frozen=True)
class _Preparation:
    """Deterministic selected-node preparation before provider work."""

    accepted: tuple[BatchNode, ...]
    compiled: tuple[CompiledNode, ...]
    reports: tuple[AuditReportItem, ...]


@dataclass(frozen=True)
class _BatchWave:
    """Prepared primary calls plus budget diagnostics."""

    calls: tuple[NodeEditBatchCall, ...]
    batch_count: int
    reports: tuple[AuditReportItem, ...]


@dataclass(frozen=True)
class _PrimaryCompilation:
    """Primary successes and bounded single-node repair requests."""

    compiled: tuple[CompiledNode, ...]
    repairs: tuple[NodeEditRepairCall, ...]
    reports: tuple[AuditReportItem, ...]


@dataclass(frozen=True)
class _ParsedPrimary:
    """Parsed primary blocks and diagnostics, preserving first-alias order."""

    block_groups: tuple[tuple[str, tuple[RawNodeBlock, ...]], ...]
    reports: tuple[AuditReportItem, ...]

    def blocks_for(self, alias: str) -> tuple[RawNodeBlock, ...]:
        """Return all emitted blocks for one call-local candidate alias."""
        for candidate_alias, blocks in self.block_groups:
            if candidate_alias == alias:
                return blocks
        return ()


@dataclass(frozen=True)
class _RepairCompilation:
    """Outcomes and reports from the one repair wave."""

    compiled: tuple[CompiledNode, ...]
    reports: tuple[AuditReportItem, ...]


def build_node_edit_outputs(
    selection_plan: SelectionPlan,
    graph: CandidateGraph,
    atoms: Sequence[MaterialAtom],
    *,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall = call_llm,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> NodeEditBuildOutput:
    """Build the plan, edited materials, and reports for the Node Edit stage."""
    locale = coerce_control_prompt_locale(control_prompt_locale)
    prompts = _load_prompt_bundle(config, locale)
    budgets = _node_edit_budgets(config)
    max_tokens = int(
        budgets.get(
            BUDGET_BATCH_MAX_INPUT_TOKENS,
            default_batch_max_input_tokens(),
        )
    )
    max_nodes = int(
        budgets.get(
            BUDGET_MAX_NODES_PER_BATCH,
            default_max_nodes_per_batch(),
        )
    )
    atoms_by_id = {atom.id: atom for atom in atoms}

    preparation = _prepare_inputs(
        selection_plan,
        graph,
        atoms_by_id,
        protocol=prompts.protocol,
    )
    batch_wave = _prepare_batch_wave(
        preparation.accepted,
        max_tokens=max_tokens,
        max_nodes=max_nodes,
        user_prompt_template=prompts.user_prompt_template,
        control_prompt_locale=locale,
    )
    batch_results = run_node_edit_batch_calls(
        batch_wave.calls,
        system_prompt=prompts.system_prompt,
        prompt_version=prompts.protocol,
        config=config,
        store=store,
        call=call,
    )
    primary = _compile_primary_wave(
        batch_results,
        atoms_by_id,
        protocol=prompts.protocol,
        user_prompt_template=prompts.user_prompt_template,
        repair_user_template=prompts.repair_user_template,
        control_prompt_locale=locale,
    )
    repair_results = run_node_edit_repair_calls(
        primary.repairs,
        system_prompt=prompts.repair_system_prompt,
        prompt_version=prompts.repair_protocol,
        config=config,
        store=store,
        call=call,
    )
    repairs = _compile_repair_wave(
        repair_results,
        atoms_by_id,
        protocol=prompts.protocol,
    )

    return _assemble_output(
        selection_plan,
        graph,
        compiled=(*preparation.compiled, *primary.compiled, *repairs.compiled),
        reports=(
            *preparation.reports,
            *batch_wave.reports,
            *primary.reports,
            *repairs.reports,
        ),
        protocol=prompts.protocol,
        batch_count=batch_wave.batch_count,
        max_tokens=max_tokens,
        max_nodes=max_nodes,
    )


def _load_prompt_bundle(
    config: RuntimeConfig,
    control_prompt_locale: ControlPromptLocale,
) -> _PromptBundle:
    system_prompt, system_version = load_system_prompt(
        NODE_EDIT_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    user_prompt_template, user_version = load_user_prompt(
        NODE_EDIT_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    field_notes_version = node_edit_field_notes_version(control_prompt_locale)
    protocol = f"{system_version}+{user_version}+{field_notes_version}"
    repair_system_prompt, repair_system_version = load_system_prompt(
        NODE_EDIT_REPAIR_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    repair_user_template, repair_user_version = load_user_prompt(
        NODE_EDIT_REPAIR_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    repair_protocol = (
        f"{repair_system_version}+{repair_user_version}+{user_version}+{field_notes_version}"
    )
    return _PromptBundle(
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        protocol=protocol,
        repair_system_prompt=repair_system_prompt,
        repair_user_template=repair_user_template,
        repair_protocol=repair_protocol,
    )


def _node_edit_budgets(config: RuntimeConfig) -> Mapping[str, Any]:
    return config.stage_budgets.get(STAGE_NODE_EDIT, {})


def _prepare_inputs(
    selection_plan: SelectionPlan,
    graph: CandidateGraph,
    atoms_by_id: Mapping[str, MaterialAtom],
    *,
    protocol: str,
) -> _Preparation:
    reports: list[AuditReportItem] = []
    accepted: list[BatchNode] = []
    compiled: list[CompiledNode] = []
    nodes_by_id = {node.candidate_id: node for node in graph.nodes}
    decisions_by_id = {decision.candidate_id: decision for decision in selection_plan.decisions}

    if graph.project_id != selection_plan.project_id:
        reports.append(
            report_item(
                "project_id_mismatch",
                f"graph project_id {graph.project_id!r} != selection plan "
                f"{selection_plan.project_id!r}; selection plan wins",
                artifact_ref=graph.graph_id,
            )
        )

    for candidate_id_ in selection_plan.selected_sequence:
        decision = decisions_by_id.get(candidate_id_)
        if decision is None or decision.status is not SelectionStatus.accepted:
            reports.append(
                report_item(
                    "non_accepted_candidate",
                    "candidate in selected_sequence is not an accepted decision; skipped",
                    artifact_ref=candidate_id_,
                    trace_refs=[candidate_id_],
                )
            )
            continue
        node = nodes_by_id.get(candidate_id_)
        if node is None:
            reports.append(
                report_item(
                    "missing_candidate_node",
                    "accepted candidate has no node in the graph; skipped",
                    artifact_ref=candidate_id_,
                    trace_refs=[candidate_id_],
                )
            )
            continue
        pool, missing = _resolve_pool(node.material_ids, atoms_by_id)
        for material_id in missing:
            reports.append(
                report_item(
                    "missing_material",
                    "node references a material absent from the atom set; excluded",
                    artifact_ref=candidate_id_,
                    trace_refs=[candidate_id_, material_id],
                )
            )
        if not pool:
            reports.append(
                report_item(
                    "empty_node_pool",
                    "accepted node has no resolvable materials; emitted empty result",
                    artifact_ref=candidate_id_,
                    trace_refs=[candidate_id_],
                )
            )
            compiled.append(
                CompiledNode(
                    result=NodeEditResult(
                        candidate_id=candidate_id_,
                        selection=decision,
                        edits=[],
                        omitted=[],
                        metadata={
                            "node_edit_protocol": protocol,
                            "empty_pool": True,
                        },
                    ),
                    edited_materials=(),
                )
            )
            continue
        accepted.append(BatchNode(candidate_id_, decision, node, pool))

    return _Preparation(
        accepted=tuple(accepted),
        compiled=tuple(compiled),
        reports=tuple(reports),
    )


def _resolve_pool(
    node_material_ids: Sequence[str],
    atoms_by_id: Mapping[str, MaterialAtom],
) -> tuple[list[MaterialAtom], list[str]]:
    pool: list[MaterialAtom] = []
    missing: list[str] = []
    for material_id in node_material_ids:
        atom = atoms_by_id.get(material_id)
        if atom is None:
            missing.append(material_id)
        else:
            pool.append(atom)
    pool.sort(key=lambda atom: (atom.source_order, atom.id))
    return pool, missing


def _prepare_batch_wave(
    accepted: Sequence[BatchNode],
    *,
    max_tokens: int,
    max_nodes: int,
    user_prompt_template: str,
    control_prompt_locale: ControlPromptLocale,
) -> _BatchWave:
    batches, over_budget = batch_nodes(
        list(accepted),
        max_tokens,
        max_nodes,
        user_prompt_template=user_prompt_template,
        control_prompt_locale=control_prompt_locale,
    )
    reports = tuple(
        report_item(
            "node_over_budget",
            "single-node projection exceeds batch_max_input_tokens; not split",
            artifact_ref=candidate_id,
            trace_refs=[candidate_id],
        )
        for candidate_id in over_budget
    )
    calls: list[NodeEditBatchCall] = []
    for batch in batches:
        projection = project_batch(batch)
        user_prompt = render_node_edit_user_prompt(
            template=user_prompt_template,
            projection=projection,
            control_prompt_locale=control_prompt_locale,
        )
        calls.append(
            NodeEditBatchCall(
                batch=batch,
                projection=projection,
                user_prompt=user_prompt,
            )
        )
    return _BatchWave(calls=tuple(calls), batch_count=len(batches), reports=reports)


def _compile_primary_wave(
    batch_results: Sequence[NodeEditBatchResult],
    atoms_by_id: Mapping[str, MaterialAtom],
    *,
    protocol: str,
    user_prompt_template: str,
    repair_user_template: str,
    control_prompt_locale: ControlPromptLocale,
) -> _PrimaryCompilation:
    compiled: list[CompiledNode] = []
    repairs: list[NodeEditRepairCall] = []
    reports: list[AuditReportItem] = []

    for batch_result in batch_results:
        if batch_result.error is not None:
            batch_compilation = _compile_failed_batch(
                batch_result,
                protocol=protocol,
            )
        else:
            if batch_result.text is None or batch_result.record is None:
                raise RuntimeError("node_edit batch result is missing successful call payload")
            batch_compilation = _compile_successful_batch(
                batch_result,
                atoms_by_id,
                protocol=protocol,
                user_prompt_template=user_prompt_template,
                repair_user_template=repair_user_template,
                control_prompt_locale=control_prompt_locale,
            )
        compiled.extend(batch_compilation.compiled)
        repairs.extend(batch_compilation.repairs)
        reports.extend(batch_compilation.reports)

    return _PrimaryCompilation(
        compiled=tuple(compiled),
        repairs=tuple(repairs),
        reports=tuple(reports),
    )


def _compile_failed_batch(
    batch_result: NodeEditBatchResult,
    *,
    protocol: str,
) -> _PrimaryCompilation:
    compiled: list[CompiledNode] = []
    reports: list[AuditReportItem] = []
    record = batch_result.record
    call_id = record.call_id if record is not None else None
    raw_path = record.raw_response_path if record is not None else None
    unit_id = f"batch:{batch_result.batch[0].candidate_id}"
    for batch_node in batch_result.batch:
        reports.append(
            report_item(
                "batch_call_failed",
                "node_edit batch call failed at transport; node fell back to all-keep",
                artifact_ref=batch_node.candidate_id,
                trace_refs=[batch_node.candidate_id],
                call_id=call_id,
                raw_response_path=raw_path,
            )
        )
        recovered = compile_all_keep(
            batch_node,
            protocol=protocol,
            fallback_reason="batch LLM call failed",
        )
        compiled.append(
            with_producer_metadata(
                recovered,
                unit_id=unit_id,
                call_id=call_id,
            )
        )
    return _PrimaryCompilation(
        compiled=tuple(compiled),
        repairs=(),
        reports=tuple(reports),
    )


def _compile_successful_batch(
    batch_result: NodeEditBatchResult,
    atoms_by_id: Mapping[str, MaterialAtom],
    *,
    protocol: str,
    user_prompt_template: str,
    repair_user_template: str,
    control_prompt_locale: ControlPromptLocale,
) -> _PrimaryCompilation:
    if batch_result.text is None or batch_result.record is None:
        raise RuntimeError("node_edit batch result is missing successful call payload")
    compiled: list[CompiledNode] = []
    repairs: list[NodeEditRepairCall] = []
    batch = batch_result.batch
    projection = batch_result.projection
    record = batch_result.record
    parsed = _parse_primary_blocks(batch_result)

    unit_id = f"batch:{batch[0].candidate_id}"
    for batch_node in batch:
        alias = projection.candidate_manifest.alias_for(batch_node.candidate_id)
        block, node_failure = select_node_block(parsed.blocks_for(alias))
        compiled_node: CompiledNode | None = None
        if block is not None:
            outcome = compile_node_block(
                batch_node,
                block,
                projection.material_manifest,
                atoms_by_id,
                protocol=protocol,
            )
            if isinstance(outcome, NodeEditFailure):
                node_failure = outcome
            else:
                compiled_node = outcome
        if node_failure is not None:
            repairs.append(
                _prepare_repair_call(
                    batch_node,
                    node_failure,
                    record=record,
                    initial_block=block,
                    user_prompt_template=user_prompt_template,
                    repair_user_template=repair_user_template,
                    control_prompt_locale=control_prompt_locale,
                )
            )
            continue
        if compiled_node is None:
            raise RuntimeError("node_edit compiler produced neither result nor failure")
        compiled.append(
            with_producer_metadata(
                compiled_node,
                unit_id=unit_id,
                call_id=record.call_id,
            )
        )
    return _PrimaryCompilation(
        compiled=tuple(compiled),
        repairs=tuple(repairs),
        reports=parsed.reports,
    )


def _parse_primary_blocks(
    batch_result: NodeEditBatchResult,
) -> _ParsedPrimary:
    """Parse a primary response and report batch-level ignored content."""
    if batch_result.text is None or batch_result.record is None:
        raise RuntimeError("node_edit batch result is missing successful call payload")
    reports: list[AuditReportItem] = []
    parsed = parse_node_edit_dsl(batch_result.text)
    blocks_by_alias: dict[str, list[RawNodeBlock]] = {}
    for block in parsed.blocks:
        if block.candidate_alias is not None:
            blocks_by_alias.setdefault(block.candidate_alias, []).append(block)

    known_aliases = {
        batch_result.projection.candidate_manifest.alias_for(batch_node.candidate_id)
        for batch_node in batch_result.batch
    }
    for alias in blocks_by_alias:
        if alias not in known_aliases:
            reports.append(
                report_item(
                    "unknown_node_block",
                    f"NODE block for alias {alias!r} matches no node in this batch; ignored",
                    call_id=batch_result.record.call_id,
                )
            )
    if parsed.stray:
        reports.append(
            report_item(
                "stray_lines",
                f"{len(parsed.stray)} line(s) outside any NODE block; ignored",
                call_id=batch_result.record.call_id,
            )
        )
    return _ParsedPrimary(
        block_groups=tuple((alias, tuple(blocks)) for alias, blocks in blocks_by_alias.items()),
        reports=tuple(reports),
    )


def _prepare_repair_call(
    batch_node: BatchNode,
    node_failure: NodeEditFailure,
    *,
    record: LlmCallRecord,
    initial_block: RawNodeBlock | None,
    user_prompt_template: str,
    repair_user_template: str,
    control_prompt_locale: ControlPromptLocale,
) -> NodeEditRepairCall:
    repair_projection = project_batch([batch_node])
    node_edit_prompt = render_node_edit_user_prompt(
        template=user_prompt_template,
        projection=repair_projection,
        control_prompt_locale=control_prompt_locale,
    )
    repair_user_prompt = substitute_prompt_blocks(
        repair_user_template,
        {
            "initial_failure": _repair_feedback(
                node_failure,
                control_prompt_locale,
            ),
            "node_edit_prompt": node_edit_prompt,
        },
        prompt_name="node-edit-block-repair",
    )
    return NodeEditRepairCall(
        batch_node=batch_node,
        projection=repair_projection,
        user_prompt=repair_user_prompt,
        initial_failure_code=node_failure.code,
        initial_failure_detail=node_failure.detail,
        initial_record=record,
        initial_raw_block=(initial_block.raw_text if initial_block is not None else None),
    )


def _repair_feedback(
    node_failure: NodeEditFailure,
    control_prompt_locale: ControlPromptLocale,
) -> str:
    locale = coerce_control_prompt_locale(control_prompt_locale)
    if locale is ControlPromptLocale.zh:
        return f"失败类别：{node_failure.code}\n修复重点：{_REPAIR_GUIDANCE[node_failure.code]}"
    return (
        f"FAILURE_CATEGORY {node_failure.code}\n"
        f"REPAIR_FOCUS {_REPAIR_GUIDANCE_EN[node_failure.code]}"
    )


def _compile_repair_wave(
    repair_results: Sequence[NodeEditRepairResult],
    atoms_by_id: Mapping[str, MaterialAtom],
    *,
    protocol: str,
) -> _RepairCompilation:
    compiled: list[CompiledNode] = []
    reports: list[AuditReportItem] = []
    for repair_result in repair_results:
        compiled_node, report = _compile_repair_result(
            repair_result,
            atoms_by_id,
            protocol=protocol,
        )
        compiled.append(compiled_node)
        reports.append(report)
    return _RepairCompilation(compiled=tuple(compiled), reports=tuple(reports))


def _compile_repair_result(
    repair_result: NodeEditRepairResult,
    atoms_by_id: Mapping[str, MaterialAtom],
    *,
    protocol: str,
) -> tuple[CompiledNode, AuditReportItem]:
    request = repair_result.request
    batch_node = request.batch_node
    record = repair_result.record
    repair_block: RawNodeBlock | None = None
    repair_failure: NodeEditFailure | None = None
    compiled_node: CompiledNode | None = None

    if repair_result.error is not None or repair_result.text is None or record is None:
        repair_failure = failure("repair_transport", "repair call failed at transport")
    else:
        parsed = parse_node_edit_dsl(repair_result.text)
        alias = request.projection.candidate_manifest.alias_for(batch_node.candidate_id)
        repair_block, repair_failure = select_node_block(
            [candidate for candidate in parsed.blocks if candidate.candidate_alias == alias]
        )
        if repair_block is not None:
            outcome = compile_node_block(
                batch_node,
                repair_block,
                request.projection.material_manifest,
                atoms_by_id,
                protocol=protocol,
            )
            if isinstance(outcome, NodeEditFailure):
                repair_failure = outcome
            else:
                compiled_node = outcome
                repair_failure = None

    if repair_failure is None and record is not None and compiled_node is not None:
        compiled_node = with_producer_metadata(
            compiled_node,
            unit_id=f"repair:{batch_node.candidate_id}",
            call_id=record.call_id,
        )
        report = report_item(
            "node_block_repaired",
            f"single-node repair recovered invalid block: {request.initial_failure_detail}",
            artifact_ref=batch_node.candidate_id,
            trace_refs=[batch_node.candidate_id],
            call_id=record.call_id,
            raw_response_path=record.raw_response_path,
            metadata={"initial_failure_code": request.initial_failure_code},
        )
        return compiled_node, report

    if repair_failure is None:
        raise RuntimeError("node_edit repair produced neither result nor failure")
    recovered = compile_all_keep(
        batch_node,
        protocol=protocol,
        fallback_reason=(
            f"{request.initial_failure_detail}; repair failed: {repair_failure.detail}"
        ),
    )
    producer_record = record or request.initial_record
    recovered = with_producer_metadata(
        recovered,
        unit_id=f"repair:{batch_node.candidate_id}",
        call_id=producer_record.call_id,
    )
    report = report_item(
        "node_fallback_all_keep",
        "node repair exhausted after one attempt; "
        f"initial={request.initial_failure_detail}; "
        f"repair={repair_failure.detail}",
        artifact_ref=batch_node.candidate_id,
        trace_refs=[batch_node.candidate_id],
        call_id=producer_record.call_id,
        raw_response_path=producer_record.raw_response_path,
        metadata={
            "initial_failure_code": request.initial_failure_code,
            "repair_failure_code": repair_failure.code,
            "initial_raw_block": request.initial_raw_block,
            "repair_raw_block": (repair_block.raw_text if repair_block is not None else None),
            "repair_attempts": 1,
        },
    )
    return recovered, report


def _assemble_output(
    selection_plan: SelectionPlan,
    graph: CandidateGraph,
    *,
    compiled: Sequence[CompiledNode],
    reports: Sequence[AuditReportItem],
    protocol: str,
    batch_count: int,
    max_tokens: int,
    max_nodes: int,
) -> NodeEditBuildOutput:
    compiled_by_id = {item.result.candidate_id: item for item in compiled}
    emitted: set[str] = set()
    node_edits: list[NodeEditResult] = []
    for candidate_id_ in selection_plan.selected_sequence:
        if candidate_id_ in compiled_by_id and candidate_id_ not in emitted:
            emitted.add(candidate_id_)
            node_edits.append(compiled_by_id[candidate_id_].result)

    edited_materials: list[MaterialAtom] = []
    seen: set[str] = set()
    for result in node_edits:
        node_edited = sorted(
            compiled_by_id[result.candidate_id].edited_materials,
            key=lambda atom: (atom.source_order, atom.id),
        )
        for atom in node_edited:
            if atom.id not in seen:
                seen.add(atom.id)
                edited_materials.append(atom)

    metadata: dict[str, Any] = {
        "node_edit_protocol": protocol,
        "batch_count": batch_count,
        "batch_max_input_tokens": max_tokens,
        "max_nodes_per_batch": max_nodes,
    }
    plan = NodeEditPlan(
        plan_id=node_edit_plan_id(
            selection_plan.project_id,
            selection_plan.plan_id,
            graph.graph_id,
        ),
        project_id=selection_plan.project_id,
        candidate_graph_id=graph.graph_id,
        selection_plan_id=selection_plan.plan_id,
        node_edits=node_edits,
        metadata=metadata,
    )
    return NodeEditBuildOutput(
        plan=plan,
        edited_materials=edited_materials,
        reports=list(reports),
    )
