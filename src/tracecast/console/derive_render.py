"""Packing-to-Assembly Render derived-run service."""

from __future__ import annotations

from typing import Any

from tracecast.control_prompt import control_prompt_locale_from_record
from tracecast.domain import estimate_tokens
from tracecast.llm import Usage, estimate_cost, resolve_cost_profile
from tracecast.name_resolution import (
    NAME_TABLE_ROUTE,
    NameResolutionMode,
    coerce_name_resolution_mode,
    estimate_name_resolution_inputs,
)
from tracecast.packing import chunk_policy_from_budgets, pack
from tracecast.pipeline import run_render_tail
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.rendering import (
    FACT_CONTEXT_ROUTE,
    RENDER_ROUTE,
    apply_render_block_overrides,
    coerce_persona,
    derive_render_chunk_positions,
    project_fact_context_outline,
    render_chunk_user_prompt,
    render_fact_context_user_prompt,
)
from tracecast.storage import (
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    EDITED_MATERIALS,
    NODE_EDIT_PLAN,
    SELECTION_PLAN,
)
from tracecast.validation import ValidationContext

from .context import ConsoleContext, load_project_context, resolve_lineage_effective
from .errors import BadRequest, Conflict
from .host_catalog import resolve_run_host
from .inventory import resolve_artifact_sources
from .jobs import Job
from .overrides import (
    ConfigOverrides,
    PersonaPatch,
    apply_config_overrides,
    apply_host_patch,
    preflight_prompts,
    route_snapshot,
)
from .run_service import (
    base_manifest,
    create_derived_store,
    progress_call,
    read_stage_reports,
    request_payload,
    run_manifested_job,
)


