"""Explicit Planning/Selection build results and report compilation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.domain import (
    AuditReportItem,
    CandidateGraph,
    LlmCallRecord,
    SelectionPlan,
    issue_id,
)

_REPORT_FIELDS = frozenset(
    {
        "stage",
        "kind",
        "level",
        "message",
        "usable",
        "artifact_ref",
        "trace_refs",
        "call_id",
        "raw_response_path",
    }
)


@dataclass(frozen=True)
class CandidateGraphBuildOutput:
    """Candidate graph plus Planning findings for Validation."""

    graph: CandidateGraph
    reports: list[AuditReportItem]


@dataclass(frozen=True)
class SelectionPlanBuildOutput:
    """Selection plan plus Selection findings for Validation."""

    plan: SelectionPlan
    reports: list[AuditReportItem]


def annotate_diagnostics_with_call(
    diagnostics: Iterable[MutableMapping[str, Any]],
    record: LlmCallRecord,
) -> None:
    """Attach the provider lineage shared by all Planning/Selection diagnostics."""

    for item in diagnostics:
        item.setdefault("call_id", record.call_id)
        if record.raw_response_path is not None:
            item.setdefault("raw_response_path", record.raw_response_path)


def diagnostics_to_audit_reports(
    diagnostics: Sequence[Mapping[str, Any]],
    *,
    artifact_ref: str,
) -> list[AuditReportItem]:
    """Compile stage-owned diagnostics into the shared report record."""
    return [
        _diagnostic_to_audit_report(item, default_artifact_ref=artifact_ref)
        for item in diagnostics
    ]


def _diagnostic_to_audit_report(
    item: Mapping[str, Any],
    *,
    default_artifact_ref: str,
) -> AuditReportItem:
    stage = str(item.get("stage") or "planning")
    kind = str(item.get("kind") or "stage_diagnostic")
    level = str(item.get("level") or "review")
    message = str(item.get("message") or kind)
    artifact_ref = _closest_artifact_ref(item, default=default_artifact_ref)
    trace_refs = _trace_refs(item)
    call_id = _optional_string(item.get("call_id"))
    raw_response_path = _optional_string(item.get("raw_response_path"))
    usable = item.get("usable")
    metadata = {key: value for key, value in item.items() if key not in _REPORT_FIELDS}
    identity_context = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return AuditReportItem(
        issue_id=issue_id(stage, kind, artifact_ref, f"{message}\x1f{identity_context}"),
        stage=stage,
        kind=kind,
        level=level,
        message=message,
        usable=usable if isinstance(usable, bool) else True,
        artifact_ref=artifact_ref,
        trace_refs=trace_refs,
        call_id=call_id,
        raw_response_path=raw_response_path,
        metadata=metadata,
    )


def _trace_refs(item: Mapping[str, Any]) -> list[str]:
    explicit = item.get("trace_refs")
    refs: list[str] = []
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        refs.extend(str(value) for value in explicit if value is not None)
    for key, value in item.items():
        if key in _REPORT_FIELDS or not key.endswith(("_id", "_ids", "_ref", "_refs")):
            continue
        if isinstance(value, str):
            refs.append(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            refs.extend(str(ref) for ref in value if isinstance(ref, str))
    return list(dict.fromkeys(refs))


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _closest_artifact_ref(item: Mapping[str, Any], *, default: str) -> str:
    for key in ("artifact_ref", "candidate_id", "window_id", "material_id", "child_id"):
        value = item.get(key)
        if value is not None:
            return str(value)
    return default
