"""Deterministic material and occurrence views for run-level name editing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tracecast.domain import RenderContract, RenderFactContext


@dataclass(frozen=True)
class SelectedMaterial:
    source_material_id: str
    content: str
    chunk_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExposureItem:
    content: str
    chunk_id: str
    audit_ref: str


def collect_selected_materials(
    contracts: Sequence[RenderContract],
) -> tuple[SelectedMaterial, ...]:
    """Return each selected material once in first render order."""

    rows: dict[str, tuple[str, list[str]]] = {}
    order: list[str] = []
    for contract in sorted(contracts, key=lambda item: item.order_index):
        for material in contract.materials:
            if not material.content.strip():
                continue
            content = material.content
            existing = rows.get(material.source_material_id)
            if existing is None:
                rows[material.source_material_id] = (content, [contract.chunk_id])
                order.append(material.source_material_id)
                continue
            existing_content, chunk_ids = existing
            if existing_content != content:
                raise ValueError(
                    "selected material id has conflicting content: "
                    + material.source_material_id
                )
            if contract.chunk_id not in chunk_ids:
                chunk_ids.append(contract.chunk_id)
    return tuple(
        SelectedMaterial(
            source_material_id=material_id,
            content=rows[material_id][0],
            chunk_ids=tuple(rows[material_id][1]),
        )
        for material_id in order
    )


def collect_exposure_items(
    contracts: Sequence[RenderContract],
    fact_contexts: Sequence[RenderFactContext],
) -> tuple[ExposureItem, ...]:
    """Return every render-visible field used for override matching and trace."""

    facts_by_chunk = {context.chunk_id: context for context in fact_contexts}
    items: list[ExposureItem] = []
    for contract in sorted(contracts, key=lambda item: item.order_index):
        for material in contract.materials:
            if material.content.strip():
                items.append(
                    ExposureItem(
                        content=material.content,
                        chunk_id=contract.chunk_id,
                        audit_ref=material.source_material_id,
                    )
                )
        for node in sorted(contract.nodes, key=lambda item: item.order_in_chunk):
            if node.directive and node.directive.strip():
                items.append(
                    ExposureItem(
                        content=node.directive,
                        chunk_id=contract.chunk_id,
                        audit_ref=f"directive:{node.source_node_id}",
                    )
                )
        context = facts_by_chunk.get(contract.chunk_id)
        if context is None:
            continue
        for hint in context.node_hints:
            if hint.established.strip():
                items.append(
                    ExposureItem(
                        content=hint.established,
                        chunk_id=contract.chunk_id,
                        audit_ref=f"fact:{hint.source_node_id}",
                    )
                )
    return tuple(items)


def render_materials_block(materials: Sequence[SelectedMaterial]) -> str:
    """Render opaque, clearly delimited evidence without persistent aliases."""

    blocks: list[str] = []
    for index, material in enumerate(materials, 1):
        blocks.extend(
            [
                f"<<<MATERIAL {index:04d}>>>",
                material.content,
                "<<<END MATERIAL>>>",
            ]
        )
    return "\n\n".join(blocks)
