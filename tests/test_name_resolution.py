"""Independent acceptance tests for one-shot full-material Name Table."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tracecast.domain import (
    ContentKind,
    LlmCallRecord,
    NameOverride,
    NameResolutionPlan,
    NameResolutionRule,
    NodeEditOperation,
    ProviderStatus,
    RenderContract,
    RenderContractMaterial,
    RenderContractNode,
    RenderFactContext,
    RenderFactContextNodeHint,
    Role,
    Treatment,
)
from tracecast.llm import LlmTransportError
from tracecast.name_resolution import (
    NAME_TABLE_ROUTE,
    NameResolutionError,
    NameResolutionMode,
    build_name_resolution,
    estimate_name_resolution_inputs,
    resolve_contract_names,
    resolve_fact_context_names,
    resolve_text_names,
)
from tracecast.storage import RunStore, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def _config(**name_budget: int | float) -> RuntimeConfig:
    config = RuntimeConfig.load(
        REPO_ROOT / "configs" / "runtime.example.json", load_env=False
    )
    if not name_budget:
        return config
    return config.model_copy(
        update={
            "stage_budgets": {
                **config.stage_budgets,
                "name_resolution": {
                    **config.stage_budgets.get("name_resolution", {}),
                    **name_budget,
                },
            }
        }
    )


def _contract(
    order: int,
    *,
    material_text: str,
    directive: str | None = None,
    material_id: str | None = None,
) -> RenderContract:
    number = order + 1
    return RenderContract(
        contract_id=f"contract_{number}",
        chunk_id=f"chunk_{number}",
        order_index=order,
        project_id="proj",
        persona_id="host",
        nodes=[
            RenderContractNode(
                node_ref="N1",
                source_node_id=f"candidate_{number}",
                order_in_chunk=0,
                role=Role.core,
                treatment=Treatment.paraphrase,
                directive=directive,
                material_refs=["R001"],
            )
        ],
        materials=[
            RenderContractMaterial(
                material_ref="R001",
                source_material_id=material_id or f"material_{number}",
                node_ref="N1",
                operation=NodeEditOperation.keep,
                content=material_text,
                language="en",
                content_kind=ContentKind.narration,
            )
        ],
    )


def _context(contract: RenderContract, text: str) -> RenderFactContext:
    return RenderFactContext(
        chunk_id=contract.chunk_id,
        order_index=contract.order_index,
        node_hints=[
            RenderFactContextNodeHint(
                source_node_id=contract.nodes[0].source_node_id,
                established=text,
            )
        ],
    )


def _plan(
    contracts: list[RenderContract],
    *rules: NameResolutionRule,
    protected_surfaces: list[str] | None = None,
) -> NameResolutionPlan:
    return NameResolutionPlan(
        plan_id="name-resolution-test",
        project_id="proj",
        output_language="zh-CN",
        input_hash="sha256:test",
        contract_ids=[contract.contract_id for contract in contracts],
        rules=list(rules),
        protected_surfaces=protected_surfaces or [],
    )


def _record(
    kwargs: dict[str, Any],
    call_id: str = "call-name-table",
    *,
    status: ProviderStatus = ProviderStatus.success,
) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage="name_resolution",
        operation_route=kwargs["operation_route"],
        provider="fake",
        model="fake",
        input_hash=f"sha256:{call_id}",
        provider_status=status,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
        prompt_version=kwargs.get("prompt_version"),
        work_unit=kwargs.get("work_unit"),
    )


def _payload(
    replacements: list[dict[str, Any]] | None = None,
    protected_surfaces: list[Any] | None = None,
) -> str:
    return json.dumps(
        {
            "replacements": replacements or [],
            "protected_surfaces": protected_surfaces or [],
        },
        ensure_ascii=False,
    )


def _override(*aliases: str, target: str = "蒂娜") -> NameOverride:
    return NameOverride(
        aliases=list(aliases),
        target_surface=target,
        output_language="zh-CN",
    )


def test_name_table_calls_once_with_complete_selected_materials_only(
    tmp_path: Path,
) -> None:
    first = _contract(
        0,
        material_text="  Terra crosses the northern gate.  ",
        directive="DirectiveOnly must dominate the opening.",
    )
    second = _contract(
        1,
        material_text="Locke waits beyond the river.",
        directive="Return to the route after the evidence.",
    )
    context = _context(first, "FactOnly was already introduced.")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            _payload(
                [
                    {"aliases": ["Terra"], "stable_name": "蒂娜"},
                    {"aliases": ["DirectiveOnly"], "stable_name": "指令名"},
                    {"aliases": ["FactOnly"], "stable_name": "事实名"},
                ]
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [second, first],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        fact_contexts=[context],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "complete-materials"),
        call=fake_call,
    )

    [call] = calls
    assert call["operation_route"] == NAME_TABLE_ROUTE
    assert call["work_unit"].kind == "name_table"
    assert call["work_unit"].unit_id == "name-table"
    assert call["work_unit"].input_refs == ["material_1", "material_2"]
    prompt = call["messages"][1]["content"]
    assert prompt.count(first.materials[0].content) == 1
    assert prompt.count(second.materials[0].content) == 1
    assert (
        "<<<MATERIAL 0001>>>\n\n"
        "  Terra crosses the northern gate.  \n\n"
        "<<<END MATERIAL>>>"
    ) in prompt
    assert first.nodes[0].directive not in prompt
    assert second.nodes[0].directive not in prompt
    assert context.node_hints[0].established not in prompt
    assert [rule.aliases for rule in result.plan.rules] == [["Terra"]]
    assert sum(report.kind == "name_table_invalid_entry" for report in result.reports) == 2
    assert result.plan.metadata["table_calls"] == 1
    assert result.plan.metadata["material_count"] == 2


@pytest.mark.parametrize(
    ("material", "aliases"),
    [
        ("Terra arrives alone.", ["Terra"]),
        ("Terra, TERRA, and ティナ refer to one person.", ["Terra", "TERRA", "ティナ"]),
    ],
)
def test_name_table_accepts_singleton_and_multi_alias_entries(
    tmp_path: Path,
    material: str,
    aliases: list[str],
) -> None:
    contract = _contract(0, material_text=material)

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return _payload([{"aliases": aliases, "stable_name": "蒂娜"}]), _record(kwargs)

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", f"aliases-{len(aliases)}"),
        call=fake_call,
    )

    [rule] = result.plan.rules
    assert set(rule.aliases) == set(aliases)
    assert rule.stable_name == "蒂娜"
    assert rule.metadata["source"] == "model"
    assert result.plan.metadata["accepted_model_rules"] == 1


def test_operator_override_is_authoritative_over_overlapping_model_entry(
    tmp_path: Path,
) -> None:
    contract = _contract(0, material_text="Locke Lamora is also called Locke.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return (
            _payload(
                [
                    {
                        "aliases": ["Locke", "Locke Lamora"],
                        "stable_name": "罗克",
                    }
                ]
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("Locke", target="洛克")],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "operator-authority"),
        call=fake_call,
    )

    [rule] = result.plan.rules
    assert set(rule.aliases) == {"Locke", "Locke Lamora"}
    assert rule.stable_name == "洛克"
    assert rule.metadata["source"] == "operator+model"


def test_model_bridge_between_distinct_operator_targets_is_dropped(
    tmp_path: Path,
) -> None:
    contract = _contract(0, material_text="Terra meets Tina at the gate.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return (
            _payload(
                [
                    {
                        "aliases": ["Terra", "Tina"],
                        "stable_name": "蒂娜",
                    }
                ]
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[
            _override("Terra", target="泰拉"),
            _override("Tina", target="蒂娜"),
        ],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "operator-bridge"),
        call=fake_call,
    )

    assert {
        (tuple(rule.aliases), rule.stable_name, rule.metadata["source"])
        for rule in result.plan.rules
    } == {
        (("Terra",), "泰拉", "operator"),
        (("Tina",), "蒂娜", "operator"),
    }
    assert [report.kind for report in result.reports] == [
        "name_table_operator_bridge"
    ]
    assert result.plan.metadata["accepted_model_rules"] == 0


def test_conflicting_operator_overrides_fail_closed(tmp_path: Path) -> None:
    contract = _contract(0, material_text="Terra arrives.")

    with pytest.raises(NameResolutionError) as exc_info:
        build_name_resolution(
            [contract],
            project_id="proj",
            output_language="zh-CN",
            overrides=[
                _override("Terra", target="蒂娜"),
                _override("Terra", target="泰拉"),
            ],
            config=_config(),
            store=RunStore.for_run(tmp_path / "runs", "override-conflict"),
            call=lambda **_kwargs: pytest.fail("overrides_only must not call a model"),
        )

    [report] = exc_info.value.reports
    assert report.kind == "name_override_target_conflict"
    assert report.usable is False


def test_protected_longer_surface_prevents_shorter_alias_replacement(
    tmp_path: Path,
) -> None:
    contract = _contract(0, material_text="Locke Lamora met Locke at dawn.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return (
            _payload(
                [{"aliases": ["Locke"], "stable_name": "洛克"}],
                ["Locke Lamora"],
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "protected"),
        call=fake_call,
    )

    assert result.plan.protected_surfaces == ["Locke Lamora"]
    assert resolve_text_names("Locke Lamora met Locke.", result.plan) == (
        "Locke Lamora met 洛克."
    )


def test_model_protection_cannot_block_operator_alias(tmp_path: Path) -> None:
    contract = _contract(0, material_text="Locke Lamora met Locke at dawn.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return _payload(protected_surfaces=["Locke Lamora"]), _record(kwargs)

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("Locke", target="洛克")],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "operator-protection"),
        call=fake_call,
    )

    assert result.plan.protected_surfaces == []
    assert resolve_text_names(contract.materials[0].content, result.plan) == (
        "洛克 Lamora met 洛克 at dawn."
    )
    assert [report.kind for report in result.reports] == [
        "name_table_invalid_protection"
    ]


def test_invalid_entries_are_salvaged_independently(tmp_path: Path) -> None:
    contract = _contract(0, material_text="Terra meets Locke.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return (
            _payload(
                [
                    {"aliases": ["Terra"], "stable_name": "蒂娜"},
                    {"aliases": [], "stable_name": "空"},
                    {"aliases": ["Locke", "Ghost"], "stable_name": "洛克"},
                    {"aliases": ["Locke"]},
                ]
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "salvage"),
        call=fake_call,
    )

    assert [(rule.aliases, rule.stable_name) for rule in result.plan.rules] == [
        (["Terra"], "蒂娜")
    ]
    assert sum(report.kind == "name_table_invalid_entry" for report in result.reports) == 3
    assert result.plan.metadata["table_calls"] == 1


def test_model_aliases_only_edit_materials_while_operator_edits_full_exposure(
    tmp_path: Path,
) -> None:
    contract = _contract(
        0,
        material_text="ModelName met OperatorName.",
        directive="ModelName then OperatorName controls the turn.",
    )
    context = _context(contract, "ModelName and OperatorName were established.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return (
            _payload(
                [{"aliases": ["ModelName"], "stable_name": "模型名"}]
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("OperatorName", target="操作名")],
        fact_contexts=[context],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "projection-scope"),
        call=fake_call,
    )

    resolved_contract = resolve_contract_names(contract, result.plan)
    resolved_context = resolve_fact_context_names(
        context, result.plan, contract=contract
    )

    assert resolved_contract.materials[0].content == "模型名 met 操作名."
    assert resolved_contract.nodes[0].directive == (
        "ModelName then 操作名 controls the turn."
    )
    assert resolved_context is not None
    assert resolved_context.node_hints[0].established == (
        "ModelName and 操作名 were established."
    )


def test_mixed_rule_limits_directive_and_fact_edits_to_operator_aliases(
    tmp_path: Path,
) -> None:
    contract = _contract(
        0,
        material_text="Terra and TERRA refer to the same person.",
        directive="Terra follows TERRA through the gate.",
    )
    context = _context(contract, "Terra and TERRA were both established.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return (
            _payload(
                [
                    {
                        "aliases": ["Terra", "TERRA"],
                        "stable_name": "泰拉",
                    }
                ]
            ),
            _record(kwargs),
        )

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("TERRA", target="蒂娜")],
        fact_contexts=[context],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "mixed-projection-scope"),
        call=fake_call,
    )

    [rule] = result.plan.rules
    assert rule.metadata["source"] == "operator+model"
    assert rule.metadata["operator_aliases"] == ["TERRA"]
    resolved_contract = resolve_contract_names(contract, result.plan)
    resolved_context = resolve_fact_context_names(
        context, result.plan, contract=contract
    )
    assert resolved_contract.materials[0].content == (
        "蒂娜 and 蒂娜 refer to the same person."
    )
    assert resolved_contract.nodes[0].directive == (
        "Terra follows 蒂娜 through the gate."
    )
    assert resolved_context is not None
    assert resolved_context.node_hints[0].established == (
        "Terra and 蒂娜 were both established."
    )


def test_malformed_json_keeps_only_operator_overrides(tmp_path: Path) -> None:
    contract = _contract(0, material_text="Terra arrives.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        return "not json", _record(kwargs)

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("Terra")],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "malformed"),
        call=fake_call,
    )

    [rule] = result.plan.rules
    assert rule.metadata["source"] == "operator"
    assert rule.stable_name == "蒂娜"
    assert [report.kind for report in result.reports] == ["name_table_invalid_output"]
    assert result.plan.metadata["table_calls"] == 1


def test_transport_failure_keeps_only_operator_overrides(tmp_path: Path) -> None:
    contract = _contract(0, material_text="Terra arrives.")

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        record = _record(kwargs, "failed-name-table", status=ProviderStatus.error)
        raise LlmTransportError("offline failure", record=record, attempts=2)

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("Terra")],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "transport"),
        call=fake_call,
    )

    [rule] = result.plan.rules
    assert rule.metadata["source"] == "operator"
    [report] = result.reports
    assert report.kind == "name_table_call_failed"
    assert report.call_id == "failed-name-table"
    assert result.plan.metadata["table_calls"] == 1


def test_over_budget_skips_model_and_keeps_operator_overrides(tmp_path: Path) -> None:
    contract = _contract(0, material_text="Terra arrives with a long account.")

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        overrides=[_override("Terra")],
        config=_config(table_max_input_tokens=1),
        store=RunStore.for_run(tmp_path / "runs", "over-budget"),
        call=lambda **_kwargs: pytest.fail("over-budget Name Table must not call"),
    )

    [rule] = result.plan.rules
    assert rule.metadata["source"] == "operator"
    assert [report.kind for report in result.reports] == ["name_table_over_budget"]
    assert result.plan.metadata["table_calls"] == 0
    assert result.plan.metadata["table_over_budget"] is True
    assert result.plan.metadata["table_input_tokens"] > 1


def test_overrides_only_has_zero_name_table_routes_and_matches_full_exposure(
    tmp_path: Path,
) -> None:
    contract = _contract(
        0,
        material_text="No named person appears in the selected material.",
        directive="DirectiveOnly must open the segment.",
    )
    context = _context(contract, "FactOnly was already introduced.")
    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.overrides_only,
        overrides=[
            _override("DirectiveOnly", target="指令名"),
            _override("FactOnly", target="事实名"),
        ],
        fact_contexts=[context],
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "overrides-only"),
        call=lambda **_kwargs: pytest.fail("overrides_only must have zero routes"),
    )

    assert {(tuple(rule.aliases), rule.stable_name) for rule in result.plan.rules} == {
        (("DirectiveOnly",), "指令名"),
        (("FactOnly",), "事实名"),
    }
    assert result.plan.metadata["mode"] == "overrides_only"
    assert result.plan.metadata["table_calls"] == 0
    assert result.plan.metadata["table_protocol"] is None


def test_name_table_input_estimate_matches_mode_and_budget() -> None:
    contracts = [
        _contract(0, material_text="Terra arrives."),
        _contract(1, material_text="Locke waits."),
    ]

    automatic = estimate_name_resolution_inputs(
        contracts,
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(),
    )
    overrides_only = estimate_name_resolution_inputs(
        contracts,
        output_language="zh-CN",
        mode=NameResolutionMode.overrides_only,
        config=_config(),
    )
    over_budget = estimate_name_resolution_inputs(
        contracts,
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(table_max_input_tokens=1),
    )

    assert automatic.table_calls == 1
    assert automatic.table_input_tokens == automatic.projected_input_tokens > 0
    assert automatic.material_count == 2
    assert automatic.over_budget is False
    assert overrides_only.total_input_tokens == 0
    assert overrides_only.table_calls == 0
    assert overrides_only.material_count == 0
    assert over_budget.table_calls == 0
    assert over_budget.table_input_tokens == 0
    assert over_budget.projected_input_tokens == automatic.projected_input_tokens
    assert over_budget.material_count == 2
    assert over_budget.over_budget is True


def test_nonpositive_fractional_name_budgets_fall_back_to_defaults(
    tmp_path: Path,
) -> None:
    contract = _contract(0, material_text="Terra arrives.")
    captured: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        captured.append(kwargs)
        return _payload(), _record(kwargs)

    result = build_name_resolution(
        [contract],
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(table_max_input_tokens=-0.5, max_rules=0.0),
        store=RunStore.for_run(tmp_path / "runs", "budget-defaults"),
        call=fake_call,
    )

    [call] = captured
    assert call["work_unit"].metadata["max_rules"] == 256
    assert result.plan.metadata["table_calls"] == 1
    assert result.plan.metadata["table_over_budget"] is False


def test_repeated_material_is_projected_once_but_traces_all_chunks(
    tmp_path: Path,
) -> None:
    contracts = [
        _contract(0, material_text="Terra arrives.", material_id="shared-material"),
        _contract(1, material_text="Terra arrives.", material_id="shared-material"),
    ]
    captured_prompt = ""

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        nonlocal captured_prompt
        captured_prompt = kwargs["messages"][1]["content"]
        assert kwargs["work_unit"].input_refs == ["shared-material"]
        return _payload([{"aliases": ["Terra"], "stable_name": "蒂娜"}]), _record(kwargs)

    result = build_name_resolution(
        contracts,
        project_id="proj",
        output_language="zh-CN",
        mode=NameResolutionMode.name_table,
        config=_config(),
        store=RunStore.for_run(tmp_path / "runs", "dedup-material"),
        call=fake_call,
    )

    assert captured_prompt.count("Terra arrives.") == 1
    [rule] = result.plan.rules
    assert rule.evidence_chunk_ids == ["chunk_1", "chunk_2"]
    assert result.plan.metadata["material_count"] == 1


def test_longest_match_is_single_pass_and_respects_latin_boundaries() -> None:
    contract = _contract(0, material_text="Terra Prime met Terra and 泰拉.")
    plan = _plan(
        [contract],
        NameResolutionRule(
            rule_id="rule-terra",
            aliases=["Terra", "Terra Prime"],
            stable_name="泰拉",
            evidence_chunk_ids=["chunk_1"],
        ),
        NameResolutionRule(
            rule_id="rule-cascade",
            aliases=["泰拉"],
            stable_name="蒂娜",
            evidence_chunk_ids=["chunk_1"],
        ),
    )

    assert resolve_text_names("Terra Prime met Terra and 泰拉.", plan) == (
        "泰拉 met 泰拉 and 蒂娜."
    )
    assert resolve_text_names("XTerra Terra_2 Terra", plan) == "XTerra Terra_2 泰拉"


def test_contract_and_fact_resolution_return_copies_and_keep_raw_inputs() -> None:
    contract = _contract(
        0,
        material_text="Terra met Locke.",
        directive="让Terra先开口。",
    )
    context = _context(contract, "Locke见过Terra及其到场、质疑动作均已安排。")
    raw_contract = contract.model_dump(mode="json")
    raw_context = context.model_dump(mode="json")
    plan = _plan(
        [contract],
        NameResolutionRule(
            rule_id="rule-terra",
            aliases=["Terra"],
            stable_name="蒂娜",
            evidence_chunk_ids=[contract.chunk_id],
            metadata={"source": "operator"},
        ),
        NameResolutionRule(
            rule_id="rule-locke",
            aliases=["Locke"],
            stable_name="洛克",
            evidence_chunk_ids=[contract.chunk_id],
            metadata={"source": "operator"},
        ),
    )

    resolved_contract = resolve_contract_names(contract, plan)
    resolved_context = resolve_fact_context_names(context, plan, contract=contract)

    assert resolved_contract.materials[0].content == "蒂娜 met 洛克."
    assert resolved_contract.nodes[0].directive == "让蒂娜先开口。"
    assert resolved_context is not None
    assert resolved_context.node_hints[0].established == (
        "洛克见过蒂娜及其到场、质疑动作均已安排。"
    )
    assert contract.model_dump(mode="json") == raw_contract
    assert context.model_dump(mode="json") == raw_context


def test_unknown_source_is_material_only_but_legacy_pinned_rule_is_authorized() -> None:
    contract = _contract(
        0,
        material_text="Terra met Locke.",
        directive="Terra then Locke controls the turn.",
    )
    context = _context(contract, "Terra and Locke were established.")
    plan = _plan(
        [contract],
        NameResolutionRule(
            rule_id="rule-unknown",
            aliases=["Terra"],
            stable_name="蒂娜",
            evidence_chunk_ids=[contract.chunk_id],
        ),
        NameResolutionRule(
            rule_id="rule-legacy-pinned",
            aliases=["Locke"],
            stable_name="洛克",
            evidence_chunk_ids=[contract.chunk_id],
            metadata={"pinned": True},
        ),
    )

    resolved_contract = resolve_contract_names(contract, plan)
    resolved_context = resolve_fact_context_names(context, plan, contract=contract)

    assert resolved_contract.materials[0].content == "蒂娜 met 洛克."
    assert resolved_contract.nodes[0].directive == (
        "Terra then 洛克 controls the turn."
    )
    assert resolved_context is not None
    assert resolved_context.node_hints[0].established == (
        "Terra and 洛克 were established."
    )


def test_mixed_rule_without_operator_aliases_authorizes_no_nonmaterial_edits() -> None:
    contract = _contract(
        0,
        material_text="Terra and TERRA refer to one person.",
        directive="Terra follows TERRA.",
    )
    context = _context(contract, "Terra and TERRA were established.")
    plan = _plan(
        [contract],
        NameResolutionRule(
            rule_id="rule-mixed-missing-authority",
            aliases=["Terra", "TERRA"],
            stable_name="蒂娜",
            evidence_chunk_ids=[contract.chunk_id],
            metadata={"source": "operator+model"},
        ),
    )

    resolved_contract = resolve_contract_names(contract, plan)
    resolved_context = resolve_fact_context_names(context, plan, contract=contract)

    assert resolved_contract.materials[0].content == (
        "蒂娜 and 蒂娜 refer to one person."
    )
    assert resolved_contract.nodes[0].directive == "Terra follows TERRA."
    assert resolved_context is not None
    assert resolved_context.node_hints[0].established == (
        "Terra and TERRA were established."
    )


def test_protections_never_apply_to_directive_or_fact_fields() -> None:
    contract = _contract(
        0,
        material_text="Locke Lamora met Locke.",
        directive="Locke Lamora follows Locke.",
    )
    context = _context(contract, "Locke Lamora and Locke were established.")
    plan = _plan(
        [contract],
        NameResolutionRule(
            rule_id="rule-locke-operator",
            aliases=["Locke"],
            stable_name="洛克",
            evidence_chunk_ids=[contract.chunk_id],
            metadata={"source": "operator"},
        ),
        protected_surfaces=["Locke Lamora"],
    )

    resolved_contract = resolve_contract_names(contract, plan)
    resolved_context = resolve_fact_context_names(context, plan, contract=contract)

    assert resolved_contract.materials[0].content == "Locke Lamora met 洛克."
    assert resolved_contract.nodes[0].directive == "洛克 Lamora follows 洛克."
    assert resolved_context is not None
    assert resolved_context.node_hints[0].established == (
        "洛克 Lamora and 洛克 were established."
    )
