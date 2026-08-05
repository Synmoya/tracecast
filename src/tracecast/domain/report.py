"""Validation and Assembly report records.

``stage``/``kind``/``level`` are open strings and ``metadata`` is the extension
point. Reports describe issues; they never gate the main chain.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import DomainModel


class AuditReportItem(DomainModel):
    """A unified, cross-stage audit issue.

    ``level`` is reading priority only (it does not decide whether the main chain
    continues); ``usable`` says whether the artifact can still be consumed
    downstream (not a retry policy). Issues should trace to a call/artifact/
    material/piece where possible.
    """

    issue_id: str
    stage: str
    kind: str
    level: str
    message: str
    usable: bool
    artifact_ref: str | None = None
    trace_refs: list[str] = Field(default_factory=list)
    call_id: str | None = None
    raw_response_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssemblyReport(DomainModel):
    """Final assembly audit.

    Assembly only mechanically concatenates available chunks; it may still ship a
    script flagged ``assembled_with_issues`` rather than refusing output.
    """

    assembled_chunk_ids: list[str] = Field(default_factory=list)
    chunk_order: list[str] = Field(default_factory=list)
    missing_chunk_ids: list[str] = Field(default_factory=list)
    duplicate_chunk_ids: list[str] = Field(default_factory=list)
    per_chunk_text_hash: dict[str, str] = Field(default_factory=dict)
    final_script_hash: str
    assembled_with_issues: bool
    validation_report_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
