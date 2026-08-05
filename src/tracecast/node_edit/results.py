"""Explicit result bundle returned by the Node Edit stage."""

from __future__ import annotations

from dataclasses import dataclass

from tracecast.domain import AuditReportItem, MaterialAtom, NodeEditPlan


@dataclass(frozen=True)
class NodeEditBuildOutput:
    """All artifacts and findings produced by one Node Edit build."""

    plan: NodeEditPlan
    edited_materials: list[MaterialAtom]
    reports: list[AuditReportItem]
    @property
    def stage_reports(self) -> list[AuditReportItem]:
        """All Node Edit findings ready for Validation aggregation."""
        return list(self.reports)
