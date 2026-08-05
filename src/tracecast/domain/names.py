"""Run-level proper-name replacement records.

One optional model call reads the complete selected factual materials and
proposes the final render-visible table directly.  Operator overrides remain
authoritative, stable ids and occurrence trace are compiled by code, and
``protected_surfaces`` prevent shorter accepted aliases from rewriting through
known longer names in materials.  Model rules apply only to the material evidence
they were derived from; operator aliases may also edit directives and fact hints.
Formal artifacts never persist model-local aliases.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .base import DomainModel


class NameOverride(DomainModel):
    """Optional project decision fixing aliases to one output-language surface."""

    aliases: list[str] = Field(min_length=1)
    target_surface: str
    output_language: str

    @model_validator(mode="after")
    def _check_override(self) -> NameOverride:
        if not self.target_surface.strip():
            raise ValueError("NameOverride.target_surface must be non-empty")
        if not self.output_language.strip():
            raise ValueError("NameOverride.output_language must be non-empty")
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("NameOverride.aliases must be non-empty strings")
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("NameOverride.aliases must be unique")
        return self


class NameResolutionRule(DomainModel):
    """One compiled run-global replacement rule."""

    rule_id: str
    aliases: list[str] = Field(min_length=1)
    stable_name: str
    evidence_chunk_ids: list[str] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_rule(self) -> NameResolutionRule:
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("NameResolutionRule.aliases must be non-empty")
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("NameResolutionRule.aliases must be unique")
        if not self.stable_name.strip():
            raise ValueError("NameResolutionRule.stable_name must be non-empty")
        if any(not chunk_id.strip() for chunk_id in self.evidence_chunk_ids):
            raise ValueError("NameResolutionRule evidence chunks must be non-empty")
        if len(self.evidence_chunk_ids) != len(set(self.evidence_chunk_ids)):
            raise ValueError("NameResolutionRule evidence chunks must be unique")
        return self


class NameResolutionPlan(DomainModel):
    """Formal pre-render editorial name table for one ordered contract set."""

    plan_id: str
    project_id: str
    output_language: str
    input_hash: str
    contract_ids: list[str]
    rules: list[NameResolutionRule] = Field(default_factory=list)
    protected_surfaces: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_plan(self) -> NameResolutionPlan:
        if not self.output_language.strip():
            raise ValueError("NameResolutionPlan.output_language must be non-empty")
        if len(self.contract_ids) != len(set(self.contract_ids)):
            raise ValueError("NameResolutionPlan contract ids must be unique")
        rule_ids = [rule.rule_id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("NameResolutionPlan rule ids must be unique")
        aliases = [alias for rule in self.rules for alias in rule.aliases]
        if len(aliases) != len(set(aliases)):
            raise ValueError("NameResolutionPlan aliases must belong to one rule")
        if any(not surface.strip() for surface in self.protected_surfaces):
            raise ValueError("NameResolutionPlan protected surfaces must be non-empty")
        if len(self.protected_surfaces) != len(set(self.protected_surfaces)):
            raise ValueError("NameResolutionPlan protected surfaces must be unique")
        overlap = set(aliases).intersection(self.protected_surfaces)
        if overlap:
            raise ValueError(
                "NameResolutionPlan surfaces cannot be both aliases and protected"
            )
        return self
