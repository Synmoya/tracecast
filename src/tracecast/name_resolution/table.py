"""Single-call full-material Name Table projection, call, and parsing."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tracecast.control_prompt import ControlPromptLocale
from tracecast.domain import AuditReportItem, LlmCallRecord, LlmWorkUnitRef, estimate_tokens
from tracecast.llm import LlmTransportError, call_llm
from tracecast.prompts import (
    load_system_prompt,
    load_user_prompt,
    substitute_prompt_blocks,
)
from tracecast.storage import RunStore, RuntimeConfig

from .compiler import ProposedRule, build_report
from .corpus import SelectedMaterial, render_materials_block

NAME_TABLE_ROUTE = "name_table"
TABLE_BUDGET_KEY = "table_max_input_tokens"
MAX_RULES_KEY = "max_rules"
DEFAULT_TABLE_MAX_INPUT_TOKENS = 49152
DEFAULT_MAX_RULES = 256
MAX_ALIASES_PER_RULE = 16

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


@dataclass(frozen=True)
class NameTableRequest:
    system_prompt: str
    user_prompt: str
    prompt_version: str
    input_refs: list[str]
    input_tokens: int
    material_count: int
    max_rules: int


@dataclass(frozen=True)
class NameTableCallResult:
    proposals: list[ProposedRule]
    protected_surfaces: list[str]
    reports: list[AuditReportItem]
    call_count: int
    protocol_version: str
    input_tokens: int
    material_count: int
    over_budget: bool
    call_id: str | None = None
    raw_response_path: str | None = None


def prepare_name_table_request(
    materials: Sequence[SelectedMaterial],
    *,
    output_language: str,
    config: RuntimeConfig,
    control_prompt_locale: ControlPromptLocale,
) -> NameTableRequest | None:
    if not materials:
        return None
    system_prompt, system_version = load_system_prompt(
        NAME_TABLE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    template, user_version = load_user_prompt(
        NAME_TABLE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    max_rules = table_max_rules(config)
    user_prompt = substitute_prompt_blocks(
        template,
        {
            "output_language": output_language,
            "max_rules": str(max_rules),
            "max_aliases_per_rule": str(MAX_ALIASES_PER_RULE),
            "materials_block": render_materials_block(materials),
        },
        prompt_name="name-table",
    )
    return NameTableRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        prompt_version=f"{system_version}+{user_version}",
        input_refs=[material.source_material_id for material in materials],
        input_tokens=estimate_tokens(system_prompt) + estimate_tokens(user_prompt),
        material_count=len(materials),
        max_rules=max_rules,
    )


def run_name_table(
    request: NameTableRequest | None,
    *,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall = call_llm,
) -> NameTableCallResult:
    if request is None:
        return NameTableCallResult([], [], [], 0, "not-used", 0, 0, False)
    budget = table_input_budget(config)
    if request.input_tokens > budget:
        return NameTableCallResult(
            [],
            [],
            [
                build_report(
                    "name_table_over_budget",
                    f"full selected-material Name Table input is {request.input_tokens} "
                    f"tokens, above {budget}; automatic editing was skipped",
                    artifact_ref="name_resolution",
                )
            ],
            0,
            request.prompt_version,
            request.input_tokens,
            request.material_count,
            True,
        )
    try:
        text, record = call(
            operation_route=NAME_TABLE_ROUTE,
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            config=config,
            store=store,
            prompt_version=request.prompt_version,
            work_unit=LlmWorkUnitRef(
                kind="name_table",
                unit_id="name-table",
                input_refs=request.input_refs,
                metadata={
                    "material_count": request.material_count,
                    "max_rules": request.max_rules,
                },
            ),
        )
    except LlmTransportError as exc:
        return NameTableCallResult(
            [],
            [],
            [
                build_report(
                    "name_table_call_failed",
                    "full-material Name Table call failed; only operator overrides "
                    "were compiled",
                    artifact_ref="name_resolution",
                    call_id=exc.record.call_id,
                    raw_response_path=exc.record.raw_response_path,
                )
            ],
            1,
            request.prompt_version,
            request.input_tokens,
            request.material_count,
            False,
            exc.record.call_id,
            exc.record.raw_response_path,
        )

    proposals, protections, reports = _parse_name_table(
        text,
        max_rules=request.max_rules,
        call_id=record.call_id,
        raw_response_path=record.raw_response_path,
    )
    return NameTableCallResult(
        proposals,
        protections,
        reports,
        1,
        request.prompt_version,
        request.input_tokens,
        request.material_count,
        False,
        record.call_id,
        record.raw_response_path,
    )


def table_input_budget(config: RuntimeConfig) -> int:
    value = (config.stage_budgets.get("name_resolution") or {}).get(
        TABLE_BUDGET_KEY
    )
    return _positive_int(value, DEFAULT_TABLE_MAX_INPUT_TOKENS)


def table_max_rules(config: RuntimeConfig) -> int:
    value = (config.stage_budgets.get("name_resolution") or {}).get(MAX_RULES_KEY)
    return _positive_int(value, DEFAULT_MAX_RULES)


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, int | float) and not isinstance(value, bool):
        coerced = int(value)
        if coerced > 0:
            return coerced
    return default


def _parse_name_table(
    text: str,
    *,
    max_rules: int,
    call_id: str,
    raw_response_path: str | None,
) -> tuple[list[ProposedRule], list[str], list[AuditReportItem]]:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        return [], [], [
            _invalid_output_report(
                f"model output is not one complete JSON object: {exc}",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        ]
    if not isinstance(payload, dict):
        return [], [], [
            _invalid_output_report(
                "model output must be a JSON object",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        ]
    if set(payload) != {"replacements", "protected_surfaces"}:
        return [], [], [
            _invalid_output_report(
                "JSON object must contain exactly replacements and protected_surfaces",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        ]
    raw_entries = payload["replacements"]
    raw_protections = payload["protected_surfaces"]
    if not isinstance(raw_entries, list) or not isinstance(raw_protections, list):
        return [], [], [
            _invalid_output_report(
                "replacements and protected_surfaces must both be arrays",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        ]
    if len(raw_entries) > max_rules or len(raw_protections) > max_rules:
        return [], [], [
            _invalid_output_report(
                f"output exceeds the configured maximum of {max_rules} entries",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        ]

    proposals: list[ProposedRule] = []
    reports: list[AuditReportItem] = []
    for index, raw_entry in enumerate(raw_entries):
        parsed, error = _parse_entry(raw_entry, call_id=call_id)
        if error is not None:
            reports.append(
                build_report(
                    "name_table_invalid_entry",
                    f"replacement entry {index} {error}; the entry was omitted",
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                    metadata={"entry_index": index},
                )
            )
        elif parsed is not None:
            proposals.append(parsed)

    protections: list[str] = []
    for index, value in enumerate(raw_protections):
        if not isinstance(value, str) or not value.strip():
            reports.append(
                build_report(
                    "name_table_invalid_protection",
                    f"protected surface {index} must be a non-empty string; it was omitted",
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                    metadata={"protection_index": index},
                )
            )
            continue
        protections.append(value.strip())
    return proposals, list(dict.fromkeys(protections)), reports


def _parse_entry(
    value: Any,
    *,
    call_id: str,
) -> tuple[ProposedRule | None, str | None]:
    if not isinstance(value, Mapping) or set(value) != {"aliases", "stable_name"}:
        return None, "must contain exactly aliases and stable_name"
    aliases = value["aliases"]
    stable_name = value["stable_name"]
    if not isinstance(aliases, list) or not 1 <= len(aliases) <= MAX_ALIASES_PER_RULE:
        return None, f"requires 1..{MAX_ALIASES_PER_RULE} aliases"
    if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
        return None, "aliases must be non-empty strings"
    cleaned_aliases = tuple(alias.strip() for alias in aliases)
    if len(cleaned_aliases) != len(set(cleaned_aliases)):
        return None, "aliases must be unique within the entry"
    if not isinstance(stable_name, str) or not stable_name.strip():
        return None, "stable_name must be a non-empty string"
    return (
        ProposedRule(
            aliases=cleaned_aliases,
            stable_name=stable_name.strip(),
            source="model",
            call_id=call_id,
        ),
        None,
    )


def _invalid_output_report(
    message: str,
    *,
    call_id: str,
    raw_response_path: str | None,
) -> AuditReportItem:
    return build_report(
        "name_table_invalid_output",
        message + "; only operator overrides were compiled",
        artifact_ref="name_resolution",
        call_id=call_id,
        raw_response_path=raw_response_path,
    )
