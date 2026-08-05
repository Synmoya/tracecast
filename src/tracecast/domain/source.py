"""Ingestion artifacts: snapshots, pieces, windows, names, material atoms.

``source_type`` and ``source_class`` are open validated strings (not enums): the
value sets are owned by the extensible source-profile config, not by this
backbone (see ``enums`` module docstring).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from .base import DomainModel
from .enums import ContentKind, TrustTier


class SourceSnapshot(DomainModel):
    """Fixed source snapshot; the anchor for every atom's provenance.

    ``source_id`` is provided by intake (the adapter that declared the source),
    not derived here; downstream ids derive *from* it.
    """

    source_id: str
    source_type: str
    source_class: str
    trust_tier: TrustTier
    url: str | None = None
    path: str | None = None
    title: str | None = None
    fetched_at: datetime | None = None
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourcePiece(DomainModel):
    """A global, deterministic *physical* slice of a source (not a semantic atom).

    ``piece_id`` is expected to be ``ids.piece_id(source_id, order_index)``; the
    record stores the value but does not recompute it.
    """

    piece_id: str
    source_id: str
    order_index: int = Field(ge=0)
    text: str
    start_line: int | None = Field(default=None, ge=0)
    end_line: int | None = Field(default=None, ge=0)
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    char_count: int = Field(ge=0)


class SourceWindow(DomainModel):
    """Call-packing boundary over a run of consecutive ``SourcePiece``s.

    Cross-window invariants (consecutive, no overlap, no gap) are enforced by
    Ingestion, not here; this record only requires at least one piece.
    """

    window_id: str
    source_id: str
    piece_ids: list[str] = Field(min_length=1)
    order_index: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Provenance(DomainModel):
    """Lineage of a :class:`MaterialAtom` back to its source snapshot/pieces."""

    source_ref: str
    source_type: str
    source_class: str
    source_content_hash: str
    fetched_at: datetime | None = None
    span_start: int | None = Field(default=None, ge=0)
    span_end: int | None = Field(default=None, ge=0)
    piece_refs: list[str] = Field(default_factory=list)
    extractor_version: str | None = None

    @model_validator(mode="after")
    def _check_span(self) -> Provenance:
        if (
            self.span_start is not None
            and self.span_end is not None
            and self.span_end < self.span_start
        ):
            raise ValueError("Provenance.span_end must be >= span_start")
        return self


class MaterialAtom(DomainModel):
    """The basic unit of the evidence path and a permitted fact source.

    ``content`` (and the ``content`` of a lineage-bearing condensed atom) is the
    only thing that may introduce facts downstream. ``content_kind``/``tags`` are
    volatile labels and are excluded from ``id`` derivation.

    A stable (Ingestion) atom has an empty ``source_material_ids``; a run-scoped
    condensed atom produced by Node Edit carries its lineage there. The
    "condense atom must carry lineage" rule is enforced where the operation is
    known (``node_edit.MaterialEdit``), since a bare atom has no operation flag.
    """

    id: str
    content: str
    language: str
    content_kind: ContentKind
    source_order: int = Field(ge=0)
    trust_tier: TrustTier
    provenance: Provenance
    tags: list[str] = Field(default_factory=list)
    source_material_ids: list[str] = Field(default_factory=list)
