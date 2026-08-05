"""Planner + compression stage: rebuild the high-recall ``CandidateGraph``.

Source-isolated planning windows project atoms into ``planner_graph_dsl`` seed
nodes; if the whole graph is still
too granular, ``planner_node_compress_dsl`` repeatedly compresses each planning
window's node summaries.  The default main chain does not generate semantic
edges; externally supplied ``CandidateGraph`` values may still carry them for
Selection compatibility.

The model emits structure + non-fact labels only; code assigns every stable id.
Malformed DSL and invariant violations become explicit builder reports. Nothing
is fabricated, and recoverable input is preserved rather than silently repaired.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.concurrency import route_max_concurrency, run_ordered_concurrently
from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    AliasManifest,
    CandidateGraph,
    CandidateNode,
    LlmCallRecord,
    LlmWorkUnitRef,
    MaterialAtom,
    SourceSpan,
    SourceWindow,
    candidate_id,
    estimate_tokens,
    graph_id,
)
from tracecast.llm import call_llm
from tracecast.prompt_components import load_planning_protocol_catalog
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.storage import RunStore, RuntimeConfig

from .compression import compress_nodes
from .dsl import RawNode, parse_planner_dsl
from .policy import (
    compression_max_rounds,
    max_final_node_count,
    planning_budgets,
    target_material_tokens_per_node,
    target_node_count,
)
from .projection import (
    Projection,
    project_window,
    render_planner_user_prompt,
)
from .results import (
    CandidateGraphBuildOutput,
    annotate_diagnostics_with_call,
    diagnostics_to_audit_reports,
)
from .vocab import (
    ALLOWED_NARRATIVE_FUNCTIONS,
    COMPRESS_ROUTE,
    PLANNER_ROUTE,
    STAGE_PLANNING,
    diagnostic,
)
from .windowing import (
    allocate_window_targets,
    group_atoms_by_source,
    pack_planning_windows,
    retained_window_token_loads,
    window_ratio_targets,
)

#: Patchable provider-call seam (signature-compatible with ``llm.call_llm``).
LlmCall = Callable[..., tuple[str, LlmCallRecord]]

_DEFAULT_COVERAGE_MIN = 0.9


@dataclass(frozen=True)
class _PlannerWindowCall:
    """Prepared node-construction call for one source window."""

    window: SourceWindow
    projection: Projection
    user_prompt: str


@dataclass(frozen=True)
class _PlannerWindowResult:
    """Provider result for one prepared planner window call."""

    window: SourceWindow
    projection: Projection
    text: str
    record: LlmCallRecord


def _message(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def _build_node(
    raw: RawNode,
    *,
    project_id: str,
    window: SourceWindow,
    manifest: AliasManifest,
    order_of: Mapping[str, int],
    material_tokens_of: Mapping[str, int],
    claimed: set[str],
    diagnostics: list[dict],
) -> CandidateNode | None:
    """Rebuild one ``CandidateNode`` from a raw planner row, enforcing invariants."""
    seen: set[str] = set()
    kept: list[str] = []
    for alias in raw.material_aliases:
        try:
            atom_id_ = manifest.resolve(alias)
        except (KeyError, ValueError):
            diagnostics.append(
                diagnostic(STAGE_PLANNING, "unknown_material_alias",
                           f"material alias {alias!r} not in window manifest",
                           alias=alias, window_id=window.window_id)
            )
            continue
        if atom_id_ in seen:
            diagnostics.append(
                diagnostic(STAGE_PLANNING, "duplicate_material_in_node",
                           f"material {alias!r} listed twice in one node", alias=alias)
            )
            continue
        if atom_id_ in claimed:
            diagnostics.append(
                diagnostic(STAGE_PLANNING, "atom_overlap",
                           "material already belongs to another node; dropped to keep "
                           "nodes non-overlapping", atom_id=atom_id_, window_id=window.window_id)
            )
            continue
        seen.add(atom_id_)
        kept.append(atom_id_)
    if not kept:
        diagnostics.append(
            diagnostic(STAGE_PLANNING, "empty_node_dropped",
                       "node has no usable material after invariant checks; dropped",
                       window_id=window.window_id, row=raw.row)
        )
        return None

    kept.sort(key=lambda a: order_of[a])
    claimed.update(kept)

    if raw.narrative_function not in ALLOWED_NARRATIVE_FUNCTIONS:
        diagnostics.append(
            diagnostic(STAGE_PLANNING, "unknown_narrative_function",
                       f"narrative_function {raw.narrative_function!r} outside the v1 set",
                       value=raw.narrative_function)
        )

    orders = [order_of[a] for a in kept]
    metadata: dict[str, Any] = {"source_ref": window.source_id}

    return CandidateNode(
        candidate_id=candidate_id(project_id, kept),
        source_window=window.window_id,
        summary=raw.summary,
        material_ids=kept,
        material_estimated_tokens=sum(material_tokens_of[item] for item in kept),
        narrative_function=raw.narrative_function,
        source_span=SourceSpan(start=min(orders), end=max(orders)),
        metadata=metadata,
    )


def build_candidate_graph(
    windows: Sequence[SourceWindow],
    atoms: Sequence[MaterialAtom],
    *,
    project_id: str,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall = call_llm,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> CandidateGraphBuildOutput:
    """Build an edge-free ``CandidateGraph`` via seed and compression calls."""
    control_prompt_locale = coerce_control_prompt_locale(control_prompt_locale)
    order_of = {atom.id: atom.source_order for atom in atoms}
    material_tokens_of = {atom.id: estimate_tokens(atom.content) for atom in atoms}
    grouped, source_order, diagnostics = group_atoms_by_source(windows, atoms)
    budgets = planning_budgets(config)
    node_budget = int(budgets.get("node_window_max_input_tokens", 32768))
    target_material_tokens = target_material_tokens_per_node(budgets)
    max_final_nodes = max_final_node_count(budgets)
    target_final_nodes_override = budgets.get("target_final_node_count")
    if target_final_nodes_override is not None:
        target_final_nodes_override = int(target_final_nodes_override)
        if target_final_nodes_override <= 0:
            target_final_nodes_override = None
    compression_rounds = compression_max_rounds(budgets)

    planner_system, planner_version = load_system_prompt(
        PLANNER_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    planner_user_template, planner_user_version = load_user_prompt(
        PLANNER_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    protocol_version = load_planning_protocol_catalog(control_prompt_locale).version
    planner_prompt_version = (
        f"{planner_version}+{planner_user_version}+{protocol_version}"
    )
    compress_prompt_version: str | None = None
    compress_system = ""
    compress_user_template = ""
    if compression_rounds:
        compress_system, compress_version = load_system_prompt(
            COMPRESS_ROUTE,
            config,
            control_prompt_locale=control_prompt_locale,
        )
        compress_user_template, compress_user_version = load_user_prompt(
            COMPRESS_ROUTE,
            config,
            control_prompt_locale=control_prompt_locale,
        )
        compress_prompt_version = (
            f"{compress_version}+{compress_user_version}+{protocol_version}"
        )

    planning_packs = pack_planning_windows(
        grouped,
        source_order,
        budget=node_budget,
        planner_user_template=planner_user_template,
        control_prompt_locale=control_prompt_locale,
    )
    window_calls: list[_PlannerWindowCall] = []
    for pack in planning_packs:
        window = pack.window
        projection = project_window(window, pack.atoms)
        user_prompt = render_planner_user_prompt(
            template=planner_user_template,
            projection=projection,
            control_prompt_locale=control_prompt_locale,
        )
        exact_tokens = estimate_tokens(user_prompt)
        if exact_tokens > node_budget:
            diagnostics.append(
                diagnostic(STAGE_PLANNING, "window_over_budget",
                           "planner projection exceeds node_window_max_input_tokens",
                           window_id=window.window_id,
                           estimated_input_tokens=exact_tokens,
                           budget=node_budget)
            )
        window_calls.append(_PlannerWindowCall(window, projection, user_prompt))

    claimed: set[str] = set()
    nodes: list[CandidateNode] = []

    for result in _run_planner_window_calls(
        window_calls,
        config=config,
        store=store,
        call=call,
        planner_system=planner_system,
        planner_prompt_version=planner_prompt_version,
    ):
        parsed = parse_planner_dsl(result.text)
        annotate_diagnostics_with_call(parsed.diagnostics, result.record)
        diagnostics.extend(parsed.diagnostics)

        for raw in parsed.nodes:
            node = _build_node(
                raw,
                project_id=project_id,
                window=result.window,
                manifest=result.projection.manifest,
                order_of=order_of,
                material_tokens_of=material_tokens_of,
                claimed=claimed,
                diagnostics=diagnostics,
            )
            if node is not None:
                metadata = dict(node.metadata)
                metadata.update(
                    {
                        "producer_unit_id": result.window.window_id,
                        "producer_call_id": result.record.call_id,
                        "producer_lineage": [
                            {
                                "unit_id": result.window.window_id,
                                "call_id": result.record.call_id,
                                "summary": node.summary,
                                "narrative_function": node.narrative_function,
                                "material_count": len(node.material_ids),
                                "source_span": (
                                    node.source_span.model_dump()
                                    if node.source_span is not None
                                    else None
                                ),
                            }
                        ],
                    }
                )
                nodes.append(node.model_copy(update={"metadata": metadata}))

    retained_tokens_by_window = retained_window_token_loads(
        nodes,
        material_tokens_of,
    )
    ratio_window_targets = window_ratio_targets(
        retained_tokens_by_window,
        target_material_tokens,
    )
    ratio_target_nodes = sum(ratio_window_targets.values())
    target_nodes = target_node_count(budgets, ratio_target_nodes)
    if target_final_nodes_override is None and target_nodes == ratio_target_nodes:
        window_targets = ratio_window_targets
    else:
        window_targets = allocate_window_targets(
            retained_tokens_by_window,
            target_nodes,
            upper_bounds=(
                ratio_window_targets
                if target_final_nodes_override is None
                else None
            ),
        )

    compression_result = compress_nodes(
        nodes,
        project_id=project_id,
        order_of=order_of,
        material_tokens_of=material_tokens_of,
        target_material_tokens_per_node=target_material_tokens,
        max_final_node_count=max_final_nodes,
        target_final_node_count_override=target_final_nodes_override,
        target_node_count=target_nodes,
        window_targets=window_targets,
        window_ratio_targets=ratio_window_targets,
        window_retained_material_tokens=retained_tokens_by_window,
        max_rounds=compression_rounds,
        config=config,
        store=store,
        call=call,
        compress_system=compress_system,
        compress_user_template=compress_user_template,
        compress_prompt_version=compress_prompt_version,
        diagnostics=diagnostics,
        control_prompt_locale=control_prompt_locale,
    )
    nodes = compression_result.nodes
    compression_stats = compression_result.metadata
    claimed = {material_id for node in nodes for material_id in node.material_ids}

    if not compression_stats.get("target_met", False):
        diagnostics.append(
            diagnostic(
                STAGE_PLANNING,
                "node_target_not_met",
                "final node count is still above its effective target",
                node_count=len(nodes),
                target_node_count=target_nodes,
            )
        )

    metadata = _graph_metadata(
        atoms,
        claimed,
        budgets,
        diagnostics,
        planner_version=planner_prompt_version,
        compress_version=compress_prompt_version,
        compression_stats=compression_stats,
        planning_window_count=len(planning_packs),
    )
    graph = CandidateGraph(
        graph_id=graph_id(project_id, [n.candidate_id for n in nodes]),
        project_id=project_id,
        nodes=nodes,
        edges=[],
        metadata=metadata,
    )
    return CandidateGraphBuildOutput(
        graph=graph,
        reports=diagnostics_to_audit_reports(
            diagnostics,
            artifact_ref=graph.graph_id,
        ),
    )


def _run_planner_window_calls(
    window_calls: list[_PlannerWindowCall],
    *,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
    planner_system: str,
    planner_prompt_version: str,
) -> list[_PlannerWindowResult]:
    """Run planner window calls concurrently; return results in window order."""

    def run_one(window_call: _PlannerWindowCall) -> _PlannerWindowResult:
        text, record = call(
            operation_route=PLANNER_ROUTE,
            messages=[
                _message("system", planner_system),
                _message("user", window_call.user_prompt),
            ],
            config=config,
            store=store,
            prompt_version=planner_prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="planning_seed",
                unit_id=window_call.window.window_id,
                input_refs=[
                    material_id
                    for _alias, material_id in window_call.projection.manifest.items()
                ],
            ),
        )
        return _PlannerWindowResult(
            window=window_call.window,
            projection=window_call.projection,
            text=text,
            record=record,
        )

    return run_ordered_concurrently(
        window_calls,
        run_one,
        max_workers=route_max_concurrency(config, PLANNER_ROUTE),
    )


def _graph_metadata(
    atoms: Sequence[MaterialAtom],
    claimed: set[str],
    budgets: Mapping[str, Any],
    diagnostics: list[dict],
    *,
    planner_version: str,
    compress_version: str | None,
    compression_stats: Mapping[str, Any],
    planning_window_count: int,
) -> dict[str, Any]:
    total = len(atoms)
    referenced = len(claimed)
    material_estimated_tokens = sum(estimate_tokens(atom.content) for atom in atoms)
    ratio = referenced / total if total else 1.0
    threshold = float(budgets.get("coverage_min", _DEFAULT_COVERAGE_MIN))
    if ratio < threshold:
        diagnostics.append(
            diagnostic(STAGE_PLANNING, "coverage_below_threshold",
                       f"coverage {ratio:.3f} < threshold {threshold:.3f}")
        )
    return {
        "planner_protocol": planner_version,
        "node_compress_protocol": compress_version,
        "planning_windows": planning_window_count,
        "node_compression": dict(compression_stats),
        "input": {
            "material_count": total,
            "material_estimated_tokens": material_estimated_tokens,
        },
        "coverage": {
            "referenced_material_count": referenced,
            "total_material_count": total,
            "ratio": ratio,
            "threshold": threshold,
        },
    }
