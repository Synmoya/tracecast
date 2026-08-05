"""Orchestrate one-shot full-material Name Table compilation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    AuditReportItem,
    LlmCallRecord,
    NameOverride,
    NameResolutionPlan,
    RenderContract,
    RenderFactContext,
    name_resolution_plan_id,
)
from tracecast.llm import call_llm
from tracecast.storage import RunStore, RuntimeConfig, canonical_json, sha256_json

from .compiler import NameResolutionError, compile_name_table
from .corpus import collect_exposure_items, collect_selected_materials
from .policy import DEFAULT_NAME_RESOLUTION_MODE, NameResolutionMode
from .table import (
    DEFAULT_TABLE_MAX_INPUT_TOKENS,
    NAME_TABLE_ROUTE,
    prepare_name_table_request,
    run_name_table,
    table_input_budget,
)

__all__ = [
    "DEFAULT_TABLE_MAX_INPUT_TOKENS",
    "NAME_TABLE_ROUTE",
    "NameResolutionError",
    "NameResolutionInputEstimate",
    "NameResolutionResult",
    "build_name_resolution",
    "estimate_name_resolution_inputs",
]

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


@dataclass(frozen=True)
class NameResolutionResult:
    plan: NameResolutionPlan
    reports: list[AuditReportItem]


@dataclass(frozen=True)
class NameResolutionInputEstimate:
    table_calls: int
    table_input_tokens: int
    projected_input_tokens: int
    material_count: int
    over_budget: bool

    @property
    def total_input_tokens(self) -> int:
        return self.table_input_tokens


def estimate_name_resolution_inputs(
    contracts: Sequence[RenderContract],
    *,
    output_language: str,
    config: RuntimeConfig,
    mode: NameResolutionMode = DEFAULT_NAME_RESOLUTION_MODE,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> NameResolutionInputEstimate:
    if not output_language.strip():
        raise ValueError("output_language must be non-empty")
    mode = NameResolutionMode(mode)
    if mode is NameResolutionMode.overrides_only:
        return NameResolutionInputEstimate(0, 0, 0, 0, False)
    locale = coerce_control_prompt_locale(control_prompt_locale)
    materials = collect_selected_materials(
        sorted(contracts, key=lambda item: item.order_index)
    )
    request = prepare_name_table_request(
        materials,
        output_language=output_language,
        config=config,
        control_prompt_locale=locale,
    )
    if request is None:
        return NameResolutionInputEstimate(0, 0, 0, 0, False)
    over_budget = request.input_tokens > table_input_budget(config)
    return NameResolutionInputEstimate(
        table_calls=0 if over_budget else 1,
        table_input_tokens=0 if over_budget else request.input_tokens,
        projected_input_tokens=request.input_tokens,
        material_count=request.material_count,
        over_budget=over_budget,
    )


def build_name_resolution(
    contracts: Sequence[RenderContract],
    *,
    project_id: str,
    output_language: str,
    mode: NameResolutionMode = DEFAULT_NAME_RESOLUTION_MODE,
    overrides: Sequence[NameOverride] = (),
    fact_contexts: Sequence[RenderFactContext] = (),
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall = call_llm,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> NameResolutionResult:
    if not output_language.strip():
        raise ValueError("output_language must be non-empty")
    mode = NameResolutionMode(mode)
    locale = coerce_control_prompt_locale(control_prompt_locale)
    ordered = sorted(contracts, key=lambda item: item.order_index)
    _validate_inputs(ordered, project_id=project_id, fact_contexts=fact_contexts)
    contract_ids = [contract.contract_id for contract in ordered]
    materials = collect_selected_materials(ordered)
    exposure_items = collect_exposure_items(ordered, fact_contexts)

    if mode is NameResolutionMode.name_table:
        request = prepare_name_table_request(
            materials,
            output_language=output_language,
            config=config,
            control_prompt_locale=locale,
        )
        table_result = run_name_table(
            request,
            config=config,
            store=store,
            call=call,
        )
    else:
        table_result = run_name_table(None, config=config, store=store, call=call)

    compiled = compile_name_table(
        table_result.proposals,
        table_result.protected_surfaces,
        overrides=overrides,
        output_language=output_language,
        project_id=project_id,
        materials=materials,
        exposure_items=exposure_items,
        call_id=table_result.call_id,
        raw_response_path=table_result.raw_response_path,
    )
    reports = [*table_result.reports, *compiled.reports]
    input_hash = "sha256:" + sha256_json(
        {
            "project_id": project_id,
            "output_language": output_language,
            "mode": mode.value,
            "contracts": [_contract_name_input(contract) for contract in ordered],
            "fact_contexts": [
                {
                    "chunk_id": context.chunk_id,
                    "order_index": context.order_index,
                    "node_hints": [
                        {
                            "source_node_id": hint.source_node_id,
                            "established": hint.established,
                        }
                        for hint in context.node_hints
                    ],
                }
                for context in sorted(fact_contexts, key=lambda item: item.order_index)
            ],
            "overrides": sorted(
                [
                    override.model_dump(mode="json")
                    for override in overrides
                    if override.output_language == output_language
                ],
                key=canonical_json,
            ),
        }
    )
    rules = compiled.rules
    protected = compiled.protected_surfaces
    plan = NameResolutionPlan(
        plan_id=name_resolution_plan_id(
            project_id,
            output_language,
            input_hash,
            contract_ids,
            [(rule.rule_id, rule.aliases, rule.stable_name) for rule in rules]
            + [("protected", protected)],
        ),
        project_id=project_id,
        output_language=output_language,
        input_hash=input_hash,
        contract_ids=contract_ids,
        rules=rules,
        protected_surfaces=protected,
        metadata={
            "producer": "name_resolution.build_name_resolution",
            "mode": mode.value,
            "table_protocol": (
                table_result.protocol_version
                if mode is NameResolutionMode.name_table
                else None
            ),
            "table_calls": table_result.call_count,
            "table_input_tokens": table_result.input_tokens,
            "table_over_budget": table_result.over_budget,
            "material_count": table_result.material_count,
            "proposal_entries": len(table_result.proposals),
            "accepted_model_rules": compiled.accepted_model_rules,
            "protected_surfaces": len(protected),
            "rule_count": len(rules),
        },
    )
    return NameResolutionResult(plan=plan, reports=reports)


def _validate_inputs(
    contracts: Sequence[RenderContract],
    *,
    project_id: str,
    fact_contexts: Sequence[RenderFactContext],
) -> None:
    if any(contract.project_id != project_id for contract in contracts):
        raise ValueError("all name-resolution contracts must match project_id")
    contract_ids = [contract.contract_id for contract in contracts]
    if len(contract_ids) != len(set(contract_ids)):
        raise ValueError("name-resolution contracts must have unique ids")
    chunk_ids = [contract.chunk_id for contract in contracts]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("name-resolution contracts must have unique chunk ids")
    fact_by_chunk = {context.chunk_id: context for context in fact_contexts}
    if len(fact_by_chunk) != len(fact_contexts):
        raise ValueError("name-resolution fact contexts must have unique chunk ids")
    extra_facts = sorted(set(fact_by_chunk) - set(chunk_ids))
    if extra_facts:
        raise ValueError("fact contexts reference unknown chunks: " + ", ".join(extra_facts))
    for contract in contracts:
        context = fact_by_chunk.get(contract.chunk_id)
        if context is not None and context.order_index != contract.order_index:
            raise ValueError(f"fact context order mismatch for {contract.chunk_id}")
        if context is None:
            continue
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
                f"fact context for {contract.chunk_id} references nodes outside "
                f"that chunk: {', '.join(foreign_hints)}"
            )


def _contract_name_input(contract: RenderContract) -> dict[str, Any]:
    return {
        "contract_id": contract.contract_id,
        "chunk_id": contract.chunk_id,
        "order_index": contract.order_index,
        "materials": [
            {
                "source_material_id": material.source_material_id,
                "content": material.content,
            }
            for material in contract.materials
        ],
        "directives": [
            {
                "source_node_id": node.source_node_id,
                "directive": node.directive,
            }
            for node in contract.nodes
            if node.directive
        ],
    }
