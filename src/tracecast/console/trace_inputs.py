"""Recorded prompt block reconstruction for Console stage trace."""

from __future__ import annotations

from typing import Any

from tracecast.control_prompt import control_prompt_locale_from_record
from tracecast.domain import LlmCallRecord
from tracecast.prompts import extract_prompt_blocks, load_route_prompt, load_user_prompt
from tracecast.storage import RuntimeConfig

from . import inventory
from .context import ConsoleContext, resolve_lineage_effective
from .errors import BadRequest
from .overrides import ConfigOverrides, apply_config_overrides


def input_blocks(
    ctx: ConsoleContext,
    record: LlmCallRecord,
    request: dict[str, Any] | None,
    *,
    origin_run_id: str,
) -> tuple[dict[str, str], bool]:
    if not isinstance(request, dict):
        return {"recorded_request": "(raw request is unavailable)"}, True
    messages = request.get("messages")
    if not isinstance(messages, list):
        return {"recorded_request": str(request)}, True
    user_messages = [
        str(message.get("content", ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    snapshot = _chunklab_input_snapshot(ctx, origin_run_id, record)
    if snapshot is not None:
        return snapshot, False
    try:
        config = _origin_config(ctx, origin_run_id)
        effective = resolve_lineage_effective(ctx, origin_run_id)
        control_prompt_locale = control_prompt_locale_from_record(
            effective.get("control_prompt_decision")
        )
        template, _version = load_user_prompt(
            record.operation_route,
            config,
            control_prompt_locale=control_prompt_locale,
        )
        blocks = extract_prompt_blocks(template, user_messages[0])
        if len(user_messages) > 1:
            repair_template, _repair_version = load_route_prompt(
                record.operation_route,
                config,
                "repair_prompt",
                control_prompt_locale=control_prompt_locale,
            )
            repair = extract_prompt_blocks(repair_template, user_messages[-1])
            blocks.update({f"repair.{key}": value for key, value in repair.items()})
        return blocks, False
    except (BadRequest, OSError, KeyError, ValueError, IndexError):
        return {
            f"user_message_{index + 1}": content
            for index, content in enumerate(user_messages)
        }, True


def _chunklab_input_snapshot(
    ctx: ConsoleContext,
    origin_run_id: str,
    record: LlmCallRecord,
) -> dict[str, str] | None:
    work = record.work_unit
    if work is None or work.kind != "render_chunk":
        return None
    manifest = inventory.read_console_manifest(
        inventory.open_run(ctx.runs_root, origin_run_id)
    ) or {}
    request = manifest.get("request")
    if (
        manifest.get("kind") != "chunklab"
        or not isinstance(request, dict)
        or request.get("chunk_id") != work.unit_id
    ):
        return None
    snapshot = request.get("injection_blocks")
    if not isinstance(snapshot, dict):
        return None
    return {str(key): str(value) for key, value in snapshot.items()}


def _origin_config(ctx: ConsoleContext, origin_run_id: str) -> RuntimeConfig:
    """Rebuild the request-scoped config recorded by the call's origin run."""
    config = ctx.load_config()
    manifest = inventory.read_console_manifest(
        inventory.open_run(ctx.runs_root, origin_run_id)
    )
    request = (manifest or {}).get("request")
    raw_overrides = request.get("config_overrides") if isinstance(request, dict) else None
    if not raw_overrides:
        return config
    return apply_config_overrides(config, ConfigOverrides.model_validate(raw_overrides))
