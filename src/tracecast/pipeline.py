"""End-to-end pipeline driver: raw sources -> target-language script.

Wires the stage packages in their canonical order::

    raw -> Ingestion -> Planning -> Selection -> Node Edit -> Packing/RenderContract
        -> Fact Context -> Name Resolution -> Rendering -> Validation -> Assembly

The driver owns orchestration and run-artifact persistence only. Every fact/shape/
stable-id rule lives inside the stages; the single provider boundary is ``llm`` (each
stage calls it through its injectable ``call`` seam). Validation is report-only and
never gates Assembly -- a script can still ship flagged ``assembled_with_issues``.

Each stage's artifact is persisted under ``runs/<run_id>/`` via :class:`RunStore`, so a
run is fully inspectable after the fact. A run id is single-use; reruns get a new id.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracecast.assembly import assemble_script
from tracecast.control_prompt import (
    ControlPromptDecision,
    ControlPromptLocale,
    ControlPromptSample,
    resolve_control_prompt_locale,
)
from tracecast.domain import (
    AssemblyReport,
    AuditReportItem,
    CandidateGraph,
    ChunkArtifact,
    LlmCallRecord,
    MaterialAtom,
    NameOverride,
    NameResolutionPlan,
    NodeEditPlan,
    SelectionPlan,
)
from tracecast.hosts import HostProfile, require_host_language
from tracecast.ingestion import (
    IngestionBudgets,
    IngestionError,
    SourceInput,
    build_source_inputs,
    ingest_sources,
    persist_ingest_result,
)
from tracecast.language import require_bcp47_language_tag
from tracecast.llm import call_llm
from tracecast.name_resolution import (
    DEFAULT_NAME_RESOLUTION_MODE,
    NameResolutionError,
    NameResolutionMode,
    build_name_resolution,
)
from tracecast.node_edit import build_node_edit_outputs
from tracecast.packing import PackingResult, chunk_policy_from_budgets, pack
from tracecast.planning import (
    build_candidate_graph,
    build_selection_plan,
)
from tracecast.project import ProjectConfig, override_script_mismatch_reports
from tracecast.rendering import FactContextResult, build_fact_contexts, render_chunks
from tracecast.storage import (
    ARTIFACT_SPECS,
    ASSEMBLY_REPORT,
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    EDITED_MATERIALS,
    LLM_CALL_RECORDS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SCRIPT,
    SELECTION_PLAN,
    RunStore,
    RuntimeConfig,
)
from tracecast.validation import ValidationContext, ValidationReport, run_validation


@dataclass(frozen=True)
class RunResult:
    """Mechanical run summary (never a judgement of prose quality)."""

    run_id: str
    runs_root: str
    sources: int
    atoms: int
    nodes: int
    selected_nodes: int
    chunks: int
    script_chars: int
    audit_items: int
    calls: int
    upstream_has_issues: bool
    control_prompt_decision: ControlPromptDecision | None = None


@dataclass(frozen=True)
class RenderTailResult:
    """Artifacts produced by the shared Packing-to-Assembly use case."""

    packing: PackingResult
    name_resolution: NameResolutionPlan
    fact_context: FactContextResult
    chunks: list[ChunkArtifact]
    validation: ValidationReport
    script: str
    assembly_report: AssemblyReport
    call_records: list[LlmCallRecord]
    upstream_has_issues: bool


def run_render_tail(
    *,
    graph: CandidateGraph,
    selection: SelectionPlan,
    node_edit_plan: NodeEditPlan,
    edited_materials: list[MaterialAtom],
    host: HostProfile,
    output_language: str,
    name_overrides: Sequence[NameOverride],
    name_resolution_mode: NameResolutionMode = DEFAULT_NAME_RESOLUTION_MODE,
    config: RuntimeConfig,
    store: RunStore,
    call: Any = call_llm,
    validation_context: ValidationContext | None = None,
    stage_reports: Sequence[AuditReportItem] = (),
    block_overrides: Mapping[str, str] | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> RenderTailResult:
    """Run the canonical Packing → Assembly tail and persist its artifacts."""

    packing = pack(
        selection,
        node_edit_plan,
        edited_materials,
        chunk_policy_from_budgets(config.stage_budgets.get("rendering", {})),
        candidate_graph=graph,
    )
    store.write_model(CHUNK_PLAN, packing.chunk_plan)
    store.write_records(RENDER_CONTRACTS, packing.contracts)

    # Fact Context runs on raw contracts.  Automatic Name Table evidence is the
    # complete selected material set only; code also scans directives and these
    # cards for operator matches and occurrence trace.  Neither upstream artifact
    # is rewritten by the resulting prompt-only replacement plan.
    fact_context = build_fact_contexts(
        packing.contracts,
        config=config,
        store=store,
        output_language=output_language,
        call=call,
        control_prompt_locale=control_prompt_locale,
    )
    store.write_records(RENDER_FACT_CONTEXTS, fact_context.contexts)
    try:
        names = build_name_resolution(
            packing.contracts,
            project_id=selection.project_id,
            output_language=output_language,
            mode=name_resolution_mode,
            overrides=name_overrides,
            fact_contexts=fact_context.contexts,
            config=config,
            store=store,
            call=call,
            control_prompt_locale=control_prompt_locale,
        )
    except NameResolutionError as exc:
        store.write_records(NAME_RESOLUTION_REPORT, exc.reports)
        raise
    store.write_model(NAME_RESOLUTION_PLAN, names.plan)
    store.write_records(NAME_RESOLUTION_REPORT, names.reports)
    rendering = render_chunks(
        packing.contracts,
        persona=host,
        config=config,
        store=store,
        call=call,
        name_resolution=names.plan,
        output_language=output_language,
        fact_contexts=fact_context.contexts,
        block_overrides=block_overrides,
        control_prompt_locale=control_prompt_locale,
    )
    chunks = rendering.artifacts
    store.write_records(CHUNK_ARTIFACTS, chunks)

    call_records = (
        store.read_records(LLM_CALL_RECORDS) if store.exists(LLM_CALL_RECORDS) else []
    )
    base_context = validation_context or ValidationContext()
    context = replace(
        base_context,
        edited_materials=edited_materials,
        candidate_graph=graph,
        selection_plan=selection,
        node_edit_plan=node_edit_plan,
        chunk_plan=packing.chunk_plan,
        contracts=packing.contracts,
        chunks=chunks,
        call_records=call_records,
        stage_reports=(
            tuple(base_context.stage_reports)
            + tuple(stage_reports)
            + tuple(packing.reports)
            + tuple(names.reports)
            + tuple(fact_context.reports)
            + tuple(rendering.reports)
        ),
        raw_chunk_text={chunk.chunk_id: chunk.text for chunk in chunks},
    )
    validation = run_validation(context)
    store.write_records(AUDIT_REPORT, validation.items)

    upstream_has_issues = bool(validation.items)
    script, assembly_report = assemble_script(
        chunk_plan=packing.chunk_plan,
        chunks=chunks,
        validation_report_ref=AUDIT_REPORT.relpath,
        upstream_has_issues=upstream_has_issues,
    )
    store.write_text(SCRIPT, script)
    store.write_model(ASSEMBLY_REPORT, assembly_report)
    return RenderTailResult(
        packing=packing,
        name_resolution=names.plan,
        fact_context=fact_context,
        chunks=chunks,
        validation=validation,
        script=script,
        assembly_report=assembly_report,
        call_records=call_records,
        upstream_has_issues=upstream_has_issues,
    )


def run_pipeline(
    project: ProjectConfig,
    project_dir: str | Path,
    *,
    host: HostProfile,
    config: RuntimeConfig,
    runs_root: str | Path = "runs",
    run_id: str | None = None,
    store: RunStore | None = None,
    max_sources: int | None = None,
    limit_source_chars: int | None = None,
    output_language: str | None = None,
    name_resolution_mode: NameResolutionMode | None = None,
    selection_human_directive: Sequence[str] | None = None,
    control_prompt_decision: ControlPromptDecision | None = None,
    call: Any = call_llm,
) -> RunResult:
    """Run the full chain for one project and persist every stage artifact.

    ``call`` is the single provider seam threaded into every LLM stage (default
    :func:`tracecast.llm.call_llm`); a fake can be injected to exercise the whole
    driver offline. Returns a mechanical :class:`RunResult`; the produced script and
    the per-stage artifacts under ``runs/<run_id>/`` are the real output.
    """
    effective_output_language = require_bcp47_language_tag(
        output_language if output_language is not None else project.output_language,
        field="output_language",
    )
    if effective_output_language.casefold() in {"und", "unknown"}:
        raise ValueError("output_language must identify a concrete target language")
    effective_name_resolution_mode = (
        NameResolutionMode(name_resolution_mode)
        if name_resolution_mode is not None
        else project.name_resolution_mode
    )
    require_host_language(host, effective_output_language)
    if store is None:
        run_id = run_id or f"{project.project_id}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
        store = RunStore.create_for_run(runs_root, run_id)
    else:
        if run_id is not None and run_id != store.run_id:
            raise ValueError(f"run_id {run_id!r} does not match store {store.run_id!r}")
        if any(store.exists(spec) for spec in ARTIFACT_SPECS):
            raise FileExistsError(f"run store already contains pipeline artifacts: {store.run_dir}")
        run_id = store.run_id
        runs_root = store.run_dir.parent

    # 1. Ingestion -- deterministic slicing + atom-boundary DSL; rebuilds verbatim atoms.
    inputs = build_source_inputs(
        project, project_dir, max_sources=max_sources, limit_source_chars=limit_source_chars
    )
    effective_control_prompt = (
        ControlPromptDecision.model_validate(control_prompt_decision)
        if control_prompt_decision is not None
        else resolve_pipeline_control_prompt(inputs)
    )
    control_prompt_locale = effective_control_prompt.variant
    budgets = IngestionBudgets.from_config(config)
    ingest = ingest_sources(
        inputs,
        config=config,
        store=store,
        budgets=budgets,
        call=call,
        control_prompt_locale=control_prompt_locale,
    )
    persist_ingest_result(store, ingest)
    if not ingest.ok:
        # Validation is not reached when the evidence path is incomplete, so
        # persist the already-unified stage reports before terminating the root.
        store.write_records(AUDIT_REPORT, ingest.reports)
        raise IngestionError(ingest)

    # 2-3. Planning + Selection -- candidate graph, then a host-driven accepted path.
    graph_output = build_candidate_graph(
        ingest.windows,
        ingest.atoms,
        project_id=project.project_id,
        config=config,
        store=store,
        call=call,
        control_prompt_locale=control_prompt_locale,
    )
    graph = graph_output.graph
    store.write_model(CANDIDATE_GRAPH, graph)
    selection_persona = host.selection_persona()
    if selection_human_directive is not None:
        selection_persona["viewpoint_profile"]["human_directive"] = list(
            selection_human_directive
        )
    selection_output = build_selection_plan(
        graph,
        persona=selection_persona,
        config=config,
        store=store,
        call=call,
        control_prompt_locale=control_prompt_locale,
    )
    selection = selection_output.plan
    store.write_model(SELECTION_PLAN, selection)

    # 4. Node Edit -- keep/condense/omit ledger + run-scoped condensed materials.
    node_edit_output = build_node_edit_outputs(
        selection,
        graph,
        ingest.atoms,
        config=config,
        store=store,
        call=call,
        control_prompt_locale=control_prompt_locale,
    )
    node_edit_plan = node_edit_output.plan
    edited_materials = node_edit_output.edited_materials
    store.write_model(NODE_EDIT_PLAN, node_edit_plan)
    store.write_records(EDITED_MATERIALS, edited_materials)
    tail = run_render_tail(
        graph=graph,
        selection=selection,
        node_edit_plan=node_edit_plan,
        edited_materials=edited_materials,
        host=host,
        output_language=effective_output_language,
        name_overrides=project.name_overrides,
        name_resolution_mode=effective_name_resolution_mode,
        config=config,
        store=store,
        call=call,
        validation_context=ValidationContext(
            snapshots=ingest.snapshots,
            pieces=ingest.pieces,
            atoms=ingest.atoms,
        ),
        stage_reports=(
            tuple(ingest.reports)
            + tuple(graph_output.reports)
            + tuple(selection_output.reports)
            + tuple(node_edit_output.stage_reports)
            + tuple(override_script_mismatch_reports(project.name_overrides))
        ),
        control_prompt_locale=control_prompt_locale,
    )

    return RunResult(
        run_id=run_id,
        runs_root=str(runs_root),
        sources=len(inputs),
        atoms=len(ingest.atoms),
        nodes=len(graph.nodes),
        selected_nodes=len(selection.selected_sequence),
        chunks=len(tail.chunks),
        script_chars=len(tail.script),
        audit_items=len(tail.validation.items),
        calls=len(tail.call_records),
        upstream_has_issues=tail.upstream_has_issues,
        control_prompt_decision=effective_control_prompt,
    )


def resolve_pipeline_control_prompt(
    inputs: Sequence[SourceInput],
) -> ControlPromptDecision:
    """Resolve the one control-prompt decision shared by a root pipeline run."""

    return resolve_control_prompt_locale(
        [
            ControlPromptSample(text=source.raw_text, language_tag=source.language)
            for source in inputs
        ]
    )
