"""Shared base model and primitive value objects for domain records.

Every persisted Tracecast artifact subclasses :class:`DomainModel`. The config
is deliberately strict: unknown fields are rejected rather than silently
dropped (code owns artifact structure; genuinely unstable fields belong in an
explicit ``metadata`` dict), and assignment is re-validated.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    """Base class for all Tracecast domain records."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SourceSpan(DomainModel):
    """A character span into a source/text: ``[start, end)``.

    ``start``/``end`` are non-negative character offsets; ``end`` must not be
    smaller than ``start``. This is an intra-record invariant only -- whether
    the span actually lines up with a piece or chunk is a stage concern.
    """

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_order(self) -> SourceSpan:
        if self.end < self.start:
            raise ValueError("SourceSpan.end must be >= start")
        return self
