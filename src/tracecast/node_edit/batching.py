"""Deterministic Node Edit batch packing."""

from __future__ import annotations

from tracecast.control_prompt import ControlPromptLocale
from tracecast.domain import estimate_tokens

from .projection import BatchNode, project_batch, render_node_edit_user_prompt


def batch_nodes(
    accepted: list[BatchNode],
    max_tokens: int,
    max_nodes: int | None,
    *,
    user_prompt_template: str,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> tuple[list[list[BatchNode]], list[str]]:
    """Pack accepted nodes in selected order without splitting a node."""
    batches: list[list[BatchNode]] = []
    over_budget: list[str] = []
    current: list[BatchNode] = []
    for batch_node in accepted:
        if _batch_prompt_tokens(
            [batch_node],
            user_prompt_template,
            control_prompt_locale,
        ) > max_tokens:
            over_budget.append(batch_node.candidate_id)
        exceeds_tokens = bool(current) and (
            _batch_prompt_tokens(
                [*current, batch_node],
                user_prompt_template,
                control_prompt_locale,
            )
            > max_tokens
        )
        exceeds_nodes = bool(current) and max_nodes is not None and len(current) >= max_nodes
        if exceeds_tokens or exceeds_nodes:
            batches.append(current)
            current = []
        current.append(batch_node)
    if current:
        batches.append(current)
    return batches, over_budget


def _batch_prompt_tokens(
    batch: list[BatchNode],
    user_prompt_template: str,
    control_prompt_locale: ControlPromptLocale,
) -> int:
    projection = project_batch(batch)
    user_prompt = render_node_edit_user_prompt(
        template=user_prompt_template,
        projection=projection,
        control_prompt_locale=control_prompt_locale,
    )
    return estimate_tokens(user_prompt)
