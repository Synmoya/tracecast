"""Candidate graph and selection path stages.

This package covers both Planning (``CandidateGraph``) and Selection
(``SelectionPlan``). The two driver entry points are
:func:`build_candidate_graph` and :func:`build_selection_plan`;
both take an injected ``RuntimeConfig``/``RunStore`` and an optional ``call``
seam (default :func:`tracecast.llm.call_llm`) so a fake provider can be injected
without any network. The driver persists the returned artifacts; ``call_llm``
persists its own call records.

The model emits DSL (structure + non-fact labels) only; this package owns alias
projection, DSL parsing, stable id derivation, the artifact rebuild, coverage and
diagnostics. Persona/viewpoint/expression enters Selection via
:class:`PersonaConfig`.
"""

from __future__ import annotations

from .candidate_graph import LlmCall, build_candidate_graph
from .persona import PersonaConfig, ViewpointProfile, render_persona_block
from .results import (
    CandidateGraphBuildOutput,
    SelectionPlanBuildOutput,
)
from .selection_plan import build_selection_plan
from .vocab import (
    ALLOWED_NARRATIVE_FUNCTIONS,
    DIRECTIONAL_EDGE_ROLES,
    SYMMETRIC_EDGE_TYPES,
)

__all__ = [
    # driver entry points
    "build_candidate_graph",
    "build_selection_plan",
    "CandidateGraphBuildOutput",
    "SelectionPlanBuildOutput",
    # persona entry layer (Selection input shape)
    "PersonaConfig",
    "ViewpointProfile",
    "render_persona_block",
    # call seam type
    "LlmCall",
    # vocabularies
    "ALLOWED_NARRATIVE_FUNCTIONS",
    "DIRECTIONAL_EDGE_ROLES",
    "SYMMETRIC_EDGE_TYPES",
]
