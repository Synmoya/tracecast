"""Read-only Chunk Lab injection-block view and template drift check."""

from __future__ import annotations

from typing import Any

from tracecast.domain import estimate_tokens
from tracecast.rendering import (
    RENDER_PORTABLE_BLOCK_KEYS,
    RENDER_ROUTE,
    apply_render_block_overrides,
    substitute_render_blocks,
)
from tracecast.storage import CHUNK_ARTIFACTS

from .chunklab_baseline import load_chunk_baseline
from .context import ConsoleContext
from .diffs import unified_text_diff
from .errors import BadRequest, NotFound
from .inventory import open_run, raw_payload, read_console_manifest


def chunk_blocks(ctx: ConsoleContext, run_id: str, chunk_id: str) -> dict[str, Any]:
    """The injection-block view of one chunk, with drift check vs the recorded call."""
    store = open_run(ctx.runs_root, run_id)
    config = ctx.load_config()
    baseline = load_chunk_baseline(ctx, store, chunk_id, config)
    effective_blocks = dict(baseline.blocks)
    block_source = "reconstructed"
    block_source_run_id: str | None = None

    prompt_origin_run_id = baseline.artifact_sources.get(
        CHUNK_ARTIFACTS.relpath, store.run_id
    )
    prompt_origin = open_run(ctx.runs_root, prompt_origin_run_id)
    manifest = read_console_manifest(prompt_origin) or {}
    request = manifest.get("request")
    if (
        manifest.get("kind") == "chunklab"
        and isinstance(request, dict)
        and request.get("chunk_id") == chunk_id
    ):
        snapshot = request.get("injection_blocks")
        if isinstance(snapshot, dict) and set(snapshot) == set(effective_blocks):
            effective_blocks = {str(key): str(value) for key, value in snapshot.items()}
            block_source = "manifest_snapshot"
            block_source_run_id = prompt_origin.run_id
        else:
            raise BadRequest(
                f"chunklab run {prompt_origin.run_id!r} has no complete injection snapshot"
            )
    elif manifest.get("kind") == "render" and isinstance(request, dict):
        overrides = request.get("render_block_overrides")
        if isinstance(overrides, dict) and overrides:
            try:
                effective_blocks = apply_render_block_overrides(
                    effective_blocks,
                    {str(key): value for key, value in overrides.items()},
                )
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
            block_source = "render_block_overrides"
            block_source_run_id = prompt_origin.run_id

    effective_prompt = substitute_render_blocks(baseline.template, effective_blocks)
    edited_keys = sorted(
        key for key, value in effective_blocks.items() if value != baseline.blocks[key]
    )
    matches: bool | None = None
    drift_diff: list[str] | None = None
    artifact = baseline.baseline_artifact
    call_id = artifact.metadata.get("call_id") if artifact else None
    if call_id:
        try:
            raw = raw_payload(store, str(call_id), "request")
            recorded = raw.get("data") if raw.get("format") == "json" else None
        except NotFound:
            recorded = None
        if isinstance(recorded, dict):
            messages = recorded.get("messages") or []
            recorded_user = next(
                (
                    str(message.get("content", ""))
                    for message in messages
                    if message.get("role") == "user"
                ),
                "",
            )
            matches = recorded_user == effective_prompt
            if not matches:
                drift_diff = unified_text_diff(
                    recorded_user,
                    effective_prompt,
                    label_a="recorded",
                    label_b="effective",
                )

    return {
        "run_id": run_id,
        "chunk_id": chunk_id,
        "order_index": baseline.contract.order_index,
        "contract_id": baseline.contract.contract_id,
        "chunk_index": baseline.chunk_position.chunk_index,
        "chunk_count": baseline.chunk_position.chunk_count,
        "segment_position": baseline.chunk_position.segment_position,
        "blocks": effective_blocks,
        "reconstructed_blocks": baseline.blocks,
        "portable_block_keys": list(RENDER_PORTABLE_BLOCK_KEYS),
        "block_source": block_source,
        "block_source_run_id": block_source_run_id,
        "edited_keys": edited_keys,
        "template": {"route": RENDER_ROUTE, "prompt_version": baseline.prompt_version},
        "artifact_sources": baseline.artifact_sources,
        "output_language": baseline.output_language,
        "name_resolution_plan_id": baseline.name_resolution.plan_id,
        "baseline": (
            {
                "call_id": call_id,
                "text_chars": len(artifact.text),
                "output_hash": artifact.output_hash,
            }
            if artifact
            else None
        ),
        "est_input_tokens": (
            estimate_tokens(baseline.system_prompt) + estimate_tokens(effective_prompt)
        ),
        "matches_recorded_request": matches,
        "drift_diff": drift_diff,
    }
