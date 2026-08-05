"""Prompt-only application of a run-level :class:`NameResolutionPlan`."""

from __future__ import annotations

from tracecast.domain import NameResolutionPlan, RenderContract, RenderFactContext

from .matching import iter_surface_spans


def resolve_text_names(text: str, plan: NameResolutionPlan) -> str:
    """Replace aliases once while preserving explicitly protected longer spans."""

    return _resolve_text_names(text, plan, operator_only=False)


def _resolve_text_names(
    text: str,
    plan: NameResolutionPlan,
    *,
    operator_only: bool,
) -> str:
    replacements: dict[str, str] = {}
    for rule in plan.rules:
        source = rule.metadata.get("source")
        aliases = rule.aliases
        if operator_only:
            if source == "operator+model":
                raw_authorized = rule.metadata.get("operator_aliases")
                authorized = (
                    set(raw_authorized) if isinstance(raw_authorized, list) else set()
                )
                aliases = [alias for alias in rule.aliases if alias in authorized]
            elif source != "operator" and rule.metadata.get("pinned") is not True:
                aliases = []
        for alias in aliases:
            if alias != rule.stable_name:
                replacements[alias] = rule.stable_name
    candidates: list[tuple[int, int, str | None]] = []
    for alias, stable_name in replacements.items():
        candidates.extend(
            (start, end, stable_name)
            for start, end in iter_surface_spans(text, alias)
        )
    protections = [] if operator_only else plan.protected_surfaces
    for surface in protections:
        candidates.extend(
            (start, end, None)
            for start, end in iter_surface_spans(text, surface)
        )
    # At one start position, the longest literal owns the span.  Protection wins
    # an otherwise exact tie, though plan validation normally prevents a surface
    # from being both protected and replaceable.
    candidates.sort(
        key=lambda item: (
            item[0],
            -(item[1] - item[0]),
            item[2] is not None,
        )
    )
    selected: list[tuple[int, int, str | None]] = []
    cursor = -1
    for start, end, replacement in candidates:
        if start < cursor:
            continue
        selected.append((start, end, replacement))
        cursor = end
    if not selected:
        return text
    out: list[str] = []
    cursor = 0
    for start, end, replacement in selected:
        out.append(text[cursor:start])
        out.append(text[start:end] if replacement is None else replacement)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def resolve_contract_names(
    contract: RenderContract,
    plan: NameResolutionPlan,
) -> RenderContract:
    """Return a prompt-only contract copy with name-table aliases resolved."""

    _check_contract(contract, plan)
    materials = [
        material.model_copy(
            update={"content": resolve_text_names(material.content, plan)}
        )
        for material in contract.materials
    ]
    nodes = [
        node.model_copy(
            update={
                "directive": (
                    _resolve_text_names(node.directive, plan, operator_only=True)
                    if node.directive
                    else None
                )
            }
        )
        for node in contract.nodes
    ]
    return contract.model_copy(update={"materials": materials, "nodes": nodes})


def resolve_fact_context_names(
    context: RenderFactContext | None,
    plan: NameResolutionPlan,
    *,
    contract: RenderContract,
) -> RenderFactContext | None:
    """Return a prompt-only fact-context copy using the same stable names."""

    _check_contract(contract, plan)
    if context is None:
        return None
    if (
        context.chunk_id != contract.chunk_id
        or context.order_index != contract.order_index
    ):
        raise ValueError("fact context does not match its render contract")
    source_node_ids = {node.source_node_id for node in contract.nodes}
    foreign_hints = sorted(
        {
            hint.source_node_id
            for hint in context.node_hints
            if hint.source_node_id not in source_node_ids
        }
    )
    if foreign_hints:
        raise ValueError(
            "fact context references nodes outside its render contract: "
            + ", ".join(foreign_hints)
        )
    return context.model_copy(
        update={
            "node_hints": [
                hint.model_copy(
                    update={
                        "established": _resolve_text_names(
                            hint.established,
                            plan,
                            operator_only=True,
                        )
                    }
                )
                for hint in context.node_hints
            ],
        }
    )


def _check_contract(contract: RenderContract, plan: NameResolutionPlan) -> None:
    if contract.project_id != plan.project_id:
        raise ValueError("NameResolutionPlan project does not match contract")
    if contract.contract_id not in plan.contract_ids:
        raise ValueError("render contract is not covered by NameResolutionPlan")
