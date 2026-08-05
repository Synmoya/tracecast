"""Node Edit artifacts: the keep/condense/omit ledger for selected nodes.

The single-record rules the contract calls for live here, where the operation
is known:

- a ``keep`` edit references exactly one original material and produces no new
  material;
- a ``condense`` edit produces a run-scoped derived ``MaterialAtom`` that **must**
  carry ``source_material_ids`` lineage;
- ``omit`` never appears in ``edits`` (it lives in ``omitted``), and an omission
  must record a ``reason`` (no silent drops).

Whether every input material is partitioned exactly once across keep/condense/
omit is a cross-record invariant owned by the Node Edit stage, not enforced here.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .base import DomainModel
from .enums import NodeEditOperation
from .selection import SelectionDecision
from .source import MaterialAtom


class MaterialEdit(DomainModel):
    """One ``keep`` or ``condense`` operation within a node."""

    operation: NodeEditOperation
    source_material_ids: list[str] = Field(min_length=1)
    result_material: MaterialAtom | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _check_operation(self) -> MaterialEdit:
        if self.operation is NodeEditOperation.omit:
            raise ValueError("omit belongs in NodeEditResult.omitted, not edits")
        if self.operation is NodeEditOperation.keep:
            if self.result_material is not None:
                raise ValueError("keep edit must not produce a new material")
            if len(self.source_material_ids) != 1:
                raise ValueError("keep edit references exactly one source material")
        if self.operation is NodeEditOperation.condense:
            if self.result_material is None:
                raise ValueError("condense edit must produce a result_material")
            if not self.result_material.source_material_ids:
                raise ValueError("condensed material must carry source_material_ids lineage")
        return self


class MaterialOmission(DomainModel):
    """One ``omit`` entry: removed from the render path but kept in the ledger."""

    material_id: str
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeEditResult(DomainModel):
    """Material operations for a single accepted node.

    ``selection`` carries the node's accepted :class:`SelectionDecision` so the
    editor sees its role/treatment/directive context.
    """

    candidate_id: str
    selection: SelectionDecision
    edits: list[MaterialEdit] = Field(default_factory=list)
    omitted: list[MaterialOmission] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeEditPlan(DomainModel):
    """The node-edit ledger across all accepted nodes.

    Aggregation order follows ``SelectionPlan.selected_sequence``; batch grouping
    is a call/report concern and is kept out of this record's identity.
    """

    plan_id: str
    project_id: str
    candidate_graph_id: str
    selection_plan_id: str
    node_edits: list[NodeEditResult] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
