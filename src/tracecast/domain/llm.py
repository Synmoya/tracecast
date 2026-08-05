"""LLM Observer artifact: one audit record per provider attempt."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .base import DomainModel
from .enums import CostKind, ProviderStatus


class LlmWorkUnitRef(DomainModel):
    """Stable audit identity for the stage unit that produced one LLM call.

    This record is observer-only: it is attached after the canonical provider
    request envelope is built, so it cannot alter provider input or
    ``input_hash``.  ``input_refs`` contain stable artifact ids, never call-local
    aliases.
    """

    kind: str
    unit_id: str
    order_index: int | None = Field(default=None, ge=0)
    compression_round: int | None = Field(default=None, ge=0)
    input_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LlmCallRecord(DomainModel):
    """Audit record for a single provider attempt.

    ``call_id`` is fresh per attempt (see ``ids.new_call_id``). ``stage`` is the
    stable audit category; ``operation_route`` is the concrete model-config key.
    ``input_hash`` comes from the canonical provider request envelope.
    ``provider_status`` (transport) is kept separate from business parse status,
    which lives in stage reports. ``cost`` in v1 is a ``configured_estimate`` and
    must not be presented as a provider invoice.

    Token/cost/output fields are optional because a failed attempt may carry none
    of them.
    """

    call_id: str
    stage: str
    operation_route: str
    provider: str
    model: str
    input_hash: str
    provider_status: ProviderStatus
    created_at: datetime
    key_id: str | None = None
    prompt_version: str | None = None
    output_hash: str | None = None
    raw_request_path: str | None = None
    raw_response_path: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_output_tokens: int | None = Field(default=None, ge=0)
    visible_output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    cost_kind: CostKind | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    error: str | None = None
    work_unit: LlmWorkUnitRef | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
