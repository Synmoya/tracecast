"""Report-only audit and validation reporting.

Validation is the pipeline's auditor, not a gate. It strings the produced
artifacts, LLM calls and final text together and reports
damage, risk and untraceable points. It never reruns, repairs, mutates an artifact,
or decides whether the chain continues.

Entry point: build a :class:`ValidationContext` from the in-memory domain records the
driver already holds, then call :func:`run_validation` for the aggregate
:class:`ValidationReport`.
"""

from __future__ import annotations

from .audit import (
    audit_contract_consistency,
    audit_llm_calls,
    audit_rendered_text,
    audit_traceability,
    run_validation,
    summarize_calls,
    summarize_traceability,
)
from .context import (
    CallAuditSummary,
    TraceabilitySummary,
    ValidationContext,
    ValidationReport,
)
from .kinds import AuditKind, Level, Stage

__all__ = [
    # entry point
    "run_validation",
    # individual audits
    "audit_traceability",
    "audit_contract_consistency",
    "audit_rendered_text",
    "audit_llm_calls",
    "summarize_traceability",
    "summarize_calls",
    # inputs / outputs
    "ValidationContext",
    "ValidationReport",
    "TraceabilitySummary",
    "CallAuditSummary",
    # controlled vocabulary
    "AuditKind",
    "Level",
    "Stage",
]
