"""Selection derived-run service."""

from __future__ import annotations

from typing import Any

from tracecast.control_prompt import control_prompt_locale_from_record
from tracecast.domain import estimate_tokens
from tracecast.planning import PersonaConfig, build_selection_plan, render_persona_block
from tracecast.planning.projection import (
    project_selector_directive_path,
    project_selector_graph,
    render_selector_directive_user_prompt,
    render_selector_user_prompt,
)
from tracecast.planning.vocab import (
    SELECTOR_DIRECTIVE_ROUTE,
    SELECTOR_ROUTE,
)
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.storage import AUDIT_REPORT, CANDIDATE_GRAPH, SELECTION_PLAN

from .context import ConsoleContext, load_project_context, resolve_lineage_effective
from .host_catalog import load_console_host
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


def derive_selection(
    ctx: ConsoleContext,
    *,
    base_run_id: str,
    host_id: str,
    persona_patch: PersonaPatch | None = None,
    config_overrides: ConfigOverrides | None = None,
    run_label: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Rerun Selection on a base run's graph with a selected, patched Host."""
    sources = resolve_artifact_sources(ctx.runs_root, base_run_id, (CANDIDATE_GRAPH,))
    report_sources = resolve_artifact_sources(
        ctx.runs_root, base_run_id, (AUDIT_REPORT,), required=False
    )
    upstream_reports = read_stage_reports(report_sources, {"ingestion", "planning"})
    graph = sources[CANDIDATE_GRAPH.relpath].read_model(CANDIDATE_GRAPH)
    project = load_project_context(ctx, graph.project_id)
    lineage_effective = resolve_lineage_effective(ctx, base_run_id)
    output_language = str(
        lineage_effective.get("output_language") or project.output_language
    )
    control_prompt_locale = control_prompt_locale_from_record(
        lineage_effective.get("control_prompt_decision")
    )
    host = apply_host_patch(
        load_console_host(ctx, host_id, output_language=output_language),
        persona_patch,
    )
    persona = host.selection_persona()
    persona_cfg = PersonaConfig.coerce(persona)
    config = apply_config_overrides(ctx.load_config(), config_overrides)
    selection_routes = [SELECTOR_ROUTE, SELECTOR_DIRECTIVE_ROUTE]
    preflight_prompts(
        config,
        selection_routes,
        control_prompt_locale=control_prompt_locale,
    )

    system_prompt, _ = load_system_prompt(
        SELECTOR_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    template, _ = load_user_prompt(
        SELECTOR_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    projection = project_selector_graph(graph.nodes, graph.edges)
    user_prompt = render_selector_user_prompt(
        template=template,
        projection=projection,
        persona_block=render_persona_block(persona_cfg),
    )
    directive_system, _ = load_system_prompt(
        SELECTOR_DIRECTIVE_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    directive_template, _ = load_user_prompt(
        SELECTOR_DIRECTIVE_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    directive_projection = project_selector_directive_path(
        graph.nodes,
        [(node.candidate_id, "core", "paraphrase") for node in graph.nodes],
    )
    directive_user = render_selector_directive_user_prompt(
        template=directive_template,
        projection=directive_projection,
        persona_block=render_persona_block(persona_cfg),
    )
    estimate = {
        "calls": 2,
        "est_input_tokens": (
            estimate_tokens(system_prompt)
            + estimate_tokens(user_prompt)
            + estimate_tokens(directive_system)
            + estimate_tokens(directive_user)
        ),
        "route": f"{SELECTOR_ROUTE}+{SELECTOR_DIRECTIVE_ROUTE}",
        "model": (
            f"{config.route(SELECTOR_ROUTE).model}"
            f"+{config.route(SELECTOR_DIRECTIVE_ROUTE).model}"
        ),
        "notes": [
            f"graph nodes={len(graph.nodes)} edges={len(graph.edges)}",
            "DIR estimate uses the full graph as an input-token upper bound",
        ],
    }
    if not confirm:
        return {"confirm_required": True, "estimate": estimate}

    store = create_derived_store(ctx, base_run_id, "sel", run_label)
    manifest_seed = base_manifest(
        kind="selection",
        base_run_id=base_run_id,
        job_id=None,
        status="pending",
        request=request_payload(
            persona_patch, config_overrides, run_label, host_id=host_id
        ),
        effective={
            "host": host.snapshot(),
            "output_language": output_language,
            "name_resolution_mode": (
                lineage_effective.get("name_resolution_mode")
                or project.name_resolution_mode.value
            ),
            "control_prompt_decision": lineage_effective.get("control_prompt_decision"),
            "routes": route_snapshot(config, selection_routes),
            "stage_budgets": config.stage_budgets,
        },
        copied_artifacts=[],
        artifact_sources={
            relpath: source.run_id
            for relpath, source in {**sources, **report_sources}.items()
        },
    )

    def target(job: Job) -> dict[str, Any]:
        output = build_selection_plan(
            graph,
            persona=persona_cfg,
            config=config,
            store=store,
            call=progress_call(ctx, job, "selection"),
            control_prompt_locale=control_prompt_locale,
        )
        plan = output.plan
        store.write_model(SELECTION_PLAN, plan)
        store.write_records(AUDIT_REPORT, [*upstream_reports, *output.reports])
        return {
            "plan_id": plan.plan_id,
            "selected": len(plan.selected_sequence),
            "diagnostics": len(output.reports),
        }

    job = run_manifested_job(
        ctx,
        store=store,
        kind="selection",
        base_run_id=base_run_id,
        manifest_seed=manifest_seed,
        target=target,
        progress_total=len(selection_routes),
    )
    return {"job_id": job.job_id, "run_id": store.run_id, "estimate": estimate}
