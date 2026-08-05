"""Provider dispatch for Node Edit batch calls and one bounded repair wave."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from tracecast.concurrency import route_max_concurrency, run_ordered_concurrently
from tracecast.domain import LlmCallRecord, LlmWorkUnitRef
from tracecast.llm import LlmTransportError
from tracecast.storage import RunStore, RuntimeConfig

from .projection import BatchNode, BatchProjection
from .vocab import NODE_EDIT_REPAIR_ROUTE, NODE_EDIT_ROUTE

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


@dataclass(frozen=True)
class NodeEditBatchCall:
    """Prepared provider call for one K/C/O batch."""

    batch: list[BatchNode]
    projection: BatchProjection
    user_prompt: str


@dataclass(frozen=True)
class NodeEditBatchResult:
    """Provider result for one K/C/O batch."""

    batch: list[BatchNode]
    projection: BatchProjection
    text: str | None
    record: LlmCallRecord | None
    error: LlmTransportError | None = None


@dataclass(frozen=True)
class NodeEditRepairCall:
    """One failed node reprojected as an independent complete-block request."""

    batch_node: BatchNode
    projection: BatchProjection
    user_prompt: str
    initial_failure_code: str
    initial_failure_detail: str
    initial_record: LlmCallRecord
    initial_raw_block: str | None


@dataclass(frozen=True)
class NodeEditRepairResult:
    """Provider result for one single-node repair request."""

    request: NodeEditRepairCall
    text: str | None
    record: LlmCallRecord | None
    error: LlmTransportError | None = None


def run_node_edit_batch_calls(
    calls: Sequence[NodeEditBatchCall],
    *,
    system_prompt: str,
    prompt_version: str,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
) -> list[NodeEditBatchResult]:
    """Dispatch the normal batch wave under the route concurrency limit."""
    return run_ordered_concurrently(
        list(calls),
        lambda item: _run_batch(
            item,
            system_prompt=system_prompt,
            prompt_version=prompt_version,
            config=config,
            store=store,
            call=call,
        ),
        max_workers=route_max_concurrency(config, NODE_EDIT_ROUTE),
    )


def run_node_edit_repair_calls(
    calls: Sequence[NodeEditRepairCall],
    *,
    system_prompt: str,
    prompt_version: str,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
) -> list[NodeEditRepairResult]:
    """Dispatch exactly one concurrent second wave for failed node blocks."""
    return run_ordered_concurrently(
        list(calls),
        lambda item: _run_repair(
            item,
            system_prompt=system_prompt,
            prompt_version=prompt_version,
            config=config,
            store=store,
            call=call,
        ),
        max_workers=route_max_concurrency(config, NODE_EDIT_REPAIR_ROUTE),
    )


def _run_batch(
    request: NodeEditBatchCall,
    *,
    system_prompt: str,
    prompt_version: str,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
) -> NodeEditBatchResult:
    unit_id = f"batch:{request.batch[0].candidate_id}"
    try:
        text, record = call(
            operation_route=NODE_EDIT_ROUTE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            config=config,
            store=store,
            prompt_version=prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="node_edit_batch",
                unit_id=unit_id,
                input_refs=[
                    ref
                    for batch_node in request.batch
                    for ref in (
                        batch_node.candidate_id,
                        *(atom.id for atom in batch_node.pool),
                    )
                ],
            ),
        )
    except LlmTransportError as exc:
        return NodeEditBatchResult(
            batch=request.batch,
            projection=request.projection,
            text=None,
            record=exc.record,
            error=exc,
        )
    return NodeEditBatchResult(
        batch=request.batch,
        projection=request.projection,
        text=text,
        record=record,
    )


def _run_repair(
    request: NodeEditRepairCall,
    *,
    system_prompt: str,
    prompt_version: str,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
) -> NodeEditRepairResult:
    unit_id = f"repair:{request.batch_node.candidate_id}"
    try:
        text, record = call(
            operation_route=NODE_EDIT_REPAIR_ROUTE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            config=config,
            store=store,
            prompt_version=prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="node_edit_block_repair",
                unit_id=unit_id,
                input_refs=[
                    request.batch_node.candidate_id,
                    *(atom.id for atom in request.batch_node.pool),
                ],
                metadata={"attempt": 1},
            ),
        )
    except LlmTransportError as exc:
        return NodeEditRepairResult(
            request=request,
            text=None,
            record=exc.record,
            error=exc,
        )
    return NodeEditRepairResult(request=request, text=text, record=record)
