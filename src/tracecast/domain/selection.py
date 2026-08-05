"""Selection artifacts: the chosen path and node-level control."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .base import DomainModel
from .enums import Role, SelectionStatus, Treatment


class SelectionDecision(DomainModel):
    """Per-candidate decision: accepted (with control) or not selected.

    For an ``accepted`` decision ``role`` and ``treatment`` are required -- they
    are the node-level control the selector emits. A ``not_selected`` decision is
    code-filled and may leave them unset. ``directive`` is optional beat control:
    ``None`` is reserved for nodes whose edited material needs no extra rendering
    instruction. It is not a fact source.
    """

    candidate_id: str
    status: SelectionStatus
    role: Role | None = None
    treatment: Treatment | None = None
    directive: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_accepted(self) -> SelectionDecision:
        if self.status is SelectionStatus.accepted and (
            self.role is None
            or self.treatment is None
        ):
            raise ValueError(
                "accepted SelectionDecision requires role and treatment"
            )
        if self.directive is not None and not self.directive.strip():
            raise ValueError("SelectionDecision directive must be non-blank or null")
        return self


class SelectionPlan(DomainModel):
    """Path, presentation order and node-level control owned by Selection.

    ``selected_sequence`` is the selector path row order -- code does not
    reorder it. Completeness (every candidate has a decision) is a stage
    invariant, not enforced here. ``persona_id`` is an external input.
    """

    plan_id: str
    project_id: str
    persona_id: str
    selected_sequence: list[str] = Field(default_factory=list)
    decisions: list[SelectionDecision] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
