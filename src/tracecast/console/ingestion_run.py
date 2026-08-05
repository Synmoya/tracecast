"""Fresh project -> Ingestion-only or full root runs for the local console.

Unlike derives, an ingestion run has no base run: it resolves the project's
declared sources, performs deterministic slicing/windowing for a zero-spend
estimate, then runs the selected scope in a background job after explicit
confirmation. Full mode hands the same resolved project/config to the canonical
pipeline driver, so the console does not maintain a second orchestration
implementation.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from tracecast.domain import estimate_tokens
from tracecast.hosts import HostProfile
from tracecast.ingestion import (
    OPERATION_ROUTE,
    IngestionBudgets,
    SourceInput,
    build_manifest,
    build_source_inputs,
    ingest_sources,
    pack_windows,
    persist_ingest_result,
    project_window,
    split_source,
)
from tracecast.language import require_bcp47_language_tag
from tracecast.llm import resolve_cost_profile
from tracecast.name_resolution import (
    NAME_TABLE_ROUTE,
    NameResolutionMode,
)
from tracecast.node_edit.vocab import NODE_EDIT_REPAIR_ROUTE, NODE_EDIT_ROUTE
from tracecast.pipeline import resolve_pipeline_control_prompt, run_pipeline
from tracecast.planning.vocab import (
    COMPRESS_ROUTE,
    PLANNER_ROUTE,
    SELECTOR_DIRECTIVE_ROUTE,
    SELECTOR_ROUTE,
)
from tracecast.project import ProjectConfig, load_project
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.rendering import (
    FACT_CONTEXT_ROUTE,
    RENDER_ROUTE,
)
from tracecast.storage import AUDIT_REPORT, RunStore, RuntimeConfig

from .context import ConsoleContext
from .errors import BadRequest, Conflict, ConsoleError
from .host_catalog import load_console_host
from .inventory import open_project_dir
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
    derived_run_id,
    progress_call,
    run_manifested_job,
)


@dataclass(frozen=True)
class _PreparedIngestion:
    """Resolved inputs shared by dry-run estimation and confirmed execution."""

    project: ProjectConfig
    host: HostProfile | None
    project_dir: Path
    config: RuntimeConfig
    budgets: IngestionBudgets
    inputs: list[SourceInput]
    source_stats: list[dict[str, Any]]
    estimate: dict[str, Any]
    output_language: str
    name_resolution_mode: NameResolutionMode
    control_prompt_decision: Any
    selection_human_directive: list[str] | None


RunMode = Literal["ingestion", "full"]
FULL_PIPELINE_ROUTES = (
    OPERATION_ROUTE,
    PLANNER_ROUTE,
    COMPRESS_ROUTE,
    SELECTOR_ROUTE,
    SELECTOR_DIRECTIVE_ROUTE,
    NODE_EDIT_ROUTE,
    NODE_EDIT_REPAIR_ROUTE,
    NAME_TABLE_ROUTE,
    FACT_CONTEXT_ROUTE,
    RENDER_ROUTE,
)


def _routes_for(mode: RunMode, name_resolution_mode: NameResolutionMode) -> tuple[str, ...]:
    if mode != "full":
        return (OPERATION_ROUTE,)
    if name_resolution_mode is NameResolutionMode.name_table:
        return FULL_PIPELINE_ROUTES
    return tuple(
        route
        for route in FULL_PIPELINE_ROUTES
        if route != NAME_TABLE_ROUTE
    )


def create_ingestion_run(
    ctx: ConsoleContext,
    *,
    project_id: str,
    host_id: str | None = None,
    source_ids: list[str] | None = None,
    output_language: str | None = None,
    name_resolution_mode: NameResolutionMode | str | None = None,
    target_output_kchars: float | None = None,
    selection_human_directive: list[str] | None = None,
    persona_patch: PersonaPatch | None = None,
    config_overrides: ConfigOverrides | None = None,
    run_label: str | None = None,
    mode: RunMode = "ingestion",
    confirm: bool = False,
) -> dict[str, Any]:
    """Estimate or create a fresh root run for ``project_id``."""
    explicit_name_resolution_mode = (
        NameResolutionMode(name_resolution_mode)
        if name_resolution_mode is not None
        else None
    )
    cleaned_directives = (
        [value.strip() for value in selection_human_directive]
        if selection_human_directive is not None
        else None
    )
    if cleaned_directives is not None and any(not value for value in cleaned_directives):
        raise BadRequest("selection_human_directive entries must be non-empty")
    if mode != "full" and persona_patch is not None and not persona_patch.is_empty():
        raise BadRequest("persona_patch only applies to mode=full runs")
    legacy_directives, host_persona_patch = _split_selection_directives(persona_patch)
    if cleaned_directives is not None and legacy_directives is not None:
        raise BadRequest(
            "selection_human_directive cannot be combined with the legacy "
            "persona_patch.viewpoint_profile.human_directive field"
        )
    effective_directives = (
        cleaned_directives if cleaned_directives is not None else legacy_directives
    )
    prepared = _prepare(
        ctx,
        project_id=project_id,
        host_id=host_id,
        source_ids=source_ids,
        output_language=output_language,
        name_resolution_mode=explicit_name_resolution_mode,
        target_output_kchars=target_output_kchars,
        selection_human_directive=effective_directives,
        persona_patch=host_persona_patch,
        config_overrides=config_overrides,
        mode=mode,
    )
    if not confirm:
        return {"confirm_required": True, "estimate": prepared.estimate}

    store = _create_root_store(ctx, project_id, run_label, mode)
    request = {
        "project_id": project_id,
        "host_id": host_id,
        "source_ids": source_ids,
        "persona_patch": (
            persona_patch.model_dump(exclude_none=True) if persona_patch else {}
        ),
        "config_overrides": (
            config_overrides.model_dump(exclude_none=True) if config_overrides else {}
        ),
        "run_label": run_label,
        "mode": mode,
        "output_language": output_language,
        "name_resolution_mode": (
            explicit_name_resolution_mode.value
            if explicit_name_resolution_mode is not None
            else None
        ),
        "target_output_kchars": target_output_kchars,
        "selection_human_directive": cleaned_directives,
    }
    routes = _routes_for(mode, prepared.name_resolution_mode)
    manifest_seed = base_manifest(
        kind=mode,
        base_run_id=None,
        job_id=None,
        status="pending",
        request=request,
        effective={
            "project_id": prepared.project.project_id,
            "output_language": prepared.output_language,
            "name_resolution_mode": prepared.name_resolution_mode.value,
            "selection_human_directive": prepared.selection_human_directive,
            "control_prompt_decision": prepared.control_prompt_decision.model_dump(mode="json"),
            "source_ids": [source.snapshot.source_id for source in prepared.inputs],
            "host": prepared.host.snapshot() if prepared.host is not None else None,
            "source_profiles": {
                source.snapshot.source_id: {
                    "allowed_content_kinds": sorted(
                        kind.value for kind in source.profile.allowed_content_kinds
                    ),
                    "allowed_tags": sorted(source.profile.allowed_tags),
                }
                for source in prepared.inputs
            },
            "source_stats": prepared.source_stats,
            "routes": route_snapshot(prepared.config, list(routes)),
            "stage_budgets": prepared.config.stage_budgets,
        },
        copied_artifacts=[],
    )

    def target(job: Job) -> dict[str, Any]:
        if mode == "full":
            assert prepared.host is not None
            result = run_pipeline(
                prepared.project,
                prepared.project_dir,
                host=prepared.host,
                config=prepared.config,
                store=store,
                call=progress_call(ctx, job, "pipeline"),
                output_language=prepared.output_language,
                name_resolution_mode=prepared.name_resolution_mode,
                selection_human_directive=prepared.selection_human_directive,
                control_prompt_decision=prepared.control_prompt_decision,
            )
            return {"mode": mode, **dataclasses.asdict(result)}
        result = ingest_sources(
            prepared.inputs,
            config=prepared.config,
            store=store,
            budgets=prepared.budgets,
            call=progress_call(ctx, job, "ingestion"),
            control_prompt_locale=prepared.control_prompt_decision.variant,
        )
        persist_ingest_result(store, result)
        store.write_records(AUDIT_REPORT, result.reports)
        errors = sum(item.level == "error" for item in result.reports)
        warnings = sum(item.level == "warning" for item in result.reports)
        return {
            "mode": mode,
            "sources": len(result.snapshots),
            "pieces": len(result.pieces),
            "windows": len(result.windows),
            "atoms": len(result.atoms),
            "calls": len(result.call_records),
            "report_items": len(result.reports),
            "errors": errors,
            "warnings": warnings,
            "ok": result.ok,
        }

    job = run_manifested_job(
        ctx,
        store=store,
        kind=mode,
        base_run_id=None,
        manifest_seed=manifest_seed,
        target=target,
        progress_total=(prepared.estimate["calls"] if mode == "ingestion" else None),
    )
    return {"job_id": job.job_id, "run_id": store.run_id, "estimate": prepared.estimate}


def _split_selection_directives(
    persona_patch: PersonaPatch | None,
) -> tuple[list[str] | None, PersonaPatch | None]:
    """Keep the legacy directive field Selection-only.

    Older Console clients placed per-run path preferences inside the transient
    Host patch.  Preserve that request shape, but remove the field before Host
    resolution so it cannot become Renderer-facing persona state.
    """

    if persona_patch is None or persona_patch.viewpoint_profile is None:
        return None, persona_patch
    legacy = persona_patch.viewpoint_profile.human_directive
    if legacy is None:
        return None, persona_patch
    cleaned = [value.strip() for value in legacy]
    if any(not value for value in cleaned):
        raise BadRequest(
            "persona_patch.viewpoint_profile.human_directive entries must be non-empty"
        )
    patch_data = persona_patch.model_dump(exclude_none=True)
    profile = dict(patch_data.get("viewpoint_profile") or {})
    profile.pop("human_directive", None)
    if profile:
        patch_data["viewpoint_profile"] = profile
    else:
        patch_data.pop("viewpoint_profile", None)
    host_patch = PersonaPatch.model_validate(patch_data) if patch_data else None
    return cleaned, host_patch


def _prepare(
    ctx: ConsoleContext,
    *,
    project_id: str,
    host_id: str | None,
    config_overrides: ConfigOverrides | None,
    mode: RunMode,
    source_ids: list[str] | None = None,
    output_language: str | None = None,
    name_resolution_mode: NameResolutionMode | None = None,
    target_output_kchars: float | None = None,
    selection_human_directive: list[str] | None = None,
    persona_patch: PersonaPatch | None = None,
) -> _PreparedIngestion:
    """Resolve project/config and build every initial window prompt without writes."""
    project_dir = open_project_dir(ctx.projects_dir, project_id)
    try:
        project = load_project(project_dir, ctx.profiles_path)
        project = _subset_sources(project, source_ids)
        if mode != "full" and output_language is not None:
            raise BadRequest("output_language override only applies to mode=full runs")
        if mode != "full" and name_resolution_mode is not None:
            raise BadRequest("name_resolution_mode only applies to mode=full runs")
        if mode != "full" and target_output_kchars is not None:
            raise BadRequest("target_output_kchars only applies to mode=full runs")
        if mode != "full" and selection_human_directive is not None:
            raise BadRequest("selection_human_directive only applies to mode=full runs")
        effective_output_language = require_bcp47_language_tag(
            output_language if output_language is not None else project.output_language,
            field="output_language",
        )
        if effective_output_language.casefold() in {"unknown", "und"}:
            raise BadRequest("output_language must identify a concrete target language")
        effective_name_resolution_mode = (
            NameResolutionMode(name_resolution_mode)
            if name_resolution_mode is not None
            else project.name_resolution_mode
        )
        host = _resolve_host(ctx, host_id, effective_output_language, persona_patch, mode)
        config = apply_config_overrides(ctx.load_config(), config_overrides)
        if target_output_kchars is not None:
            if isinstance(target_output_kchars, bool) or target_output_kchars <= 0:
                raise BadRequest("target_output_kchars must be positive")
            config_data = config.model_dump()
            config_data.setdefault("stage_budgets", {}).setdefault("selection", {})[
                "target_output_kchars"
            ] = target_output_kchars
            config = RuntimeConfig.model_validate(config_data)
        budgets = IngestionBudgets.from_config(config)
        inputs = build_source_inputs(project, project_dir)
        control_prompt_decision = resolve_pipeline_control_prompt(inputs)
        routes = _routes_for(mode, effective_name_resolution_mode)
        preflight_prompts(
            config,
            list(routes),
            control_prompt_locale=control_prompt_decision.variant,
        )
        system_prompt, _system_version = load_system_prompt(
            OPERATION_ROUTE,
            config,
            control_prompt_locale=control_prompt_decision.variant,
        )
        user_template, _user_version = load_user_prompt(
            OPERATION_ROUTE,
            config,
            control_prompt_locale=control_prompt_decision.variant,
        )
    except ConsoleError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot prepare ingestion run for project {project_id!r}: {exc}") from exc

    if not inputs:
        raise BadRequest(f"project {project_id!r} declares no sources")

    source_stats: list[dict[str, Any]] = []
    total_pieces = 0
    total_windows = 0
    total_input_tokens = 0
    for source in inputs:
        pieces = split_source(
            source.snapshot.source_id,
            source.raw_text,
            budgets.source_piece_max_tokens,
        )
        windows = pack_windows(
            source.snapshot.source_id,
            pieces,
            max_input_tokens=budgets.atom_window_max_input_tokens,
            pieces_per_call_soft_limit=budgets.pieces_per_call_soft_limit,
        )
        piece_by_id = {piece.piece_id: piece for piece in pieces}
        source_input_tokens = 0
        for window in windows:
            messages = project_window(
                manifest=build_manifest(window.piece_ids),
                piece_by_id=piece_by_id,
                profile=source.profile,
                system_prompt=system_prompt,
                user_prompt_template=user_template,
            )
            source_input_tokens += sum(
                estimate_tokens(message["content"]) for message in messages
            )
        source_stats.append(
            {
                "source_id": source.snapshot.source_id,
                "chars": len(source.raw_text),
                "pieces": len(pieces),
                "windows": len(windows),
                "est_input_tokens": source_input_tokens,
            }
        )
        total_pieces += len(pieces)
        total_windows += len(windows)
        total_input_tokens += source_input_tokens

    repair_enabled = bool(config.route(OPERATION_ROUTE).repair_prompt)
    estimate = {
        "calls": total_windows,
        "max_calls_with_repair": total_windows * (2 if repair_enabled else 1),
        "est_input_tokens": total_input_tokens,
        "route": OPERATION_ROUTE,
        "model": config.route(OPERATION_ROUTE).model,
        "sources": len(inputs),
        "source_chars": sum(len(source.raw_text) for source in inputs),
        "pieces": total_pieces,
        "windows": total_windows,
        "source_stats": source_stats,
        "notes": [
            "calls/est_input_tokens count initial window calls only",
            (
                "a failed window may add one localized repair call"
                if repair_enabled
                else "localized repair is disabled for this route"
            ),
        ],
    }
    unconfigured_rate_routes = [
        route
        for route in routes
        if resolve_cost_profile(config.route(route).cost_profile, config)[1]
        != "runtime_config"
    ]
    estimate.update(
        {
            "rate_status": (
                "configured" if not unconfigured_rate_routes else "unconfigured"
            ),
            "unconfigured_rate_routes": unconfigured_rate_routes,
        }
    )
    if mode == "full":
        estimate.update(
            {
                "mode": "full",
                "ingestion_initial_calls": total_windows,
                "calls": None,
                "models": {
                    route: config.route(route).model for route in routes
                },
            }
        )
        estimate["notes"].extend(
            [
                "下游 call 数与 token 量取决于 ingestion/planning 的动态产物，无法在调用前精确计算",
                "est_input_tokens 仅统计初始 ingestion window；"
                "完整 run 会继续 Planning 到 Assembly",
            ]
        )
    return _PreparedIngestion(
        project=project,
        host=host,
        project_dir=project_dir,
        config=config,
        budgets=budgets,
        inputs=inputs,
        source_stats=source_stats,
        estimate=estimate,
        output_language=effective_output_language,
        name_resolution_mode=effective_name_resolution_mode,
        control_prompt_decision=control_prompt_decision,
        selection_human_directive=selection_human_directive,
    )


def _subset_sources(project: ProjectConfig, source_ids: list[str] | None) -> ProjectConfig:
    """Restrict the run to a subset of declared sources, keeping ``sources.json`` order."""
    if source_ids is None:
        return project
    if not source_ids:
        raise BadRequest("source_ids must be non-empty; omit it to run every source")
    duplicates = sorted({sid for sid in source_ids if source_ids.count(sid) > 1})
    if duplicates:
        raise BadRequest(f"duplicate source_ids: {', '.join(duplicates)}")
    known = {source.source_id for source in project.sources}
    unknown = sorted(set(source_ids) - known)
    if unknown:
        raise BadRequest(f"unknown source_ids: {', '.join(unknown)}")
    wanted = set(source_ids)
    return dataclasses.replace(
        project,
        sources=[source for source in project.sources if source.source_id in wanted],
    )


def _resolve_host(
    ctx: ConsoleContext,
    host_id: str | None,
    output_language: str,
    persona_patch: PersonaPatch | None,
    mode: RunMode,
) -> HostProfile | None:
    """Resolve and transiently patch a Host only for full root runs."""

    if mode != "full":
        if host_id is not None:
            raise BadRequest("host_id only applies to mode=full runs")
        if persona_patch is not None and not persona_patch.is_empty():
            raise BadRequest("persona_patch only applies to mode=full runs")
        return None
    if not host_id:
        raise BadRequest("host_id is required for mode=full runs")
    host = load_console_host(ctx, host_id, output_language=output_language)
    return apply_host_patch(host, persona_patch)


def _create_root_store(
    ctx: ConsoleContext, project_id: str, run_label: str | None, mode: RunMode
) -> RunStore:
    run_id = derived_run_id(project_id, mode, run_label)
    try:
        return RunStore.create_for_run(ctx.runs_root, run_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    except FileExistsError:
        raise Conflict(f"run dir {run_id!r} already exists") from None
