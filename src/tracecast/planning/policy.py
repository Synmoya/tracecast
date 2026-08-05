"""Planning budget interpretation.

This module owns the small deterministic policy adapter between RuntimeConfig
and the graph builder.  It contains no model calls or artifact compilation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tracecast.storage import RuntimeConfig

from .vocab import STAGE_PLANNING

DEFAULT_TARGET_MATERIAL_TOKENS_PER_NODE = 650
DEFAULT_MAX_FINAL_NODE_COUNT = 170


def planning_budgets(config: RuntimeConfig) -> Mapping[str, Any]:
    return config.stage_budgets.get(STAGE_PLANNING, {})


def target_material_tokens_per_node(budgets: Mapping[str, Any]) -> int:
    raw = budgets.get(
        "target_material_tokens_per_node",
        DEFAULT_TARGET_MATERIAL_TOKENS_PER_NODE,
    )
    value = int(raw)
    return value if value > 0 else DEFAULT_TARGET_MATERIAL_TOKENS_PER_NODE


def max_final_node_count(budgets: Mapping[str, Any]) -> int:
    raw = budgets.get("max_final_node_count", DEFAULT_MAX_FINAL_NODE_COUNT)
    value = int(raw)
    return value if value > 0 else DEFAULT_MAX_FINAL_NODE_COUNT


def target_node_count(
    budgets: Mapping[str, Any],
    token_ratio_node_count: int,
) -> int:
    """Return the effective compression target.

    A positive explicit ``target_final_node_count`` replaces the token-ratio
    total for one run. ``max_final_node_count`` is applied only after each
    planning window has derived its own token-ratio target. Model output can
    still miss the resulting target and is reported by the graph builder.
    """
    explicit = budgets.get("target_final_node_count")
    explicit_value = int(explicit) if explicit is not None else None
    if explicit_value is not None and explicit_value > 0:
        desired = explicit_value
    else:
        desired = max(0, token_ratio_node_count)
    return min(desired, max_final_node_count(budgets))


def compression_max_rounds(budgets: Mapping[str, Any]) -> int:
    return max(0, int(budgets.get("max_compression_rounds", 0)))
