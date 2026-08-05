"""Planning artifacts: the high-recall candidate graph.

``edge_type`` and the endpoint *role* keys are protocol-owned open strings.
Symmetric-edge normalization/dedup is a Planning concern and is not enforced
here.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .base import DomainModel, SourceSpan


class CandidateNode(DomainModel):
    """A candidate grouping of materials from a single source window.

    ``summary``/``narrative_function`` are for selector understanding and are not
    fact sources; ``narrative_function`` is volatile and excluded from
    ``candidate_id``. Per v1, each ``MaterialAtom`` belongs to at most one node --
    that partition invariant lives in Planning, not on this record.
    """

    candidate_id: str
    source_window: str
    summary: str
    material_ids: list[str] = Field(min_length=1)
    material_estimated_tokens: int | None = Field(default=None, ge=0)
    narrative_function: str | None = None
    source_span: SourceSpan | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateEdge(DomainModel):
    """A selector hint relating two candidate nodes -- not a path constraint.

    ``endpoints`` maps an endpoint *role* to a candidate ref: directional edges
    use role pairs like ``{"setup": ..., "payoff": ...}`` or
    ``{"cause": ..., "effect": ...}``; symmetric edges use ``{"a": ..., "b": ...}``.
    No continuous strength/confidence. ``reason`` is not a fact source.
    """

    edge_type: str
    endpoints: dict[str, str] = Field(min_length=2)
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateGraph(DomainModel):
    """High-recall candidate space owned by Planning.

    ``metadata`` holds protocol identifiers, coverage counts, and compact
    compression/edge build summaries. Parse and coverage findings are returned
    explicitly by the Planning build output, not mirrored into this artifact.
    """

    graph_id: str
    project_id: str
    nodes: list[CandidateNode] = Field(default_factory=list)
    edges: list[CandidateEdge] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
