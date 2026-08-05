"""Selected-node material editing stage.

The Node Edit stage is the material-editing layer *inside* each accepted candidate
node: it decides which materials are kept,
which contiguous same-source runs are condensed into run-scoped derived
materials, and which are omitted, leaving a complete keep/condense/omit audit
ledger. The model only judges within one node's own pool and emits DSL; this
package owns batch packing, alias projection/expansion, DSL parsing, the complete
partition check, stable condensed-material ids + lineage, the ``NodeEditPlan``
artifact, the edited-material pack, and per-node recovery + reporting.

Driver entry point: :func:`build_node_edit_outputs`, returning the K/C/O plan,
edited materials, and explicit reports.
``call`` is an injected ``RuntimeConfig``/``RunStore`` plus an optional provider
seam (default :func:`tracecast.llm.call_llm`) so a fake provider can be used
without network.
"""

from __future__ import annotations

from .dsl import NodeEditParse, RawNodeBlock, RawOp, parse_node_edit_dsl
from .plan import LlmCall, build_node_edit_outputs
from .projection import BatchNode, BatchProjection, project_batch
from .results import NodeEditBuildOutput
from .vocab import NODE_EDIT_ROUTE, STAGE_NODE_EDIT, report_item

__all__ = [
    # driver entry point
    "build_node_edit_outputs",
    "NodeEditBuildOutput",
    # call seam type
    "LlmCall",
    # projection
    "BatchNode",
    "BatchProjection",
    "project_batch",
    # dsl
    "parse_node_edit_dsl",
    "NodeEditParse",
    "RawNodeBlock",
    "RawOp",
    # protocol + reporting
    "NODE_EDIT_ROUTE",
    "STAGE_NODE_EDIT",
    "report_item",
]
