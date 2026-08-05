"""Chunk packing and render-contract generation (deterministic, LLM-free).

Turns the post-Node-Edit path plus edited materials into a ``ChunkPlan`` and one
``RenderContract`` per chunk, under driver-injected :class:`ChunkPolicy` budgets.

    from tracecast.packing import pack, ChunkPolicy
    result = pack(selection_plan, node_edit_plan, edited_materials, policy)
    result.chunk_plan      # ChunkPlan
    result.contracts       # list[RenderContract], 1:1 with chunk_plan.chunks
    result.reports         # list[AuditReportItem] (e.g. single-node overloads)
"""

from __future__ import annotations

from .packer import PackingError, PackingResult, pack
from .policy import DEFAULT_CHUNK_POLICY, ChunkPolicy, chunk_policy_from_budgets

__all__ = [
    "pack",
    "PackingResult",
    "PackingError",
    "ChunkPolicy",
    "DEFAULT_CHUNK_POLICY",
    "chunk_policy_from_budgets",
]
