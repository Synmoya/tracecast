"""Single-call Chunk Lab rendering into a fresh derived run."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tracecast.domain import ChunkArtifact, LlmWorkUnitRef, estimate_tokens
from tracecast.rendering import RENDER_ROUTE, substitute_render_blocks
from tracecast.storage import (
    AUDIT_REPORT,
    CHUNK_ARTIFACTS,
    LLM_CALL_RECORDS,
    NAME_RESOLUTION_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    content_hash,
    json_io,
)
from tracecast.validation import ValidationContext, run_validation

from .chunklab_baseline import CONTRACT_SNAPSHOT_RELPATH, load_chunk_baseline
from .context import ConsoleContext, resolve_lineage_effective
from .errors import BadRequest
from .inventory import open_run, resolve_artifact_sources
from .jobs import Job
from .overrides import (
    ConfigOverrides,
    apply_config_overrides,
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


def chunklab_render(
    ctx: ConsoleContext,
    *,
    base_run_id: str,
    chunk_id: str,
    blocks: dict[str, str],
    config_overrides: ConfigOverrides | None = None,
    run_label: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Re-render one chunk from (possibly edited) injection blocks; one call."""
    base = open_run(ctx.runs_root, base_run_id)
    config = apply_config_overrides(ctx.load_config(), config_overrides)
    baseline = load_chunk_baseline(ctx, base, chunk_id, config)
    lineage_effective = resolve_lineage_effective(ctx, base_run_id)
    try:
        user_prompt = substitute_render_blocks(baseline.template, blocks)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    edited_keys = sorted(key for key in blocks if blocks[key] != baseline.blocks.get(key))
    estimate = {
        "calls": 1,
        "est_input_tokens": (
            estimate_tokens(baseline.system_prompt) + estimate_tokens(user_prompt)
        ),
        "route": RENDER_ROUTE,
        "model": config.route(RENDER_ROUTE).model,
        "notes": [],
    }
    if not confirm:
        return {"confirm_required": True, "estimate": estimate, "edited_keys": edited_keys}

    contract = baseline.contract
    report_sources = resolve_artifact_sources(
        ctx.runs_root, base_run_id, (AUDIT_REPORT,), required=False
    )
    upstream_reports = read_stage_reports(
        report_sources,
        {
            "ingestion",
            "planning",
            "selection",
            "node_edit",
            "packing",
            "name_resolution",
        },
    )
    store = create_derived_store(ctx, base_run_id, "chunklab", run_label)
    manifest_seed = base_manifest(
        kind="chunklab",
        base_run_id=base_run_id,
        job_id=None,
        status="pending",
        request=request_payload(
            None,
            config_overrides,
            run_label,
            chunk_id=chunk_id,
            edited_blocks={key: blocks[key] for key in edited_keys},
            injection_blocks=blocks,
            output_language=baseline.output_language,
            name_resolution_plan_id=baseline.name_resolution.plan_id,
            chunk_index=baseline.chunk_position.chunk_index,
            chunk_count=baseline.chunk_position.chunk_count,
            segment_position=baseline.chunk_position.segment_position,
        ),
        effective={
            "host": baseline.host.snapshot(),
            "output_language": baseline.output_language,
            "name_resolution_mode": lineage_effective.get("name_resolution_mode"),
            "control_prompt_decision": lineage_effective.get("control_prompt_decision"),
            "name_resolution_plan_id": baseline.name_resolution.plan_id,
            "chunk_index": baseline.chunk_position.chunk_index,
            "chunk_count": baseline.chunk_position.chunk_count,
            "segment_position": baseline.chunk_position.segment_position,
            "routes": route_snapshot(config, [RENDER_ROUTE]),
            "stage_budgets": config.stage_budgets,
        },
        copied_artifacts=[
            RENDER_CONTRACTS.relpath,
            NAME_RESOLUTION_PLAN.relpath,
            *(
                [RENDER_FACT_CONTEXTS.relpath]
                if baseline.fact_context is not None
                else []
            ),
            CONTRACT_SNAPSHOT_RELPATH,
        ],
        artifact_sources={
            **baseline.artifact_sources,
            **{relpath: source.run_id for relpath, source in report_sources.items()},
        },
    )

    def target(job: Job) -> dict[str, Any]:
        store.write_records(RENDER_CONTRACTS, baseline.contracts)
        store.write_model(NAME_RESOLUTION_PLAN, baseline.name_resolution)
        if baseline.fact_context is not None:
            store.write_records(RENDER_FACT_CONTEXTS, [baseline.fact_context])
        json_io.write_json(
            store.path_for(CONTRACT_SNAPSHOT_RELPATH), contract.model_dump(mode="json")
        )
        call = progress_call(ctx, job, "chunklab")
        text, record = call(
            operation_route=RENDER_ROUTE,
            messages=[
                {"role": "system", "content": baseline.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            config=config,
            store=store,
            prompt_version=baseline.prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="render_chunk",
                unit_id=contract.chunk_id,
                order_index=contract.order_index,
                input_refs=[
                    *[node.source_node_id for node in contract.nodes],
                    *[
                        material.source_material_id for material in contract.materials
                    ],
                ],
                metadata={
                    "console_chunklab": True,
                    "name_resolution_plan_id": baseline.name_resolution.plan_id,
                    "output_language": baseline.output_language,
                    "chunk_index": baseline.chunk_position.chunk_index,
                    "chunk_count": baseline.chunk_position.chunk_count,
                    "segment_position": baseline.chunk_position.segment_position,
                },
            ),
        )
        artifact = ChunkArtifact(
            chunk_id=contract.chunk_id,
            order_index=contract.order_index,
            contract_id=contract.contract_id,
            text=text,
            input_hash=content_hash(user_prompt),
            output_hash=content_hash(text),
            generated_at=datetime.now(tz=UTC),
            metadata={
                "persona_id": baseline.persona.persona_id,
                "prompt_version": baseline.prompt_version,
                "call_id": record.call_id,
                "console_chunklab": True,
                "base_run_id": base_run_id,
                "edited_blocks": edited_keys,
                "name_resolution_plan_id": baseline.name_resolution.plan_id,
                "output_language": baseline.output_language,
                "chunk_index": baseline.chunk_position.chunk_index,
                "chunk_count": baseline.chunk_position.chunk_count,
                "segment_position": baseline.chunk_position.segment_position,
            },
        )
        store.write_records(CHUNK_ARTIFACTS, [artifact])
        call_records = (
            store.read_records(LLM_CALL_RECORDS) if store.exists(LLM_CALL_RECORDS) else []
        )
        report = run_validation(
            ValidationContext(
                contracts=(contract,),
                chunks=(artifact,),
                call_records=tuple(call_records),
                stage_reports=tuple(upstream_reports),
                raw_chunk_text={artifact.chunk_id: text},
            )
        )
        store.write_records(AUDIT_REPORT, report.items)
        return {
            "chunk_id": artifact.chunk_id,
            "call_id": record.call_id,
            "chars": len(text),
            "audit_items": len(report.items),
            "text": text,
        }

    job = run_manifested_job(
        ctx,
        store=store,
        kind="chunklab",
        base_run_id=base_run_id,
        manifest_seed=manifest_seed,
        target=target,
        progress_total=1,
    )
    return {
        "job_id": job.job_id,
        "run_id": store.run_id,
        "estimate": estimate,
        "edited_keys": edited_keys,
    }
