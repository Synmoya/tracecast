"""Node Edit derived-run service."""

from __future__ import annotations

from typing import Any

from tracecast.control_prompt import control_prompt_locale_from_record
from tracecast.domain import estimate_tokens
from tracecast.node_edit import build_node_edit_outputs
from tracecast.node_edit.vocab import NODE_EDIT_REPAIR_ROUTE, NODE_EDIT_ROUTE
from tracecast.storage import (
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    EDITED_MATERIALS,
    MATERIAL_ATOMS,
    NODE_EDIT_PLAN,
    SELECTION_PLAN,
)

from .context import ConsoleContext, load_project_context, resolve_lineage_effective
from .inventory import resolve_artifact_sources
from .jobs import Job
from .overrides import (
    ConfigOverrides,
    apply_config_overrides,
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


def derive_node_edit(
    ctx: ConsoleContext,
    *,
    base_run_id: str,
    config_overrides: ConfigOverrides | None = None,
    run_label: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Rerun Node Edit against a base run's graph + selection plan + atoms."""
    sources = resolve_artifact_sources(
        ctx.runs_root,
        base_run_id,
        (CANDIDATE_GRAPH, SELECTION_PLAN, MATERIAL_ATOMS),
    )
    report_sources = resolve_artifact_sources(
        ctx.runs_root, base_run_id, (AUDIT_REPORT,), required=False
    )
    upstream_reports = read_stage_reports(
        report_sources, {"ingestion", "planning", "selection"}
    )
    graph = sources[CANDIDATE_GRAPH.relpath].read_model(CANDIDATE_GRAPH)
    selection = sources[SELECTION_PLAN.relpath].read_model(SELECTION_PLAN)
    atoms = sources[MATERIAL_ATOMS.relpath].read_records(MATERIAL_ATOMS)
    project = load_project_context(ctx, graph.project_id)
    lineage_effective = resolve_lineage_effective(ctx, base_run_id)
    control_prompt_decision = lineage_effective.get("control_prompt_decision") or {
        "source_language": "zh",
        "variant": "zh",
        "policy_version": "legacy-console-default@v1",
    }
    control_prompt_locale = control_prompt_locale_from_record(control_prompt_decision)
    config = apply_config_overrides(ctx.load_config(), config_overrides)
    preflight_prompts(
        config,
        [NODE_EDIT_ROUTE, NODE_EDIT_REPAIR_ROUTE],
        control_prompt_locale=control_prompt_locale,
    )

    accepted = set(selection.selected_sequence)
    accepted_material_ids = {
        material_id
        for node in graph.nodes
        if node.candidate_id in accepted
        for material_id in node.material_ids
    }
    pool_tokens = sum(
        estimate_tokens(atom.content)
        for atom in atoms
        if atom.id in accepted_material_ids
    )
    estimate = {
        "calls": None,
        "est_input_tokens": pool_tokens,
        "route": NODE_EDIT_ROUTE,
        "model": config.route(NODE_EDIT_ROUTE).model,
        "notes": [f"accepted nodes={len(accepted)}; call count depends on batching"],
    }
    if not confirm:
        return {"confirm_required": True, "estimate": estimate}

    store = create_derived_store(ctx, base_run_id, "nodeedit", run_label)
    manifest_seed = base_manifest(
        kind="node_edit",
        base_run_id=base_run_id,
        job_id=None,
        status="pending",
        request=request_payload(None, config_overrides, run_label),
        effective={
            "persona": None,
            "output_language": (
                lineage_effective.get("output_language") or project.output_language
            ),
            "name_resolution_mode": (
                lineage_effective.get("name_resolution_mode")
                or project.name_resolution_mode.value
            ),
            "control_prompt_decision": control_prompt_decision,
            "routes": route_snapshot(config, [NODE_EDIT_ROUTE, NODE_EDIT_REPAIR_ROUTE]),
            "stage_budgets": config.stage_budgets,
        },
        copied_artifacts=[],
        artifact_sources={
            relpath: source.run_id
            for relpath, source in {**sources, **report_sources}.items()
        },
    )

    def target(job: Job) -> dict[str, Any]:
        output = build_node_edit_outputs(
            selection,
            graph,
            atoms,
            config=config,
            store=store,
            call=progress_call(ctx, job, "node_edit"),
            control_prompt_locale=control_prompt_locale,
        )
        store.write_model(NODE_EDIT_PLAN, output.plan)
        store.write_records(EDITED_MATERIALS, output.edited_materials)
        store.write_records(AUDIT_REPORT, [*upstream_reports, *output.stage_reports])
        return {
            "plan_id": output.plan.plan_id,
            "node_edits": len(output.plan.node_edits),
            "edited_materials": len(output.edited_materials),
        }

    job = run_manifested_job(
        ctx,
        store=store,
        kind="node_edit",
        base_run_id=base_run_id,
        manifest_seed=manifest_seed,
        target=target,
        progress_total=None,
    )
    return {"job_id": job.job_id, "run_id": store.run_id, "estimate": estimate}
