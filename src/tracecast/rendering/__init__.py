"""Rendering stage: contract projection, the render call, and chunk wrapping.

``persona`` exposes the replaceable voice / expression layer; ``renderer`` owns the
model-facing chunk projection and one independent provider call per chunk, and code owns
:class:`~tracecast.domain.ChunkArtifact` wrapping (body verbatim, hashes computed
by code).
"""

from __future__ import annotations

from .degenerate import is_degenerate_repetition, longest_repetition_run
from .fact_context import (
    FACT_CONTEXT_ROUTE,
    FactContextBudgets,
    FactContextResult,
    build_fact_contexts,
    parse_fact_contexts,
    project_fact_context_outline,
    render_fact_context_user_prompt,
)
from .persona import RenderPersona, ViewpointProfile, coerce_persona
from .renderer import (
    RENDER_PORTABLE_BLOCK_KEYS,
    RENDER_ROUTE,
    STAGE_RENDERING,
    RenderChunkPosition,
    apply_render_block_overrides,
    bind_render_output_language,
    build_render_blocks,
    derive_render_chunk_positions,
    project_chunk,
    render_chunk_position_block,
    render_chunk_user_prompt,
    render_chunks,
    render_host_expression_block,
    render_host_thinking_block,
    substitute_render_blocks,
)
from .results import RenderResult

__all__ = [
    # degenerate output detection
    "is_degenerate_repetition",
    "longest_repetition_run",
    # persona (voice / expression layer)
    "RenderPersona",
    "ViewpointProfile",
    "coerce_persona",
    # pre-render factual linkage
    "FactContextBudgets",
    "FactContextResult",
    "build_fact_contexts",
    "parse_fact_contexts",
    "project_fact_context_outline",
    "render_fact_context_user_prompt",
    "FACT_CONTEXT_ROUTE",
    # renderer
    "render_chunks",
    "RenderResult",
    "project_chunk",
    "apply_render_block_overrides",
    "bind_render_output_language",
    "build_render_blocks",
    "derive_render_chunk_positions",
    "substitute_render_blocks",
    "render_chunk_position_block",
    "render_chunk_user_prompt",
    "render_host_expression_block",
    "render_host_thinking_block",
    "RENDER_ROUTE",
    "RENDER_PORTABLE_BLOCK_KEYS",
    "RenderChunkPosition",
    "STAGE_RENDERING",
]
