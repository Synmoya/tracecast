"""Protocol constants and the audit-report helper for Node Edit.

The route key and the ``stage_budgets`` key names are protocol-owned, not
project-specific. Budget *values* are read from ``RuntimeConfig``; the defaults
here are documented fallbacks used only when config
omits the keys, never hardcoded operational overrides.
"""

from __future__ import annotations

from typing import Any

from tracecast.domain import AuditReportItem, issue_id

# operation_route key looked up in RuntimeConfig.routes (configs/runtime.json).
NODE_EDIT_ROUTE = "node_edit"
NODE_EDIT_REPAIR_ROUTE = "node_edit_block_repair"

# Stage label on every report item and the stage_budgets key.
STAGE_NODE_EDIT = "node_edit"

# stage_budgets["node_edit"] keys (configs/runtime.json).
BUDGET_BATCH_MAX_INPUT_TOKENS = "batch_max_input_tokens"
BUDGET_MAX_NODES_PER_BATCH = "max_nodes_per_batch"

# Fallback batch limits; runtime config overrides them when set.
_DEFAULT_BATCH_MAX_INPUT_TOKENS = 32768
_DEFAULT_MAX_NODES_PER_BATCH = 5


def default_batch_max_input_tokens() -> int:
    """The v1 fallback batch input budget, used only when config omits the key."""
    return _DEFAULT_BATCH_MAX_INPUT_TOKENS


def default_max_nodes_per_batch() -> int:
    """Fallback batch width, used only when config omits the key."""
    return _DEFAULT_MAX_NODES_PER_BATCH


def report_item(
    kind: str,
    message: str,
    *,
    usable: bool = True,
    artifact_ref: str | None = None,
    trace_refs: list[str] | None = None,
    call_id: str | None = None,
    raw_response_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditReportItem:
    """Build one report-only :class:`AuditReportItem` for a node-edit issue.

    Report-only: ``usable`` stays ``True`` -- the issue is recorded and the main
    chain can continue. ``issue_id`` is deterministic from ``(stage, kind,
    artifact_ref, message)`` so re-runs dedupe.
    """
    return AuditReportItem(
        issue_id=issue_id(STAGE_NODE_EDIT, kind, artifact_ref, message),
        stage=STAGE_NODE_EDIT,
        kind=kind,
        level="review",
        message=message,
        usable=usable,
        artifact_ref=artifact_ref,
        trace_refs=trace_refs or [],
        call_id=call_id,
        raw_response_path=raw_response_path,
        metadata=metadata or {},
    )
