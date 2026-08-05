"""Planning node-compression calls and deterministic result compilation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.concurrency import route_max_concurrency, run_ordered_concurrently
from tracecast.control_prompt import ControlPromptLocale
from tracecast.domain import (
    AliasManifest,
    CandidateNode,
    LlmCallRecord,
    LlmWorkUnitRef,
    SourceSpan,
    candidate_id,
    estimate_tokens,
)
from tracecast.storage import RunStore, RuntimeConfig

from .dsl import RawCompressedNode, parse_node_compression_dsl
from .policy import planning_budgets
from .projection import (
    Projection,
    project_compression_nodes,
    render_node_compress_user_prompt,
)
from .results import annotate_diagnostics_with_call
from .vocab import COMPRESS_ROUTE, STAGE_PLANNING, diagnostic
from .windowing import window_targets_met

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


@dataclass(frozen=True)
class CompressionOutput:
    """Final node frontier and compression audit metadata."""

    nodes: list[CandidateNode]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _NodeCompressCall:
    source_window: str
    projection: Projection
    user_prompt: str
    child_nodes: list[CandidateNode]
    target_parent_count: int
    round_index: int


@dataclass(frozen=True)
class _NodeCompressResult:
    source_window: str
    projection: Projection
    child_nodes: list[CandidateNode]
    target_parent_count: int
    text: str
    record: LlmCallRecord


def compress_nodes(
    nodes: list[CandidateNode],
    *,
    project_id: str,
    order_of: Mapping[str, int],
    material_tokens_of: Mapping[str, int],
    target_material_tokens_per_node: int,
    max_final_node_count: int,
    target_final_node_count_override: int | None,
    target_node_count: int,
    window_targets: Mapping[str, int],
    window_ratio_targets: Mapping[str, int],
    window_retained_material_tokens: Mapping[str, int],
    max_rounds: int,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
    compress_system: str,
    compress_user_template: str,
    compress_prompt_version: str | None,
    diagnostics: list[dict],
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> CompressionOutput:
    """Compress each planning-window frontier until local and global targets are met."""
    metadata: dict[str, Any] = {
        "enabled": bool(window_targets and max_rounds),
        "target_material_tokens_per_node": target_material_tokens_per_node,
        "max_final_node_count": max_final_node_count,
        "target_final_node_count_override": target_final_node_count_override,
        "target_node_count": target_node_count,
        "token_ratio_target_node_count": sum(window_ratio_targets.values()),
        "allocated_window_target_node_count": sum(window_targets.values()),
        "window_targets": dict(window_targets),
        "window_ratio_targets": dict(window_ratio_targets),
        "window_retained_material_tokens": dict(window_retained_material_tokens),
        "max_rounds": max_rounds,
        "initial": _round_summary(nodes),
        "rounds": [],
    }
    current = list(nodes)
    seed_summaries_by_node_id = {
        node.candidate_id: [node.summary] for node in nodes
    }
    if not metadata["enabled"]:
        metadata["final"] = _round_summary(current)
        metadata["target_met"] = _targets_met(
            current,
            target_node_count=target_node_count,
            window_targets=window_targets,
        )
        return CompressionOutput(current, metadata)
    if _targets_met(
        current,
        target_node_count=target_node_count,
        window_targets=window_targets,
    ):
        metadata["final"] = _round_summary(current)
        metadata["target_met"] = True
        return CompressionOutput(current, metadata)
    if compress_prompt_version is None:
        diagnostics.append(
            diagnostic(
                STAGE_PLANNING,
                "compression_route_missing",
                "node compression target is configured but route prompts were not loaded",
            )
        )
        metadata["final"] = _round_summary(current)
        metadata["target_met"] = _targets_met(
            current,
            target_node_count=target_node_count,
            window_targets=window_targets,
        )
        return CompressionOutput(current, metadata)

    budgets = planning_budgets(config)
    budget = int(
        budgets.get(
            "node_compress_max_input_tokens",
            budgets.get("node_window_max_input_tokens", 32768),
        )
    )
    for round_index in range(1, max_rounds + 1):
        before_count = len(current)
        if _targets_met(
            current,
            target_node_count=target_node_count,
            window_targets=window_targets,
        ):
            break
        grouped = _nodes_by_window(current)
        calls: list[_NodeCompressCall] = []
        carried: dict[str, list[CandidateNode]] = {}
        for source_window in _ordered_windows(current):
            child_nodes = grouped[source_window]
            target_parent_count = max(1, window_targets.get(source_window, len(child_nodes)))
            if len(child_nodes) <= 1 or len(child_nodes) <= target_parent_count:
                carried[source_window] = child_nodes
                continue
            projection = project_compression_nodes(
                child_nodes,
                seed_summaries_by_node_id,
            )
            user_prompt = render_node_compress_user_prompt(
                template=compress_user_template,
                projection=projection,
                input_child_count=len(child_nodes),
                target_parent_count=target_parent_count,
                control_prompt_locale=control_prompt_locale,
            )
            exact_tokens = estimate_tokens(user_prompt)
            if exact_tokens > budget:
                diagnostics.append(
                    diagnostic(
                        STAGE_PLANNING,
                        "node_compress_over_budget",
                        "node-compress projection exceeds node_compress_max_input_tokens",
                        source_window=source_window,
                        round=round_index,
                        estimated_input_tokens=exact_tokens,
                        budget=budget,
                    )
                )
            calls.append(
                _NodeCompressCall(
                    source_window=source_window,
                    projection=projection,
                    user_prompt=user_prompt,
                    child_nodes=child_nodes,
                    target_parent_count=target_parent_count,
                    round_index=round_index,
                )
            )

        result_by_window = {
            result.source_window: result
            for result in _run_calls(
                calls,
                config=config,
                store=store,
                call=call,
                compress_system=compress_system,
                compress_prompt_version=compress_prompt_version,
            )
        }
        next_nodes: list[CandidateNode] = []
        for source_window in _ordered_windows(current):
            if source_window in carried:
                next_nodes.extend(carried[source_window])
                continue
            next_nodes.extend(
                _compile_result(
                    result_by_window[source_window],
                    project_id=project_id,
                    order_of=order_of,
                    material_tokens_of=material_tokens_of,
                    seed_summaries_by_node_id=seed_summaries_by_node_id,
                    round_index=round_index,
                    diagnostics=diagnostics,
                )
            )

        after_count = len(next_nodes)
        metadata["rounds"].append(
            {
                "round": round_index,
                "input_node_count": before_count,
                "output_node_count": after_count,
                "call_count": len(calls),
                "target_node_count": target_node_count,
                "over_target_windows": len(calls),
                "summary": _round_summary(next_nodes),
            }
        )
        current = next_nodes
        if after_count >= before_count:
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compression_no_shrink",
                    "compression round did not reduce node count; stopping further rounds",
                    round=round_index,
                    input_node_count=before_count,
                    output_node_count=after_count,
                )
            )
            break
    metadata["final"] = _round_summary(current)
    metadata["target_met"] = _targets_met(
        current,
        target_node_count=target_node_count,
        window_targets=window_targets,
    )
    return CompressionOutput(current, metadata)


def _targets_met(
    nodes: Sequence[CandidateNode],
    *,
    target_node_count: int,
    window_targets: Mapping[str, int],
) -> bool:
    return (
        len(nodes) <= target_node_count
        and window_targets_met(nodes, window_targets)
    )


def _compile_result(
    result: _NodeCompressResult,
    *,
    project_id: str,
    order_of: Mapping[str, int],
    material_tokens_of: Mapping[str, int],
    seed_summaries_by_node_id: dict[str, list[str]],
    round_index: int,
    diagnostics: list[dict],
) -> list[CandidateNode]:
    parsed = parse_node_compression_dsl(result.text)
    annotate_diagnostics_with_call(parsed.diagnostics, result.record)
    diagnostics.extend(parsed.diagnostics)
    child_by_id = {node.candidate_id: node for node in result.child_nodes}
    assigned_children: set[str] = set()
    compressed: list[CandidateNode] = []
    for raw in parsed.nodes:
        node = _build_node(
            raw,
            project_id=project_id,
            source_window=result.source_window,
            manifest=result.projection.manifest,
            child_by_id=child_by_id,
            assigned_children=assigned_children,
            order_of=order_of,
            material_tokens_of=material_tokens_of,
            round_index=round_index,
            diagnostics=diagnostics,
        )
        if node is None:
            continue
        node_materials = set(node.material_ids)
        selected_children = [
            child
            for child in result.child_nodes
            if set(child.material_ids).issubset(node_materials)
        ]
        producer_unit_id = f"{result.source_window}:compress:{round_index}"
        producer_lineage = [
            dict(entry)
            for child in selected_children
            for entry in child.metadata.get("producer_lineage", [])
            if isinstance(entry, dict)
        ]
        producer_lineage.append(
            {
                "unit_id": producer_unit_id,
                "call_id": result.record.call_id,
                "summary": node.summary,
                "narrative_function": node.narrative_function,
                "material_count": len(node.material_ids),
                "source_span": (
                    node.source_span.model_dump()
                    if node.source_span is not None
                    else None
                ),
                "compression_round": round_index,
            }
        )
        metadata = dict(node.metadata)
        metadata.update(
            {
                "producer_unit_id": producer_unit_id,
                "producer_call_id": result.record.call_id,
                "compression_input_node_ids": [
                    child.candidate_id for child in selected_children
                ],
                "compression_lineage_units": list(
                    dict.fromkeys(
                        unit_id
                        for child in selected_children
                        for unit_id in (
                            *child.metadata.get("compression_lineage_units", []),
                            child.metadata.get("producer_unit_id"),
                        )
                        if isinstance(unit_id, str) and unit_id
                    )
                ),
                "producer_lineage": producer_lineage,
            }
        )
        node = node.model_copy(update={"metadata": metadata})
        compressed.append(node)
        summaries: list[str] = []
        seen_children: set[str] = set()
        for alias in raw.child_aliases:
            try:
                child_id = result.projection.manifest.resolve(alias)
            except (KeyError, ValueError):
                continue
            if child_id in seen_children:
                continue
            seen_children.add(child_id)
            summaries.extend(seed_summaries_by_node_id.get(child_id, []))
        seed_summaries_by_node_id[node.candidate_id] = summaries or [node.summary]

    for child in result.child_nodes:
        if child.candidate_id not in assigned_children:
            compressed.append(
                _carry_child(
                    child,
                    round_index=round_index,
                    diagnostics=diagnostics,
                    reason="missing_from_compression_output",
                )
            )
            seed_summaries_by_node_id.setdefault(child.candidate_id, [child.summary])
    return compressed


def _build_node(
    raw: RawCompressedNode,
    *,
    project_id: str,
    source_window: str,
    manifest: AliasManifest,
    child_by_id: Mapping[str, CandidateNode],
    assigned_children: set[str],
    order_of: Mapping[str, int],
    material_tokens_of: Mapping[str, int],
    round_index: int,
    diagnostics: list[dict],
) -> CandidateNode | None:
    children: list[CandidateNode] = []
    seen_in_parent: set[str] = set()
    for alias in raw.child_aliases:
        try:
            child_id = manifest.resolve(alias)
        except (KeyError, ValueError):
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_unknown_child_alias",
                    f"child alias {alias!r} not in compression manifest",
                    alias=alias,
                    source_window=source_window,
                    round=round_index,
                )
            )
            continue
        child = child_by_id.get(child_id)
        if child is None:
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_missing_child_node",
                    "resolved child alias has no node in this compression group",
                    alias=alias,
                    candidate_id=child_id,
                    source_window=source_window,
                    round=round_index,
                )
            )
            continue
        if child_id in seen_in_parent:
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_duplicate_child_in_parent",
                    "child node listed twice in one compressed node",
                    alias=alias,
                    candidate_id=child_id,
                    source_window=source_window,
                    round=round_index,
                )
            )
            continue
        if child_id in assigned_children:
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_child_overlap",
                    "child node already assigned to another compressed node; duplicate ref dropped",
                    alias=alias,
                    candidate_id=child_id,
                    source_window=source_window,
                    round=round_index,
                )
            )
            continue
        seen_in_parent.add(child_id)
        assigned_children.add(child_id)
        children.append(child)
    if not children:
        diagnostics.append(
            diagnostic(
                STAGE_PLANNING,
                "compress_empty_node_dropped",
                "compressed node has no usable children after invariant checks; dropped",
                source_window=source_window,
                round=round_index,
                row=raw.row,
            )
        )
        return None
    material_ids = _child_material_ids(children, order_of)
    source_ref = _source_ref(children)
    metadata: dict[str, Any] = {
        "source_ref": source_ref,
        "compression_round": round_index,
        "compression_input_nodes": len(children),
        "material_count": len(material_ids),
    }
    if source_ref is None:
        metadata.pop("source_ref")
    return CandidateNode(
        candidate_id=candidate_id(project_id, material_ids),
        source_window=source_window,
        summary=raw.summary,
        material_ids=material_ids,
        material_estimated_tokens=sum(
            material_tokens_of[material_id] for material_id in material_ids
        ),
        narrative_function=None,
        source_span=_source_span(children),
        metadata=metadata,
    )


def _run_calls(
    calls: list[_NodeCompressCall],
    *,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
    compress_system: str,
    compress_prompt_version: str,
) -> list[_NodeCompressResult]:
    def run_one(compress_call: _NodeCompressCall) -> _NodeCompressResult:
        text, record = call(
            operation_route=COMPRESS_ROUTE,
            messages=[
                {"role": "system", "content": compress_system},
                {"role": "user", "content": compress_call.user_prompt},
            ],
            config=config,
            store=store,
            prompt_version=compress_prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="planning_compress",
                unit_id=(
                    f"{compress_call.source_window}:compress:"
                    f"{compress_call.round_index}"
                ),
                compression_round=compress_call.round_index,
                input_refs=[node.candidate_id for node in compress_call.child_nodes],
                metadata={"source_window_id": compress_call.source_window},
            ),
        )
        return _NodeCompressResult(
            source_window=compress_call.source_window,
            projection=compress_call.projection,
            child_nodes=compress_call.child_nodes,
            target_parent_count=compress_call.target_parent_count,
            text=text,
            record=record,
        )

    return run_ordered_concurrently(
        calls,
        run_one,
        max_workers=route_max_concurrency(config, COMPRESS_ROUTE),
    )


def _ordered_windows(nodes: Sequence[CandidateNode]) -> list[str]:
    return list(dict.fromkeys(node.source_window for node in nodes))


def _nodes_by_window(nodes: Sequence[CandidateNode]) -> dict[str, list[CandidateNode]]:
    grouped: dict[str, list[CandidateNode]] = {}
    for node in nodes:
        grouped.setdefault(node.source_window, []).append(node)
    return grouped


def _source_span(children: Sequence[CandidateNode]) -> SourceSpan | None:
    spans = [node.source_span for node in children if node.source_span is not None]
    if not spans:
        return None
    return SourceSpan(
        start=min(span.start for span in spans),
        end=max(span.end for span in spans),
    )


def _source_ref(children: Sequence[CandidateNode]) -> str | None:
    for node in children:
        source_ref = node.metadata.get("source_ref")
        if source_ref:
            return str(source_ref)
    return None


def _child_material_ids(
    children: Sequence[CandidateNode],
    order_of: Mapping[str, int],
) -> list[str]:
    material_ids = list(
        dict.fromkeys(
            material_id
            for child in children
            for material_id in child.material_ids
        )
    )
    material_ids.sort(key=lambda material_id: order_of.get(material_id, 0))
    return material_ids


def _carry_child(
    child: CandidateNode,
    *,
    round_index: int,
    diagnostics: list[dict],
    reason: str,
) -> CandidateNode:
    diagnostics.append(
        diagnostic(
            STAGE_PLANNING,
            "compress_child_carried",
            "child node carried forward because compression did not assign it exactly once",
            candidate_id=child.candidate_id,
            round=round_index,
            reason=reason,
        )
    )
    metadata = dict(child.metadata)
    metadata["compression_carried_round"] = round_index
    metadata["compression_carry_reason"] = reason
    return child.model_copy(update={"metadata": metadata})


def _round_summary(nodes: Sequence[CandidateNode]) -> dict[str, Any]:
    material_counts = [len(node.material_ids) for node in nodes]
    if not material_counts:
        return {
            "nodes": 0,
            "min_materials": 0,
            "avg_materials": 0.0,
            "max_materials": 0,
        }
    return {
        "nodes": len(nodes),
        "min_materials": min(material_counts),
        "avg_materials": sum(material_counts) / len(material_counts),
        "max_materials": max(material_counts),
    }
