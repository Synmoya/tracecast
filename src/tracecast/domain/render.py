"""Packing and Rendering artifacts: chunk plan, render contract, chunk output.

``node_ref``/``material_ref`` are contract-local debug aliases (e.g. ``N1``,
``M1``) and must never appear in final body text. ``RenderContract`` materials
only carry ``keep``/``condense`` (``omit`` material is excluded from the
contract), enforced on the material record below.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from .base import DomainModel
from .enums import ContentKind, NodeEditOperation, Role, Treatment


class ChunkPlanChunk(DomainModel):
    """One chunk boundary plus its engineering payload.

    ``node_ids`` keep selected-sequence relative order; ``material_ids`` only ever
    reference keep/condense results. No target body word count. A single
    overloaded node must be reported by Packing, never silently split.
    """

    chunk_id: str
    order_index: int = Field(ge=0)
    node_ids: list[str] = Field(min_length=1)
    material_ids: list[str] = Field(default_factory=list)
    estimated_material_tokens: int = Field(ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkPlan(DomainModel):
    """Chunk boundaries and engineering payload owned by Packing.

    ``policy`` (the packing policy used) is saved with the artifact.
    """

    plan_id: str
    project_id: str
    selection_plan_id: str
    node_edit_plan_id: str
    policy: dict[str, Any] = Field(default_factory=dict)
    chunks: list[ChunkPlanChunk] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RenderContractNode(DomainModel):
    """A node as projected into a render contract.

    ``role``/``treatment`` are required because a contract only carries accepted,
    to-be-rendered nodes. ``summary``/``directive`` are retained for context but
    are not fact sources; ``narrative_function`` is volatile.
    """

    node_ref: str
    source_node_id: str
    order_in_chunk: int = Field(ge=0)
    role: Role
    treatment: Treatment
    directive: str | None = None
    summary: str | None = None
    narrative_function: str | None = None
    material_refs: list[str] = Field(default_factory=list)


class RenderContractMaterial(DomainModel):
    """A material as projected into a render contract; a permitted fact source.

    ``operation`` is restricted to ``keep``/``condense``. A ``condense`` material
    must carry ``source_material_refs`` lineage (the contract-local mirror of the
    atom's ``source_material_ids``).
    """

    material_ref: str
    source_material_id: str
    node_ref: str
    operation: NodeEditOperation
    content: str
    language: str
    content_kind: ContentKind
    tags: list[str] = Field(default_factory=list)
    source_position: int | None = Field(default=None, ge=0)
    source_material_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_operation(self) -> RenderContractMaterial:
        if self.operation is NodeEditOperation.omit:
            raise ValueError("RenderContract materials cannot be 'omit'")
        if self.operation is NodeEditOperation.condense and not self.source_material_refs:
            raise ValueError("condense material must carry source_material_refs lineage")
        return self


class RenderContract(DomainModel):
    """The renderer's single source of truth for one chunk.

    The contract intentionally omits generated-body continuity state, omitted
    material, unselected nodes, edges, hashes, and storage/extractor metadata.
    Independent render calls receive only code-derived position and the separate
    planned-duplication hints.
    """

    contract_id: str
    chunk_id: str
    order_index: int = Field(ge=0)
    project_id: str
    persona_id: str
    nodes: list[RenderContractNode] = Field(min_length=1)
    materials: list[RenderContractMaterial] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RenderFactContextNodeHint(DomainModel):
    """One sparse, node-owned planned-duplication hint.

    ``source_node_id`` is stable artifact lineage. ``established`` is only a
    description of content or a narrative action already present in the earlier
    ordered plan; it is neither evidence nor an instruction to the Renderer.
    """

    source_node_id: str
    established: str

    @field_validator("source_node_id")
    @classmethod
    def _normalize_source_node_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source_node_id must be non-empty")
        return value

    @field_validator("established")
    @classmethod
    def _normalize_established(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("established must be non-empty")
        return value


class RenderFactContext(DomainModel):
    """Sparse planned-duplication hints grouped by render chunk.

    The preflight call compares each selected node's SUM/directive plan with
    earlier ordered nodes. Only high-confidence repeated content or narrative
    actions become hints. Hints are not evidence, commands, or claims about
    rendered body text.
    """

    chunk_id: str
    order_index: int = Field(ge=0)
    node_hints: list[RenderFactContextNodeHint] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_planned_context(cls, value: Any) -> Any:
        """Read v1/v2 chunk cards without assigning them to specific nodes."""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        v1_fields = {"carry", "established", "prior_chunk_ids"}
        v2_fields = {
            "planned_entry_state",
            "planned_content_overlap",
            "planned_action_overlap",
            "current_node_ids",
        }
        legacy_fields = v1_fields | v2_fields
        present_legacy = {field for field in legacy_fields if field in data}
        if present_legacy and "node_hints" in data:
            raise ValueError("RenderFactContext cannot mix legacy and v3 fields")

        metadata = dict(data.get("metadata") or {})
        if present_legacy:
            prior_semantics = metadata.get("semantics")
            if prior_semantics:
                metadata["legacy_metadata_semantics"] = prior_semantics
            source_version = (
                "render_fact_context_v1"
                if present_legacy.intersection(v1_fields)
                else "render_fact_context_v2"
            )
            legacy_payload = {
                field: data.pop(field)
                for field in sorted(present_legacy)
            }
            metadata["semantics"] = f"legacy_{source_version}"
            metadata["migrated_from"] = source_version
            metadata["legacy_chunk_context"] = legacy_payload
            data["node_hints"] = []
        else:
            metadata.setdefault("semantics", "sparse_node_established_v3")
        data["metadata"] = metadata
        return data

    @model_validator(mode="after")
    def _check_node_hint_limits(self) -> RenderFactContext:
        counts: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        for hint in self.node_hints:
            counts[hint.source_node_id] = counts.get(hint.source_node_id, 0) + 1
            key = (hint.source_node_id, hint.established)
            if key in seen:
                raise ValueError(
                    "RenderFactContext node_hints cannot repeat identical hints "
                    "for one source_node_id"
                )
            seen.add(key)
        if any(count > 3 for count in counts.values()):
            raise ValueError(
                "RenderFactContext allows at most three hints per source_node_id"
            )
        return self


class ChunkArtifact(DomainModel):
    """Code-wrapped body result for one chunk.

    ``text`` is the LLM body verbatim -- no auto-polish or repair. The model never
    emits this record; code wraps the text and computes the hashes.
    """

    chunk_id: str
    order_index: int = Field(ge=0)
    contract_id: str
    text: str
    input_hash: str
    output_hash: str
    generated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
