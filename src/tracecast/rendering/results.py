"""Explicit outputs from the body rendering stage."""

from __future__ import annotations

from dataclasses import dataclass

from tracecast.domain import AuditReportItem, ChunkArtifact


@dataclass(frozen=True)
class RenderResult:
    """Rendered bodies and stage-owned findings for Validation."""

    artifacts: list[ChunkArtifact]
    reports: list[AuditReportItem]
