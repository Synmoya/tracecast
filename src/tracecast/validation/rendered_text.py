"""Rendered-body hygiene audits."""

from __future__ import annotations

import re

from tracecast.domain import AuditReportItem, ChunkArtifact, RenderContract

from ._common import make_item
from .context import ValidationContext
from .kinds import AuditKind, Level, Stage

_GLOBAL_ID_RE = re.compile(
    r"(?:atom|cand|graph|selplan|nedit|chunkplan|chunk|contract|win|catalog|issue)_[0-9a-f]{8,}"
)
_RENDER_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9])R\d{3,}(?![A-Za-z0-9])")
_PIECE_ALIAS_RE = re.compile(r"(?<![A-Za-z0-9])p\d{4,}(?![A-Za-z0-9])")
_JSON_KEY_RE = re.compile(r'"[A-Za-z_][A-Za-z0-9_]*"\s*:')
_SCHEMA_TOKENS = (
    "source_material_id",
    "material_ref",
    "node_ref",
    "forbidden_claims",
    "ChunkArtifact",
    "RenderContract",
)


def audit_rendered_text(ctx: ValidationContext) -> list[AuditReportItem]:
    """Audit forbidden claims, internal leaks, and verbatim body preservation."""
    items: list[AuditReportItem] = []
    by_chunk = {contract.chunk_id: contract for contract in ctx.contracts}
    for chunk in ctx.chunks:
        items.extend(_audit_one_chunk_text(ctx, chunk, by_chunk.get(chunk.chunk_id)))
    return items


def _audit_one_chunk_text(
    ctx: ValidationContext,
    chunk: ChunkArtifact,
    contract: RenderContract | None,
) -> list[AuditReportItem]:
    items: list[AuditReportItem] = []
    text = chunk.text
    if contract is not None:
        for claim in contract.forbidden_claims:
            if claim and claim in text:
                items.append(
                    make_item(
                        Stage.rendering,
                        AuditKind.forbidden_claim,
                        f"ChunkArtifact {chunk.chunk_id} contains forbidden claim {claim!r}.",
                        artifact_ref=chunk.chunk_id,
                        trace_refs=[f"chunk:{chunk.chunk_id}", f"contract:{contract.contract_id}"],
                    )
                )
    leaks = _detect_id_leaks(text, contract)
    if leaks:
        items.append(
            make_item(
                Stage.rendering,
                AuditKind.internal_id_leak,
                f"ChunkArtifact {chunk.chunk_id} body leaks internal ref(s): {', '.join(leaks)}.",
                artifact_ref=chunk.chunk_id,
                trace_refs=[f"chunk:{chunk.chunk_id}"],
                metadata={"leaked": leaks},
            )
        )
    if _JSON_KEY_RE.search(text) or any(token in text for token in _SCHEMA_TOKENS):
        items.append(
            make_item(
                Stage.rendering,
                AuditKind.json_or_schema_leak,
                f"ChunkArtifact {chunk.chunk_id} body looks like it leaked JSON or a "
                "schema field list.",
                level=Level.warning,
                artifact_ref=chunk.chunk_id,
                trace_refs=[f"chunk:{chunk.chunk_id}"],
            )
        )
    raw = ctx.raw_chunk_text
    if raw is not None and chunk.chunk_id in raw and raw[chunk.chunk_id] != text:
        items.append(
            make_item(
                Stage.rendering,
                AuditKind.text_changed_after_render,
                f"ChunkArtifact {chunk.chunk_id} text differs from the verbatim LLM body.",
                artifact_ref=chunk.chunk_id,
                trace_refs=[f"chunk:{chunk.chunk_id}"],
            )
        )
    return items


def _detect_id_leaks(text: str, contract: RenderContract | None) -> list[str]:
    found: set[str] = set()
    found.update(_GLOBAL_ID_RE.findall(text))
    found.update(_RENDER_ALIAS_RE.findall(text))
    found.update(_PIECE_ALIAS_RE.findall(text))
    if contract is not None:
        for node in contract.nodes:
            if node.node_ref and node.node_ref in text:
                found.add(node.node_ref)
        for material in contract.materials:
            if material.material_ref and material.material_ref in text:
                found.add(material.material_ref)
    return sorted(found)
