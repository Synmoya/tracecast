"""Stage-local report items and result containers for Ingestion.

Failures and degradations are *recorded*, never silently repaired (architecture
``主链失败处理``). ``IngestionItem`` retains the source/window-shaped view used
inside the stage; :attr:`IngestResult.reports` exposes the same findings as
unified :class:`~tracecast.domain.AuditReportItem` records for Validation.
Producing no atoms for a hard-failed window plus a report item is the correct,
auditable outcome; fabricating filler atoms is the forbidden silent auto-repair.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracecast.domain import (
    AuditReportItem,
    LlmCallRecord,
    MaterialAtom,
    SourcePiece,
    SourceSnapshot,
    SourceWindow,
    issue_id,
)

# Report levels (reading priority, never a main-chain gate).
LEVEL_ERROR = "error"
LEVEL_WARNING = "warning"

# Report kinds.  Exact causes live in ``metadata["reason_code"]`` so callers can
# group by a stable recovery class without parsing human-readable messages.
SOURCE_PRECONDITION_FAILED = "source_precondition_failed"
WINDOW_TRANSPORT_FAILED = "window_transport_failed"
WINDOW_PROTOCOL_FAILED = "window_protocol_failed"
WINDOW_PARTITION_FAILED = "window_partition_failed"
WINDOW_PARTITION_REPAIRED = "window_partition_repaired"
SOURCE_INTEGRITY_FAILED = "source_integrity_failed"
UNKNOWN_TAG = "unknown_tag"  # optional label outside the controlled set -> dropped


@dataclass(frozen=True)
class IngestionItem:
    """One stage-local audit/report entry."""

    source_id: str
    kind: str
    level: str
    message: str
    window_id: str | None = None
    piece_refs: tuple[str, ...] = ()
    call_id: str | None = None
    raw_response_path: str | None = None
    usable: bool = True
    metadata: dict[str, object] = field(default_factory=dict)

    def to_audit_report(self) -> AuditReportItem:
        """Return the Validation-facing form without losing source trace refs."""
        artifact_ref = self.window_id or self.source_id
        trace_refs = list(dict.fromkeys((self.source_id, *self.piece_refs)))
        return AuditReportItem(
            issue_id=issue_id(
                "ingestion",
                self.kind,
                artifact_ref,
                "\x1f".join((self.message, *trace_refs)),
            ),
            stage="ingestion",
            kind=self.kind,
            level=self.level,
            message=self.message,
            usable=self.usable,
            artifact_ref=artifact_ref,
            trace_refs=trace_refs,
            call_id=self.call_id,
            raw_response_path=self.raw_response_path,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class SourceIngestResult:
    """Built artifacts plus report for a single source."""

    snapshot: SourceSnapshot
    pieces: list[SourcePiece] = field(default_factory=list)
    windows: list[SourceWindow] = field(default_factory=list)
    atoms: list[MaterialAtom] = field(default_factory=list)
    items: list[IngestionItem] = field(default_factory=list)
    call_records: list[LlmCallRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether this source proved a complete downstream-consumable evidence path."""
        return all(item.usable for item in self.items)

    @property
    def reports(self) -> list[AuditReportItem]:
        """Unified report items ready for ``ValidationContext.stage_reports``."""
        return [item.to_audit_report() for item in self.items]


@dataclass(frozen=True)
class IngestResult:
    """Aggregate result across every ingested source."""

    sources: list[SourceIngestResult] = field(default_factory=list)

    @property
    def snapshots(self) -> list[SourceSnapshot]:
        return [s.snapshot for s in self.sources]

    @property
    def pieces(self) -> list[SourcePiece]:
        return [p for s in self.sources for p in s.pieces]

    @property
    def windows(self) -> list[SourceWindow]:
        return [w for s in self.sources for w in s.windows]

    @property
    def atoms(self) -> list[MaterialAtom]:
        return [a for s in self.sources for a in s.atoms]

    @property
    def reports(self) -> list[AuditReportItem]:
        """All source findings in Validation's cross-stage report shape."""
        return [item for source in self.sources for item in source.reports]

    @property
    def call_records(self) -> list[LlmCallRecord]:
        return [rec for s in self.sources for rec in s.call_records]

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.sources)


class IngestionError(RuntimeError):
    """Raised by a root pipeline when Ingestion cannot publish evidence atoms."""

    def __init__(self, result: IngestResult) -> None:
        failed_sources = [
            source.snapshot.source_id for source in result.sources if not source.ok
        ]
        detail = ", ".join(failed_sources) or "unknown source"
        super().__init__(f"ingestion failed for source(s): {detail}")
        self.result = result
        self.reports = result.reports
