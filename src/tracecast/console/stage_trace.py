"""Stable facade for the lineage-aware reverse stage trace read model."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tracecast.domain import LlmCallRecord
from tracecast.storage import CHUNK_PLAN, sha256_json

from . import inventory
from .context import ConsoleContext
from .errors import BadRequest, NotFound
from .trace_common import TRACE_STAGES
from .trace_data import (
    artifacts,
    call_records,
    chunk_order,
    relevant_units,
    trace_stage,
)
from .trace_inputs import input_blocks
from .trace_outputs import compiled_output, packing_unit


def trace_index(
    ctx: ConsoleContext,
    run_id: str,
    *,
    chunk_id: str | None = None,
) -> dict[str, Any]:
    """List reverse stages and relevant work units for an optional chunk."""
    artifact_values = artifacts(ctx, run_id)
    records = call_records(ctx.runs_root, run_id)
    relevant = relevant_units(artifact_values, chunk_id=chunk_id)
    grouped: dict[tuple[str, str], list[tuple[LlmCallRecord, str]]] = defaultdict(list)
    chosen_origin: dict[tuple[str, str], str] = {}
    for record, origin in records:
        if record.work_unit is None:
            continue
        stage = trace_stage(record)
        key = (stage, record.work_unit.unit_id)
        if chunk_id is not None and record.work_unit.unit_id not in relevant.get(stage, set()):
            if record.work_unit.kind != "name_table":
                continue
        if key in chosen_origin and chosen_origin[key] != origin:
            continue
        chosen_origin.setdefault(key, origin)
        grouped[key].append((record, origin))

    stages: list[dict[str, Any]] = []
    for stage in TRACE_STAGES:
        units: list[dict[str, Any]] = []
        if stage == "packing":
            for unit_id in sorted(relevant.get(stage, set())):
                origin = artifact_values["origins"].get(CHUNK_PLAN.relpath)
                units.append(
                    {
                        "unit_id": unit_id,
                        "kind": "packing_chunk",
                        "order_index": chunk_order(artifact_values, unit_id),
                        "origin_run_id": origin,
                        "call_count": 0,
                    }
                )
        for (record_stage, unit_id), rows in grouped.items():
            if record_stage != stage:
                continue
            record = rows[-1][0]
            work = record.work_unit
            if work is None:
                continue
            units.append(
                {
                    "unit_id": unit_id,
                    "kind": work.kind,
                    "order_index": work.order_index,
                    "compression_round": work.compression_round,
                    "origin_run_id": rows[-1][1],
                    "call_count": len(rows),
                    "status": record.provider_status.value,
                }
            )
        units.sort(
            key=lambda item: (
                item.get("order_index") is None,
                item.get("order_index") or 0,
                item["unit_id"],
            )
        )
        stages.append({"stage": stage, "available": bool(units), "units": units})
    exact = bool(records) and all(record.work_unit is not None for record, _ in records)
    if not records:
        message = "此 run lineage 没有 LLM call ledger；无法做精确阶段追查。"
    elif exact:
        message = None
    else:
        message = (
            "此 run lineage 含有未记录 work-unit 的历史 call；已列出的 unit 可精确"
            "追查，其余阶段需用当前版本重新运行。"
        )
    return {
        "run_id": run_id,
        "chunk_id": chunk_id,
        "exact_trace_available": exact,
        "message": message,
        "stages": stages,
        "artifact_origins": artifact_values["origins"],
    }


def trace_unit(
    ctx: ConsoleContext,
    run_id: str,
    stage: str,
    unit_id: str,
) -> dict[str, Any]:
    """Return inspectable inputs, semantic compiled output and technical links."""
    normalized = stage.replace("-", "_").lower()
    if normalized not in TRACE_STAGES:
        raise BadRequest(f"unknown trace stage {stage!r}")
    artifact_values = artifacts(ctx, run_id)
    records = [
        (record, origin)
        for record, origin in call_records(ctx.runs_root, run_id)
        if record.work_unit is not None
        and trace_stage(record) == normalized
        and record.work_unit.unit_id == unit_id
    ]
    if normalized == "packing" and not records:
        return packing_unit(run_id, artifact_values, unit_id)
    if not records:
        raise NotFound(
            f"no exact {normalized} work unit {unit_id!r}; old runs must be rerun"
        )
    origin = records[0][1]
    records = [(record, item_origin) for record, item_origin in records if item_origin == origin]
    selected = next(
        (
            record
            for record, _item_origin in reversed(records)
            if record.provider_status.value == "success"
        ),
        records[-1][0],
    )
    raw_request = inventory.raw_payload(
        inventory.open_run(ctx.runs_root, origin), selected.call_id, "request"
    )
    request_data = raw_request.get("data") if raw_request.get("format") == "json" else None
    blocks, template_drift = input_blocks(
        ctx, selected, request_data, origin_run_id=origin
    )
    matches_hash = bool(
        isinstance(request_data, dict)
        and selected.input_hash == f"sha256:{sha256_json(request_data)}"
    )
    compiled, diagnostics = compiled_output(
        normalized, selected, artifact_values, unit_id
    )
    return {
        "run_id": run_id,
        "stage": normalized,
        "unit_id": unit_id,
        "kind": selected.work_unit.kind if selected.work_unit else None,
        "origin_run_id": origin,
        "input_blocks": blocks,
        "compiled_output": compiled,
        "diagnostics": diagnostics,
        "call_links": [
            {
                "call_id": record.call_id,
                "status": record.provider_status.value,
                "attempt": record.metadata.get("attempt"),
                "request_url": (
                    f"/api/runs/{run_id}/llm/calls/{record.call_id}/raw?role=request"
                ),
                "response_url": (
                    f"/api/runs/{run_id}/llm/calls/{record.call_id}/raw?role=response"
                ),
            }
            for record, _item_origin in records
        ],
        "technical": {
            "input_refs": selected.work_unit.input_refs if selected.work_unit else [],
            "compression_round": (
                selected.work_unit.compression_round if selected.work_unit else None
            ),
            "recorded_request_matches_hash": matches_hash,
            "prompt_template_drift": template_drift,
            "matches_recorded_request": matches_hash and not template_drift,
        },
    }
