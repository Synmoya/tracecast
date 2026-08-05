"""Compile sparse node-level duplication hints from the ordered plan.

The preflight model sees selected nodes in their final render-chunk packing.
Each node exposes only its planned summary and optional narrative directive;
neither is evidence. The model may emit a hint only when the current node
repeats content or a narrative action already scheduled by an earlier chunk.
Missing aliases are normal: the output is deliberately sparse and has no
semantic repair pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    AuditReportItem,
    LlmCallRecord,
    LlmWorkUnitRef,
    RenderContract,
    RenderContractNode,
    RenderFactContext,
    RenderFactContextNodeHint,
    estimate_tokens,
    issue_id,
)
from tracecast.llm import LlmTransportError, call_llm
from tracecast.prompts import load_system_prompt, load_user_prompt, substitute_prompt_blocks
from tracecast.storage import RunStore, RuntimeConfig

FACT_CONTEXT_ROUTE = "render_fact_context_dsl"
STAGE_RENDERING = "rendering"
SPARSE_CONTEXT_SEMANTICS = "sparse_node_established_v3"
_PARSE_KIND = "fact_context_parse_error"
_LENGTH_KIND = "fact_context_length_warning"
_HINT_RE = re.compile(
    r"^HINT\s+(?P<alias>C\d{3,}N\d{3,})\s+\|\s*(?P<text>.*)$"
)
_NODE_ALIAS_IN_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:C\d{3,}N\d{3,}|C\d{3,}|N\d{3,})(?![A-Za-z0-9_])"
)
_MAX_HINTS_PER_NODE = 3

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


@dataclass(frozen=True)
class FactContextBudgets:
    """Chunk-relative diagnostic lengths resolved from rendering budgets."""

    ratio: float = 0.10
    min_tokens: int = 80
    max_tokens: int = 320

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> FactContextBudgets:
        raw = config.stage_budgets.get(STAGE_RENDERING, {})
        return cls(
            ratio=float(raw.get("fact_context_ratio", cls.ratio)),
            min_tokens=int(raw.get("fact_context_min_tokens", cls.min_tokens)),
            max_tokens=int(raw.get("fact_context_max_tokens", cls.max_tokens)),
        )

    def __post_init__(self) -> None:
        if not 0 < self.ratio <= 1:
            raise ValueError("FactContextBudgets.ratio must be in (0, 1]")
        if self.min_tokens <= 0 or self.max_tokens <= 0:
            raise ValueError("FactContextBudgets token limits must be positive")
        if self.max_tokens < self.min_tokens:
            raise ValueError("fact context max_tokens must be >= min_tokens")

    def limit_for(self, contract: RenderContract) -> int:
        material_tokens = sum(estimate_tokens(item.content) for item in contract.materials)
        proportional = int(material_tokens * self.ratio)
        return min(self.max_tokens, max(self.min_tokens, proportional))


@dataclass(frozen=True)
class FactContextResult:
    contexts: list[RenderFactContext]
    reports: list[AuditReportItem]


@dataclass(frozen=True)
class _PlannedNode:
    alias: str
    chunk_alias: str
    node_alias: str
    contract: RenderContract
    node: RenderContractNode


def project_fact_context_outline(
    contracts: Iterable[RenderContract],
) -> str:
    """Project the packed node plan containing only SUM and directive."""

    ordered = sorted(contracts, key=lambda item: item.order_index)
    planned = _planned_nodes(ordered)
    planned_by_chunk: dict[str, list[_PlannedNode]] = {}
    for item in planned:
        planned_by_chunk.setdefault(item.contract.chunk_id, []).append(item)

    lines = ["ORDERED_CHUNK_PLAN"]
    for contract in ordered:
        chunk_nodes = planned_by_chunk.get(contract.chunk_id, [])
        lines.append(f"CHUNK {chunk_nodes[0].chunk_alias}")
        for item in chunk_nodes:
            summary = item.node.summary.strip() if item.node.summary else "none"
            directive = (
                item.node.directive.strip() if item.node.directive else "none"
            )
            lines.extend(
                (
                    f"NODE {item.node_alias}",
                    f"CONTENT_SCHEDULED_HERE {summary}",
                    f"ACTION_SCHEDULED_HERE {directive}",
                    "ENDNODE",
                )
            )
        lines.append("ENDCHUNK")
    lines.append("END_ORDERED_CHUNK_PLAN")
    return "\n".join(lines)


def render_fact_context_user_prompt(
    template: str,
    outline: str,
    output_language: str,
) -> str:
    return substitute_prompt_blocks(
        template,
        build_fact_context_blocks(outline, output_language),
        prompt_name="fact-context",
    )


def build_fact_context_blocks(outline: str, output_language: str) -> dict[str, str]:
    """Return the exact inspectable blocks for the run-wide preflight call."""

    if not output_language.strip():
        raise ValueError("output_language must be non-empty")
    return {"node_outline": outline, "output_language": output_language}


def build_fact_contexts(
    contracts: Sequence[RenderContract],
    *,
    config: RuntimeConfig,
    store: RunStore,
    output_language: str,
    call: LlmCall = call_llm,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> FactContextResult:
    """Generate sparse hints in one run-wide call; malformed lines are dropped."""

    control_prompt_locale = coerce_control_prompt_locale(control_prompt_locale)
    ordered = sorted(contracts, key=lambda item: item.order_index)
    if not ordered:
        return FactContextResult(contexts=[], reports=[])
    _validate_contract_order(ordered)
    budgets = FactContextBudgets.from_config(config)
    planned = _planned_nodes(ordered)
    if len(ordered) == 1:
        return FactContextResult(
            contexts=_compile_contexts(
                ordered,
                planned,
                {},
                budgets=budgets,
                reason="no_earlier_node",
            ),
            reports=[],
        )

    route = config.route(FACT_CONTEXT_ROUTE)
    if route.temperature != 0:
        raise ValueError("render_fact_context_dsl temperature must be 0")

    system_prompt, system_version = load_system_prompt(
        FACT_CONTEXT_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    template, user_version = load_user_prompt(
        FACT_CONTEXT_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    prompt_version = f"{system_version}+{user_version}"
    outline = project_fact_context_outline(ordered)
    user_prompt = render_fact_context_user_prompt(template, outline, output_language)
    try:
        raw, record = call(
            operation_route=FACT_CONTEXT_ROUTE,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            config=config,
            store=store,
            prompt_version=prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="render_fact_context",
                unit_id="fact-context",
                input_refs=list(
                    dict.fromkeys(item.node.source_node_id for item in planned)
                ),
                metadata={
                    "attempt": 1,
                    "output_language": output_language,
                    "semantics": SPARSE_CONTEXT_SEMANTICS,
                },
            ),
        )
    except LlmTransportError as exc:
        record = exc.record
        contexts = _compile_contexts(
            ordered,
            planned,
            {},
            budgets=budgets,
            prompt_version=prompt_version,
            call_id=record.call_id,
            reason="call_failed",
        )
        message = "run-wide sparse fact-context call failed; all chunks use empty hints"
        return FactContextResult(
            contexts=contexts,
            reports=[
                _fact_context_report(
                    kind="fact_context_call_failed",
                    message=message,
                    trace_refs=[contract.chunk_id for contract in ordered],
                    record=record,
                )
            ],
        )

    hints, issues = _recover_hints(raw, planned)
    contexts = _compile_contexts(
        ordered,
        planned,
        hints,
        budgets=budgets,
        prompt_version=prompt_version,
        call_id=record.call_id,
    )
    reports = [
        _fact_context_report(
            kind=_PARSE_KIND,
            message=message,
            trace_refs=trace_refs,
            record=record,
        )
        for message, trace_refs in issues
    ]
    checked_contexts: list[RenderFactContext] = []
    for context in contexts:
        limit = budgets.limit_for(_contract_by_chunk(ordered, context.chunk_id))
        context_tokens = int(context.metadata.get("context_tokens", 0))
        if context_tokens <= limit:
            checked_contexts.append(context)
            continue
        context = context.model_copy(
            update={
                "metadata": {
                    **context.metadata,
                    "context_length_warning": True,
                }
            }
        )
        checked_contexts.append(context)
        reports.append(
            _fact_context_report(
                kind=_LENGTH_KIND,
                message=(
                    f"{context.chunk_id} sparse hints use {context_tokens} estimated "
                    f"tokens (diagnostic target {limit}); valid hints were preserved"
                ),
                trace_refs=[context.chunk_id],
                record=record,
            )
        )
    return FactContextResult(contexts=checked_contexts, reports=reports)


def parse_fact_contexts(
    raw: str,
    contracts: Sequence[RenderContract],
    *,
    budgets: FactContextBudgets,
    prompt_version: str,
    call_id: str,
) -> list[RenderFactContext]:
    """Strict parser for callers that require a fully clean sparse response."""

    ordered = sorted(contracts, key=lambda item: item.order_index)
    _validate_contract_order(ordered)
    planned = _planned_nodes(ordered)
    hints, issues = _recover_hints(raw, planned)
    if issues:
        raise ValueError("; ".join(message for message, _refs in issues))
    return _compile_contexts(
        ordered,
        planned,
        hints,
        budgets=budgets,
        prompt_version=prompt_version,
        call_id=call_id,
    )


def _recover_hints(
    raw: str,
    planned: Sequence[_PlannedNode],
) -> tuple[dict[str, list[str]], list[tuple[str, list[str]]]]:
    by_alias = {item.alias: item for item in planned}
    first_chunk_id = planned[0].contract.chunk_id if planned else None
    candidates: dict[str, list[str]] = {}
    issues: list[tuple[str, list[str]]] = []
    all_chunks = list(dict.fromkeys(item.contract.chunk_id for item in planned))

    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = _HINT_RE.fullmatch(line)
        if match is None:
            issues.append(
                (
                    f"line {line_number} is outside sparse HINT DSL and was dropped",
                    all_chunks,
                )
            )
            continue
        alias = match.group("alias")
        text = match.group("text").strip()
        target = by_alias.get(alias)
        if target is None:
            issues.append(
                (
                    f"unknown node alias {alias} at line {line_number} was dropped",
                    all_chunks,
                )
            )
            continue
        if target.contract.chunk_id == first_chunk_id:
            issues.append(
                (
                    f"node alias {alias} is in the first render chunk; hint was dropped",
                    [target.node.source_node_id],
                )
            )
            continue
        if not text or text.casefold() == "none":
            issues.append(
                (
                    f"{alias} has empty/none established text; hint was dropped",
                    [target.node.source_node_id],
                )
            )
            continue
        if _NODE_ALIAS_IN_TEXT_RE.search(text):
            issues.append(
                (
                    f"{alias} established text contains a node alias; hint was dropped",
                    [target.node.source_node_id],
                )
            )
            continue
        candidates.setdefault(alias, []).append(text)

    hints: dict[str, list[str]] = {}
    for alias, texts in candidates.items():
        target = by_alias[alias]
        distinct_texts = list(dict.fromkeys(texts))
        if len(distinct_texts) != len(texts):
            issues.append(
                (
                    f"{alias} repeated identical hint text; duplicate lines were dropped",
                    [target.node.source_node_id],
                )
            )
        if len(distinct_texts) > _MAX_HINTS_PER_NODE:
            issues.append(
                (
                    f"{alias} emitted {len(distinct_texts)} distinct hints; "
                    f"only the first {_MAX_HINTS_PER_NODE} were preserved",
                    [target.node.source_node_id],
                )
            )
        hints[alias] = distinct_texts[:_MAX_HINTS_PER_NODE]
    return hints, issues


def _compile_contexts(
    contracts: Sequence[RenderContract],
    planned: Sequence[_PlannedNode],
    hints: dict[str, list[str]],
    *,
    budgets: FactContextBudgets,
    prompt_version: str | None = None,
    call_id: str | None = None,
    reason: str | None = None,
) -> list[RenderFactContext]:
    planned_by_chunk: dict[str, list[_PlannedNode]] = {}
    for item in planned:
        planned_by_chunk.setdefault(item.contract.chunk_id, []).append(item)

    contexts: list[RenderFactContext] = []
    for contract in contracts:
        node_hints = [
            RenderFactContextNodeHint(
                source_node_id=item.node.source_node_id,
                established=established,
            )
            for item in planned_by_chunk.get(contract.chunk_id, [])
            for established in hints.get(item.alias, [])
        ]
        context_tokens = sum(estimate_tokens(item.established) for item in node_hints)
        metadata: dict[str, Any] = {
            "semantics": SPARSE_CONTEXT_SEMANTICS,
            "hint_count": len(node_hints),
            "context_tokens": context_tokens,
            "max_context_tokens": budgets.limit_for(contract),
        }
        if prompt_version:
            metadata["prompt_version"] = prompt_version
        if call_id:
            metadata["call_id"] = call_id
        if reason:
            metadata["reason"] = reason
        contexts.append(
            RenderFactContext(
                chunk_id=contract.chunk_id,
                order_index=contract.order_index,
                node_hints=node_hints,
                metadata=metadata,
            )
        )
    return contexts


def _planned_nodes(contracts: Iterable[RenderContract]) -> list[_PlannedNode]:
    planned: list[_PlannedNode] = []
    ordered = sorted(contracts, key=lambda item: item.order_index)
    for chunk_index, contract in enumerate(ordered, start=1):
        chunk_alias = f"C{chunk_index:03d}"
        for node_index, node in enumerate(
            sorted(contract.nodes, key=lambda item: item.order_in_chunk),
            start=1,
        ):
            node_alias = f"N{node_index:03d}"
            planned.append(
                _PlannedNode(
                    alias=f"{chunk_alias}{node_alias}",
                    chunk_alias=chunk_alias,
                    node_alias=node_alias,
                    contract=contract,
                    node=node,
                )
            )
    return planned


def _validate_contract_order(contracts: Sequence[RenderContract]) -> None:
    chunk_ids = [contract.chunk_id for contract in contracts]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("fact-context contracts must have unique chunk ids")
    order_indices = [contract.order_index for contract in contracts]
    if len(order_indices) != len(set(order_indices)):
        raise ValueError("fact-context contracts must have unique order_index values")
    source_node_ids = [
        node.source_node_id
        for contract in contracts
        for node in contract.nodes
    ]
    if len(source_node_ids) != len(set(source_node_ids)):
        raise ValueError("fact-context selected nodes must have unique source_node_id values")


def _contract_by_chunk(
    contracts: Sequence[RenderContract],
    chunk_id: str,
) -> RenderContract:
    return next(contract for contract in contracts if contract.chunk_id == chunk_id)


def _fact_context_report(
    *,
    kind: str,
    message: str,
    trace_refs: list[str],
    record: LlmCallRecord,
) -> AuditReportItem:
    artifact_ref = "rendering/fact_contexts.jsonl"
    return AuditReportItem(
        issue_id=issue_id(STAGE_RENDERING, kind, artifact_ref, message),
        stage=STAGE_RENDERING,
        kind=kind,
        level="warning",
        message=message,
        usable=True,
        artifact_ref=artifact_ref,
        trace_refs=trace_refs,
        call_id=record.call_id,
        raw_response_path=record.raw_response_path,
    )
