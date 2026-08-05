"""Shared helper for building audit items with deterministic ids."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from tracecast.domain import AuditReportItem, issue_id

from .kinds import AuditKind, Level, Stage


def make_item(
    stage: Stage,
    kind: AuditKind,
    message: str,
    *,
    level: Level = Level.error,
    usable: bool = True,
    artifact_ref: str | None = None,
    trace_refs: Iterable[str] | None = None,
    call_id: str | None = None,
    raw_response_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditReportItem:
    """Build an :class:`AuditReportItem`.

    ``usable`` defaults to ``True`` because Validation is report-only: a finding
    flags a problem but does not, by itself, make an artifact unconsumable (the
    message carries the specific ref, keeping the derived ``issue_id`` distinct
    per finding).
    """
    return AuditReportItem(
        issue_id=issue_id(str(stage), str(kind), artifact_ref, message),
        stage=str(stage),
        kind=str(kind),
        level=str(level),
        message=message,
        usable=usable,
        artifact_ref=artifact_ref,
        trace_refs=list(trace_refs or []),
        call_id=call_id,
        raw_response_path=raw_response_path,
        metadata=dict(metadata or {}),
    )
