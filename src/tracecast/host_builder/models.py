"""Typed Host Builder artifacts and compiled intermediate values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from tracecast.control_prompt import ControlPromptDecision
from tracecast.domain import DomainModel
from tracecast.hosts import HostProfile, StyleExamplePosition


class HostBuildInputRecord(DomainModel):
    path: str
    content_hash: str
    char_count: int = Field(ge=0)
    script_counts: dict[str, int] = Field(default_factory=dict)
    eligible: bool
    exclusion_reason: str | None = None
    trim_start: int = Field(default=0, ge=0)
    trim_end: int = Field(default=0, ge=0)


class HostBuildSelectedExample(DomainModel):
    candidate_id: str
    position: StyleExamplePosition
    input_path: str
    input_content_hash: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    render_hash: str
    estimated_tokens: int = Field(ge=0)


class HostBuildCandidate(DomainModel):
    candidate_id: str
    position: StyleExamplePosition
    input_path: str
    input_content_hash: str
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    render_hash: str
    estimated_tokens: int = Field(ge=0)


class HostBuildManifest(DomainModel):
    schema_version: Literal["tracecast-host-build/1"] = "tracecast-host-build/1"
    run_id: str
    host_id: str
    status: Literal["running", "complete", "failed"] = "running"
    piece_max_tokens: int = Field(gt=0)
    profile_sample_token_budget: int = Field(gt=0)
    seed: int
    dominant_script: str
    control_prompt_decision: ControlPromptDecision
    output_language: str | None = None
    inputs: list[HostBuildInputRecord] = Field(default_factory=list)
    candidates: list[HostBuildCandidate] = Field(default_factory=list)
    selected_examples: list[HostBuildSelectedExample] = Field(default_factory=list)
    call_ids: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    installed_path: str | None = None


class DistilledHostProfile(DomainModel):
    output_language: str
    viewpoint: str
    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    thinking_rules: list[str] = Field(default_factory=list)
    expression_description: str
    expression_rules: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class HostBuildResult:
    run_id: str
    run_dir: str
    host_path: str
    host: HostProfile
    calls: int
    sampled_examples: int


__all__ = [
    "DistilledHostProfile",
    "HostBuildCandidate",
    "HostBuildInputRecord",
    "HostBuildManifest",
    "HostBuildResult",
    "HostBuildSelectedExample",
]
