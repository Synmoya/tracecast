"""Sampled source-intake proposal stage.

``intake`` is a pre-ingestion adapter layer: it scans local raw files, samples
them with the existing ingestion slicer, asks a low-cost DSL probe call to choose
source profiles, then writes a human-reviewable ``sources.proposed.json``.
Core ingestion still only accepts already-declared source profiles.
"""

from __future__ import annotations

from .dsl import IntakeParseError, parse_probe_response, render_probe_user_prompt
from .pipeline import (
    IntakeEstimate,
    IntakeResult,
    estimate_intake,
    resolve_intake_control_prompt,
    run_intake,
)
from .sampling import SourceProbe, SourceSample, build_source_probe

__all__ = [
    "IntakeParseError",
    "IntakeEstimate",
    "IntakeResult",
    "SourceProbe",
    "SourceSample",
    "build_source_probe",
    "estimate_intake",
    "parse_probe_response",
    "render_probe_user_prompt",
    "resolve_intake_control_prompt",
    "run_intake",
]