def derive_render(
    ctx: ConsoleContext,
    *,
    base_run_id: str,
    persona_patch: PersonaPatch | None = None,
    config_overrides: ConfigOverrides | None = None,
    render_block_overrides: dict[str, str] | None = None,
    name_resolution_mode: NameResolutionMode | str | None = None,
    run_label: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Re-pack and re-render a base run's node-edit outputs end to end."""
    sources = resolve_artifact_sources(
        ctx.runs_root,
        base_run_id,
        (CANDIDATE_GRAPH, SELECTION_PLAN, NODE_EDIT_PLAN, EDITED_MATERIALS),
    )
    graph = sources[CANDIDATE_GRAPH.relpath].read_model(CANDIDATE_GRAPH)
    selection = sources[SELECTION_PLAN.relpath].read_model(SELECTION_PLAN)
    node_edit = sources[NODE_EDIT_PLAN.relpath].read_model(NODE_EDIT_PLAN)
    edited_materials = sources[EDITED_MATERIALS.relpath].read_records(EDITED_MATERIALS)
    if node_edit.selection_plan_id != selection.plan_id:
        raise Conflict(
            "selection/node_edit mismatch in base run: "
            f"{selection.plan_id} vs {node_edit.selection_plan_id}"
        )
    optional_sources = resolve_artifact_sources(
        ctx.runs_root, base_run_id, (AUDIT_REPORT,), required=False
    )
    upstream_reports = read_stage_reports(
        optional_sources, {"ingestion", "planning", "selection", "node_edit"}
    )

    project = load_project_context(ctx, selection.project_id)
    lineage_effective = resolve_lineage_effective(ctx, base_run_id)
    output_language = str(
        lineage_effective.get("output_language") or project.output_language
    )
    effective_name_resolution_mode = (
        coerce_name_resolution_mode(name_resolution_mode)
        if name_resolution_mode is not None
        else coerce_name_resolution_mode(
            lineage_effective.get("name_resolution_mode")
            or project.name_resolution_mode
        )
    )
    control_prompt_locale = control_prompt_locale_from_record(
        lineage_effective.get("control_prompt_decision")
    )
    host = apply_host_patch(
        resolve_run_host(
            ctx,
            base_run_id,
            selection.persona_id,
            output_language=output_language,
        ),
        persona_patch,
    )
    config = apply_config_overrides(ctx.load_config(), config_overrides)
    name_routes = (
        [NAME_TABLE_ROUTE]
        if effective_name_resolution_mode is NameResolutionMode.name_table
        else []
    )
    preflight_prompts(
        config,
        [*name_routes, FACT_CONTEXT_ROUTE, RENDER_ROUTE],
        control_prompt_locale=control_prompt_locale,
    )
    try:
        effective_block_overrides = apply_render_block_overrides(
            {}, render_block_overrides
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    packed = pack(
        selection,
        node_edit,
        edited_materials,
        chunk_policy_from_budgets(config.stage_budgets.get("rendering", {})),
        candidate_graph=graph,
    )
    system_prompt, _ = load_system_prompt(
        RENDER_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    template, _ = load_user_prompt(
        RENDER_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    render_persona = coerce_persona(host)
    positions_by_chunk = derive_render_chunk_positions(packed.contracts)
    render_est_input = sum(
        estimate_tokens(system_prompt)
        + estimate_tokens(
            render_chunk_user_prompt(
                template=template,
                contract=contract,
                persona=render_persona,
                output_language=output_language,
                block_overrides=effective_block_overrides,
                chunk_index=positions_by_chunk[contract.chunk_id].chunk_index,
                chunk_count=positions_by_chunk[contract.chunk_id].chunk_count,
                segment_position=positions_by_chunk[contract.chunk_id].segment_position,
            )
        )
        for contract in packed.contracts
    )
    fact_context_calls = int(len(packed.contracts) > 1)
    context_est_input = 0
    if fact_context_calls:
        context_system, _ = load_system_prompt(
            FACT_CONTEXT_ROUTE, config, control_prompt_locale=control_prompt_locale
        )
        context_template, _ = load_user_prompt(
            FACT_CONTEXT_ROUTE, config, control_prompt_locale=control_prompt_locale
        )
        outline = project_fact_context_outline(packed.contracts)
        context_prompt = render_fact_context_user_prompt(
            context_template, outline, output_language
        )
        context_est_input = estimate_tokens(context_system) + estimate_tokens(
            context_prompt
        )
    name_estimate = estimate_name_resolution_inputs(
        packed.contracts,
        output_language=output_language,
        config=config,
        mode=effective_name_resolution_mode,
        control_prompt_locale=control_prompt_locale,
    )
    name_table_calls = name_estimate.table_calls
    if effective_name_resolution_mode is NameResolutionMode.name_table:
        table_profile, table_rate_source = resolve_cost_profile(
            config.route(NAME_TABLE_ROUTE).cost_profile, config
        )
        name_input_costs = {
            "name_table": estimate_cost(
                Usage(
                    input_tokens=name_estimate.table_input_tokens,
                    uncached_input_tokens=name_estimate.table_input_tokens,
                ),
                table_profile,
            ) if table_rate_source == "runtime_config" else None,
        }
    else:
        table_rate_source = "not_used"
        name_input_costs = {}
    initial_calls = len(packed.contracts) + fact_context_calls + name_table_calls
    input_tokens_by_stage = {
        "fact_context": context_est_input,
        "rendering_before_fact_context": render_est_input,
    }
    models_by_stage = {
        "fact_context": (
            config.route(FACT_CONTEXT_ROUTE).model if fact_context_calls else None
        ),
        "rendering": config.route(RENDER_ROUTE).model,
    }
    if effective_name_resolution_mode is NameResolutionMode.name_table:
        input_tokens_by_stage.update(
            {"name_table": name_estimate.table_input_tokens}
        )
        models_by_stage.update(
            {"name_table": config.route(NAME_TABLE_ROUTE).model}
        )
    estimate = {
        "calls": initial_calls,
        "max_calls_with_repair": initial_calls,
        "est_input_tokens": render_est_input + context_est_input + name_estimate.total_input_tokens,
        "est_input_tokens_by_stage": input_tokens_by_stage,
        "models_by_stage": models_by_stage,
        "configured_uncached_input_cost_by_stage": name_input_costs,
        "route": RENDER_ROUTE,
        "model": config.route(RENDER_ROUTE).model,
        "notes": [
            f"chunks={len(packed.contracts)} packing_reports={len(packed.reports)}",
            (
                f"name resolution mode={effective_name_resolution_mode.value}; "
                f"name table calls={name_table_calls}; selected materials="
                f"{name_estimate.material_count}; input≈"
                f"{name_estimate.projected_input_tokens} tokens; "
                f"over_budget={name_estimate.over_budget}"
            ),
            (
                "name-table configured input cost assumes no cache and excludes "
                f"output tokens; rate source={table_rate_source}"
            ),
            (
                f"fact-context calls={fact_context_calls}; sparse v3 drops malformed "
                "lines and does not issue semantic repair calls"
            ),
            (
                "全量声音块覆盖="
                + (
                    ", ".join(sorted(effective_block_overrides))
                    if effective_block_overrides
                    else "none"
                )
            ),
            "render 输入估算未计入尚未生成的 fact context 正文",
        ],
    }
    if not confirm:
        return {"confirm_required": True, "estimate": estimate}

    store = create_derived_store(ctx, base_run_id, "render", run_label)
    manifest_seed = base_manifest(
        kind="render",
        base_run_id=base_run_id,
        job_id=None,
        status="pending",
        request=request_payload(
            persona_patch,
            config_overrides,
            run_label,
            render_block_overrides=effective_block_overrides,
            output_language=None,
            name_resolution_mode=(
                coerce_name_resolution_mode(name_resolution_mode).value
                if name_resolution_mode is not None
                else None
            ),
        ),
        effective={
            "host": host.snapshot(),
            "output_language": output_language,
            "name_resolution_mode": effective_name_resolution_mode.value,
            "control_prompt_decision": lineage_effective.get("control_prompt_decision"),
            "routes": route_snapshot(
                config,
                [*name_routes, FACT_CONTEXT_ROUTE, RENDER_ROUTE],
            ),
            "stage_budgets": config.stage_budgets,
            "render_block_overrides": effective_block_overrides,
        },
        copied_artifacts=[],
        artifact_sources={
            relpath: source.run_id
            for relpath, source in {**sources, **optional_sources}.items()
        },
    )

    def target(job: Job) -> dict[str, Any]:
        tail = run_render_tail(
            graph=graph,
            selection=selection,
            node_edit_plan=node_edit,
            edited_materials=edited_materials,
            host=host,
            output_language=output_language,
            name_overrides=project.name_overrides,
            name_resolution_mode=effective_name_resolution_mode,
            config=config,
            store=store,
            call=progress_call(ctx, job, "render"),
            validation_context=ValidationContext(
                edited_materials=tuple(edited_materials),
                candidate_graph=graph,
                selection_plan=selection,
                node_edit_plan=node_edit,
            ),
            stage_reports=tuple(upstream_reports),
            block_overrides=effective_block_overrides,
            control_prompt_locale=control_prompt_locale,
        )
        return {
            "chunks": len(tail.chunks),
            "script_chars": len(tail.script),
            "audit_items": len(tail.validation.items),
            "calls": len(tail.call_records),
        }

    job = run_manifested_job(
        ctx,
        store=store,
        kind="render",
        base_run_id=base_run_id,
        manifest_seed=manifest_seed,
        target=target,
        progress_total=initial_calls,
    )
    return {"job_id": job.job_id, "run_id": store.run_id, "estimate": estimate}
