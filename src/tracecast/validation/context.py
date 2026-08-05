"""In-memory inputs and aggregate outputs for the validation pass.

Validation is a pure transform: the pipeline driver injects already-built domain
records as parameters (no config load, no run-file IO, no network). These
containers are plain frozen dataclasses; only ``AuditReportItem`` records are
persisted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from tracecast.domain import (
    AuditReportItem,
    CandidateGraph,
    ChunkArtifact,
    ChunkPlan,
    LlmCallRecord,
    MaterialAtom,
    NodeEditPlan,
    RenderContract,
    SelectionPlan,
    SourcePiece,
    SourceSnapshot,
)


@dataclass(frozen=True)
class ValidationContext:
    """Everything the auditor may inspect, all optional.

    A finding for a "missing" reference is only emitted when the *referenced*
    artifact class was actually supplied (e.g. ``missing_piece_ref`` requires a
    non-empty ``pieces``); auditing a partial slice therefore does not produce
    cascades of false positives for records the driver simply did not inject.

    ``stage_reports`` are pre-existing stage-local ``AuditReportItem``s (parser/
    compiler failures, packing budget overloads, renderer reports) that Validation
    aggregates rather than recomputes. ``available_response_paths`` lets the driver
    supply raw-response existence info so the auditor never touches the filesystem;
    ``raw_chunk_text`` supplies the verbatim LLM body for the
    ``text_changed_after_render`` check.
    """

    snapshots: Sequence[SourceSnapshot] = ()
    pieces: Sequence[SourcePiece] = ()
    atoms: Sequence[MaterialAtom] = ()
    edited_materials: Sequence[MaterialAtom] = ()
    candidate_graph: CandidateGraph | None = None
    selection_plan: SelectionPlan | None = None
    node_edit_plan: NodeEditPlan | None = None
    chunk_plan: ChunkPlan | None = None
    contracts: Sequence[RenderContract] = ()
    chunks: Sequence[ChunkArtifact] = ()
    call_records: Sequence[LlmCallRecord] = ()
    stage_reports: Sequence[AuditReportItem] = ()
    raw_chunk_text: Mapping[str, str] | None = None
    available_response_paths: frozenset[str] | None = None


@dataclass(frozen=True)
class TraceabilitySummary:
    """Counts for the evidence-grounding chain (every chunk should reach an atom)."""

    chunks_total: int
    chunks_traceable: int
    materials_total: int
    materials_traceable: int
    broken_issue_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CallAuditSummary:
    """LLM call ledger summary (cost in v1 is a configured estimate)."""

    calls_total: int
    calls_by_stage: Mapping[str, int] = field(default_factory=dict)
    total_estimated_cost: float = 0.0
    missing_raw_response: int = 0
    missing_prompt_version: int = 0


@dataclass(frozen=True)
class ValidationReport:
    """Aggregate report-only result handed back to the driver.

    Nothing here gates the chain: Assembly may ship a script flagged
    ``assembled_with_issues`` regardless of these findings.
    """

    items: tuple[AuditReportItem, ...]
    traceability: TraceabilitySummary
    call_audit: CallAuditSummary
