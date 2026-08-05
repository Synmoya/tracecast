"""Domain schemas and stable identifiers.

The shared contract backbone every other Tracecast module imports from. Re-exports
the full public surface so callers can do e.g.::

    from tracecast.domain import MaterialAtom, RenderContract, estimate_tokens
"""

from __future__ import annotations

from .base import DomainModel, SourceSpan
from .enums import (
    ContentKind,
    CostKind,
    NodeEditOperation,
    ProviderStatus,
    Role,
    SelectionStatus,
    Treatment,
    TrustTier,
)
from .graph import CandidateEdge, CandidateGraph, CandidateNode
from .ids import (
    CANDIDATE_ALIASES,
    MATERIAL_ALIASES,
    NODE_ALIASES,
    PIECE_ALIASES,
    RENDER_ALIASES,
    AliasManifest,
    AliasScheme,
    atom_id,
    candidate_id,
    chunk_id,
    chunk_plan_id,
    contract_id,
    derive_id,
    graph_id,
    issue_id,
    name_resolution_plan_id,
    name_resolution_rule_id,
    new_call_id,
    node_edit_plan_id,
    piece_id,
    selection_plan_id,
    window_id,
)
from .llm import LlmCallRecord, LlmWorkUnitRef
from .names import NameOverride, NameResolutionPlan, NameResolutionRule
from .node_edit import (
    MaterialEdit,
    MaterialOmission,
    NodeEditPlan,
    NodeEditResult,
)
from .render import (
    ChunkArtifact,
    ChunkPlan,
    ChunkPlanChunk,
    RenderContract,
    RenderContractMaterial,
    RenderContractNode,
    RenderFactContext,
    RenderFactContextNodeHint,
)
from .report import (
    AssemblyReport,
    AuditReportItem,
)
from .selection import SelectionDecision, SelectionPlan
from .source import (
    MaterialAtom,
    Provenance,
    SourcePiece,
    SourceSnapshot,
    SourceWindow,
)
from .tokens import estimate_tokens

__all__ = [
    # base
    "DomainModel",
    "SourceSpan",
    # enums
    "ContentKind",
    "TrustTier",
    "Role",
    "Treatment",
    "NodeEditOperation",
    "SelectionStatus",
    "CostKind",
    "ProviderStatus",
    # source / ingestion records
    "SourceSnapshot",
    "SourcePiece",
    "SourceWindow",
    "Provenance",
    "MaterialAtom",
    # run-level name resolution records
    "NameResolutionPlan",
    "NameResolutionRule",
    "NameOverride",
    # planning records
    "CandidateGraph",
    "CandidateNode",
    "CandidateEdge",
    # selection records
    "SelectionPlan",
    "SelectionDecision",
    # node edit records
    "NodeEditPlan",
    "NodeEditResult",
    "MaterialEdit",
    "MaterialOmission",
    # packing / rendering records
    "ChunkPlan",
    "ChunkPlanChunk",
    "RenderContract",
    "RenderFactContext",
    "RenderFactContextNodeHint",
    "RenderContractNode",
    "RenderContractMaterial",
    "ChunkArtifact",
    # report records
    "AuditReportItem",
    "AssemblyReport",
    # llm records
    "LlmCallRecord",
    "LlmWorkUnitRef",
    # token estimation
    "estimate_tokens",
    # stable id helpers
    "derive_id",
    "piece_id",
    "window_id",
    "atom_id",
    "name_resolution_rule_id",
    "name_resolution_plan_id",
    "candidate_id",
    "graph_id",
    "selection_plan_id",
    "node_edit_plan_id",
    "chunk_plan_id",
    "chunk_id",
    "contract_id",
    "issue_id",
    "new_call_id",
    # alias helpers
    "AliasScheme",
    "AliasManifest",
    "PIECE_ALIASES",
    "MATERIAL_ALIASES",
    "CANDIDATE_ALIASES",
    "NODE_ALIASES",
    "RENDER_ALIASES",
]
