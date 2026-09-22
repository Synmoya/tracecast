"""FactContext v3 plus Renderer position, protocol, and retry regressions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock
from typing import Any

import pytest

import tracecast.prompt_components as prompt_components
from tracecast.control_prompt import ControlPromptLocale
from tracecast.domain import (
    ContentKind,
    LlmCallRecord,
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
from tracecast.hosts import HostProfile, load_host
from tracecast.llm import LlmTransportError
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.rendering import (
    FactContextBudgets,
    apply_render_block_overrides,
    build_fact_contexts,
    build_render_blocks,
    coerce_persona,
    derive_render_chunk_positions,
    is_degenerate_repetition,
    longest_repetition_run,
    parse_fact_contexts,
    project_chunk,
    project_fact_context_outline,
    render_chunk_position_block,
    render_chunk_user_prompt,
    render_chunks,
)
from tracecast.rendering.renderer import (
    RENDER_DEGENERATE_MAX_ATTEMPTS,
    render_style_examples_block,
)
from tracecast.storage import RunStore, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def _host() -> HostProfile:
    return load_host(REPO_ROOT / "hosts", "example_host_zh")


def _v1_host() -> HostProfile:
    payload = _host().model_dump(mode="json")
    payload["schema_version"] = "tracecast-host/1"
    payload["style_examples_by_position"]["single"] = [
        {
            "source": f"v1 single source {index}",
            "render": f"v1 single render {index}",
        }
        for index in (1, 2)
    ]
    # The catalog host is v2 and carries no lesson; only v1 owns that field.
    for position, examples in payload["style_examples_by_position"].items():
        for index, example in enumerate(examples, start=1):
            example["lesson"] = f"v1 {position} lesson {index}"
    return HostProfile.model_validate(payload)


def _v2_host_with_pool(pool_size: int = 24) -> HostProfile:
    payload = _host().model_dump(mode="json")
    payload["schema_version"] = "tracecast-host/2"
    payload["style_examples_by_position"] = {
        position: [
            {
                "source": f"{position} source pool #{index:03d}#",
                "render": f"{position} render pool #{index:03d}#",
            }
            for index in range(pool_size)
        ]
        for position in ("opening", "middle", "ending")
    }
    return HostProfile.model_validate(payload)


def _render_persona():
    return coerce_persona(_v1_host())


def _contract(
    order: int,
    *,
    two_nodes: bool = False,
    first_directive: str | None = "先交代变化，不逐条复述",
) -> RenderContract:
    chunk_id = f"chunk_{order + 1}"
    nodes = [
        RenderContractNode(
            node_ref="N1",
            source_node_id=f"cand_{order}_1",
            order_in_chunk=0,
            role=Role.core,
            treatment=Treatment.paraphrase,
            directive=first_directive,
            summary=f"第{order + 1}段的主事实",
            narrative_function="state_change",
            material_refs=["R001"],
        )
    ]
    materials = [
        RenderContractMaterial(
            material_ref="R001",
            source_material_id=f"mat_{order}_1",
            node_ref="N1",
            operation=NodeEditOperation.keep,
            content=f"第{order + 1}段原始材料甲。",
            language="zh",
            content_kind=ContentKind.dialogue,
            tags=["speaker_turn"],
        )
    ]
    if two_nodes:
        nodes.append(
            RenderContractNode(
                node_ref="N2",
                source_node_id=f"cand_{order}_2",
                order_in_chunk=1,
                role=Role.bridge,
                treatment=Treatment.summarize,
                directive="收束到结果",
                summary=f"第{order + 1}段的结果",
                material_refs=["R002"],
            )
        )
        materials.append(
            RenderContractMaterial(
                material_ref="R002",
                source_material_id=f"mat_{order}_2",
                node_ref="N2",
                operation=NodeEditOperation.keep,
                content=f"第{order + 1}段原始材料乙。",
                language="zh",
                content_kind=ContentKind.narration,
            )
        )
    return RenderContract(
        contract_id=f"contract_{order + 1}",
        chunk_id=chunk_id,
        order_index=order,
        project_id="proj",
        persona_id="persona",
        nodes=nodes,
        materials=materials,
    )


def _empty_name_resolution(contracts: list[RenderContract]) -> NameResolutionPlan:
    return NameResolutionPlan(
        plan_id="name-resolution-empty",
        project_id="proj",
        output_language="zh-CN",
        input_hash="sha256:test",
        contract_ids=[contract.contract_id for contract in contracts],
        rules=[],
    )


def test_fact_context_outline_uses_chunk_boundaries_sums_and_directives() -> None:
    contracts = [_contract(0, two_nodes=True), _contract(1)]
    outline = project_fact_context_outline(contracts)
    assert outline == (
        "ORDERED_CHUNK_PLAN\n"
        "CHUNK C001\n"
        "NODE N001\n"
        "CONTENT_SCHEDULED_HERE 第1段的主事实\n"
        "ACTION_SCHEDULED_HERE 先交代变化，不逐条复述\n"
        "ENDNODE\n"
        "NODE N002\n"
        "CONTENT_SCHEDULED_HERE 第1段的结果\n"
        "ACTION_SCHEDULED_HERE 收束到结果\n"
        "ENDNODE\n"
        "ENDCHUNK\n"
        "CHUNK C002\n"
        "NODE N001\n"
        "CONTENT_SCHEDULED_HERE 第2段的主事实\n"
        "ACTION_SCHEDULED_HERE 先交代变化，不逐条复述\n"
        "ENDNODE\n"
        "ENDCHUNK\n"
        "END_ORDERED_CHUNK_PLAN"
    )
    assert outline.count("\nNODE N001") == 2
    assert outline.count("\nNODE N002") == 1
    assert "PLAN " not in outline
    assert "ENDPLAN" not in outline
    assert "CONTENT_SCHEDULED_HERE 第1段的主事实" in outline
    assert "ACTION_SCHEDULED_HERE 先交代变化，不逐条复述" in outline
    assert "CONTENT_SCHEDULED_HERE 第1段的结果" in outline
    assert "ACTION_SCHEDULED_HERE 收束到结果" in outline
    assert "CONTENT_SCHEDULED_HERE 第2段的主事实" in outline
    assert "第1段原始材料甲" not in outline
    assert "persona" not in outline
    assert "正文" not in outline
    assert "chunk_" not in outline
    assert "speaker_turn" not in outline
    assert "state_change" not in outline
    assert "context_max_tokens" not in outline


def test_null_directive_is_absent_from_fact_context_and_renderer_markers() -> None:
    contract = _contract(0, two_nodes=True, first_directive=None)

    outline = project_fact_context_outline([contract])
    assert "CONTENT_SCHEDULED_HERE 第1段的主事实" in outline
    assert "ACTION_SCHEDULED_HERE none" in outline
    assert "ACTION_SCHEDULED_HERE 收束到结果" in outline
    assert outline.count("ACTION_SCHEDULED_HERE ") == 2

    projected = project_chunk(contract)
    assert projected == (
        "第1段原始材料甲。\n\n"
        "【叙事提示｜收束到结果】\n\n"
        "第1段原始材料乙。"
    )
    assert projected.count("【叙事提示｜") == 1


def test_fact_context_parser_accepts_sparse_hints_and_rejects_bad_lines() -> None:
    contracts = [_contract(0, two_nodes=True), _contract(1)]
    budgets = FactContextBudgets(min_tokens=80, max_tokens=320)
    raw = (
        "HINT C002N001 | 提问动作已在更早 chunk 计划安排。\n"
        "HINT C002N001 | 到场内容也已在更早 chunk 计划安排。"
    )
    parsed = parse_fact_contexts(
        raw,
        contracts,
        budgets=budgets,
        prompt_version="test@v3",
        call_id="call_1",
    )
    assert parsed[0].node_hints == []
    assert [hint.source_node_id for hint in parsed[1].node_hints] == [
        "cand_1_1",
        "cand_1_1",
    ]
    assert [hint.established for hint in parsed[1].node_hints] == [
        "提问动作已在更早 chunk 计划安排。",
        "到场内容也已在更早 chunk 计划安排。",
    ]
    assert all(
        item.metadata["semantics"] == "sparse_node_established_v3"
        for item in parsed
    )

    with pytest.raises(ValueError, match="in the first render chunk"):
        parse_fact_contexts(
            "HINT C001N002 | 同一 chunk 内的重复不应生成提示。",
            contracts,
            budgets=budgets,
            prompt_version="test@v3",
            call_id="call_1",
        )

    for leaked_alias in ("C001N001", "C001", "N001"):
        with pytest.raises(ValueError, match="contains a node alias"):
            parse_fact_contexts(
                f"HINT C002N001 | {leaked_alias} 已经安排。",
                contracts,
                budgets=budgets,
                prompt_version="test@v3",
                call_id="call_1",
            )

    with pytest.raises(ValueError, match="outside sparse HINT DSL"):
        parse_fact_contexts(
            "额外说明\nHINT C002N001 | 合法但整份响应并不干净。",
            contracts,
            budgets=budgets,
            prompt_version="test@v3",
            call_id="call_1",
        )


def test_fact_context_uses_one_run_wide_call_and_compiles_hints_by_node(
    tmp_path: Path,
) -> None:
    contracts = [_contract(0), _contract(1), _contract(2)]
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "HINT C002N001 | 第一项内容已在更早计划安排。\n"
            "HINT C003N001 | 开场动作已在更早计划安排。",
            LlmCallRecord(
                call_id="call_directional_context",
                stage="rendering",
                operation_route=kwargs["operation_route"],
                provider="fake",
                model="fake",
                input_hash="sha256:fake",
                provider_status=ProviderStatus.success,
                created_at=datetime(2026, 7, 28, tzinfo=UTC),
            ),
        )

    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    result = build_fact_contexts(
        contracts,
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "directional-context"),
        output_language="zh-CN",
        call=fake_call,
    )

    assert len(calls) == 1
    prompt = calls[0]["messages"][1]["content"]
    assert (
        prompt.index("CHUNK C001")
        < prompt.index("CHUNK C002")
        < prompt.index("CHUNK C003")
    )
    assert prompt.count("NODE N001") == 3
    assert "NODE N002" not in prompt
    assert "PLAN " not in prompt
    assert " P001" not in prompt
    assert "第1段原始材料甲" not in prompt
    assert calls[0]["work_unit"].kind == "render_fact_context"
    assert calls[0]["work_unit"].input_refs == ["cand_0_1", "cand_1_1", "cand_2_1"]
    assert result.contexts[0].node_hints == []
    assert result.contexts[1].node_hints[0].source_node_id == "cand_1_1"
    assert result.contexts[2].node_hints[0].source_node_id == "cand_2_1"
    assert all(
        context.metadata["call_id"] == "call_directional_context"
        for context in result.contexts
    )

    system_prompt, _ = load_system_prompt("render_fact_context_dsl", config)
    user_prompt, _ = load_user_prompt("render_fact_context_dsl", config)
    system_no_ws = "".join(system_prompt.split())
    user_no_ws = "".join(user_prompt.split())
    assert "同一CHUNK内即使相似也不输出" in system_no_ws
    assert "当前NODE和某个更早NODE中同时指认同一项内容" in system_no_ws
    assert "HINTCxxxNxxx|" in system_no_ws
    assert "当前NODE是否位于第二或更晚CHUNK" in user_no_ws
    assert "同一NODE有多条HINT" in user_no_ws
    assert "HINTCxxxNxxx|" in user_no_ws
    for old_reference_shape in (
        "prior=",
        "BEFORE_CURRENT",
        "earlier_refs",
        "PLAN P",
        "HINT P",
    ):
        assert old_reference_shape not in system_prompt
        assert old_reference_shape not in user_prompt
    assert "提示不是事实来源，也不能授权添加或删除当前材料的新信息" in system_no_ws
    assert "拿不准就省略，不为每个NODE自身总结，不追求覆盖率" in system_no_ws


def test_render_fact_context_legacy_decode_is_one_way_and_mixed_schema_is_rejected() -> None:
    legacy = RenderFactContext.model_validate(
        {
            "chunk_id": "chunk_legacy",
            "order_index": 4,
            "carry": "旧版承接文本",
            "established": "旧版既有文本",
            "prior_chunk_ids": ["chunk_1", "chunk_2"],
            "current_node_ids": ["cand_legacy"],
            "metadata": {"semantics": "old_runtime_label"},
        }
    )
    assert legacy.node_hints == []
    assert legacy.metadata["semantics"] == "legacy_render_fact_context_v1"
    assert legacy.metadata["migrated_from"] == "render_fact_context_v1"
    assert legacy.metadata["legacy_metadata_semantics"] == "old_runtime_label"
    assert legacy.metadata["legacy_chunk_context"] == {
        "carry": "旧版承接文本",
        "current_node_ids": ["cand_legacy"],
        "established": "旧版既有文本",
        "prior_chunk_ids": ["chunk_1", "chunk_2"],
    }

    dumped = legacy.model_dump(mode="json")
    assert {"carry", "established", "prior_chunk_ids"}.isdisjoint(dumped)
    assert dumped["node_hints"] == []

    legacy_v2 = RenderFactContext.model_validate(
        {
            "chunk_id": "chunk_legacy_v2",
            "order_index": 5,
            "planned_entry_state": "旧前态",
            "planned_content_overlap": "旧内容重合",
            "planned_action_overlap": None,
            "current_node_ids": ["cand_legacy_v2"],
        }
    )
    assert legacy_v2.node_hints == []
    assert legacy_v2.metadata["migrated_from"] == "render_fact_context_v2"
    assert legacy_v2.metadata["legacy_chunk_context"] == {
        "current_node_ids": ["cand_legacy_v2"],
        "planned_action_overlap": None,
        "planned_content_overlap": "旧内容重合",
        "planned_entry_state": "旧前态",
    }

    current = RenderFactContext(chunk_id="chunk_current", order_index=0)
    current_dump = current.model_dump(mode="json")
    assert current_dump["metadata"]["semantics"] == "sparse_node_established_v3"
    assert {"carry", "established", "prior_chunk_ids"}.isdisjoint(current_dump)

    with pytest.raises(ValueError, match="cannot mix legacy and v3 fields"):
        RenderFactContext.model_validate(
            {
                "chunk_id": "chunk_mixed",
                "order_index": 1,
                "carry": "legacy",
                "node_hints": [
                    {
                        "source_node_id": "cand_current",
                        "established": "current",
                    }
                ],
            }
        )


def test_render_fact_context_domain_limits_each_node_to_three_distinct_hints() -> None:
    context = RenderFactContext(
        chunk_id="chunk_limits",
        order_index=1,
        node_hints=[
            RenderFactContextNodeHint(
                source_node_id="node_a",
                established=f"distinct hint {index}",
            )
            for index in range(3)
        ]
        + [
            RenderFactContextNodeHint(
                source_node_id="node_b",
                established="distinct hint 0",
            )
        ],
    )
    assert [hint.source_node_id for hint in context.node_hints].count("node_a") == 3

    with pytest.raises(ValueError, match="at most three hints"):
        RenderFactContext(
            chunk_id="chunk_too_many",
            order_index=1,
            node_hints=[
                RenderFactContextNodeHint(
                    source_node_id="node_a",
                    established=f"hint {index}",
                )
                for index in range(4)
            ],
        )

    with pytest.raises(ValueError, match="cannot repeat identical hints"):
        RenderFactContext(
            chunk_id="chunk_duplicate",
            order_index=1,
            node_hints=[
                RenderFactContextNodeHint(
                    source_node_id="node_a",
                    established="same hint",
                ),
                RenderFactContextNodeHint(
                    source_node_id="node_a",
                    established="same hint",
                ),
            ],
        )


def test_sparse_recovery_drops_each_bad_line_and_keeps_other_valid_hints(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        text = (
            "说明文字\n"
            "HINT C001N001 | 第一 chunk 的首节点不应有 hint。\n"
            "HINT C001N002 | 第一 chunk 的第二节点也不应有 hint。\n"
            "HINT C002N001 | 共同内容甲。\n"
            "HINT C002N001 | 共同内容甲。\n"
            "HINT C002N001 | 共同动作乙。\n"
            "HINT C002N001 | 共同内容丙。\n"
            "HINT C002N001 | 第四条应被截断。\n"
            "HINT C002N002 | 第二节点的合法提示。\n"
            "HINT C003N001 | C001 已经安排。\n"
            "HINT C003N002 | none\n"
            "HINT C999N001 | 未知节点。"
        )
        return text, LlmCallRecord(
            call_id="call_sparse_recovery",
            stage="rendering",
            operation_route=kwargs["operation_route"],
            provider="fake",
            model="fake",
            input_hash="sha256:fake",
            provider_status=ProviderStatus.success,
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
        )

    result = build_fact_contexts(
        [
            _contract(0, two_nodes=True),
            _contract(1, two_nodes=True),
            _contract(2, two_nodes=True),
        ],
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "missing-repair"),
        output_language="zh-CN",
        call=fake_call,
    )

    assert len(calls) == 1
    assert calls[0]["operation_route"] == "render_fact_context_dsl"
    assert result.contexts[0].node_hints == []
    assert [
        (hint.source_node_id, hint.established)
        for hint in result.contexts[1].node_hints
    ] == [
        ("cand_1_1", "共同内容甲。"),
        ("cand_1_1", "共同动作乙。"),
        ("cand_1_1", "共同内容丙。"),
        ("cand_1_2", "第二节点的合法提示。"),
    ]
    assert result.contexts[2].node_hints == []
    messages = "\n".join(report.message for report in result.reports)
    for fragment in (
        "outside sparse HINT DSL",
        "node alias C001N001 is in the first render chunk",
        "node alias C001N002 is in the first render chunk",
        "C002N001 repeated identical hint text",
        "C002N001 emitted 4 distinct hints",
        "C003N001 established text contains a node alias",
        "C003N002 has empty/none",
        "unknown node alias C999N001",
    ):
        assert fragment in messages
    assert all(report.kind == "fact_context_parse_error" for report in result.reports)


def test_fact_context_rejects_nonzero_route_temperature_before_call(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    route = config.route("render_fact_context_dsl")
    config = config.model_copy(
        update={
            "routes": {
                **config.routes,
                "render_fact_context_dsl": route.model_copy(
                    update={"temperature": 0.2}
                ),
            }
        }
    )
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return "", LlmCallRecord(
            call_id="must-not-call",
            stage="rendering",
            operation_route=kwargs["operation_route"],
            provider="fake",
            model="fake",
            input_hash="sha256:fake",
            provider_status=ProviderStatus.success,
            created_at=datetime(2026, 7, 28, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="temperature must be 0"):
        build_fact_contexts(
            [_contract(0), _contract(1)],
            config=config,
            store=RunStore.for_run(tmp_path / "runs", "temperature"),
            output_language="zh-CN",
            call=fake_call,
        )
    assert calls == []


def test_fact_context_transport_failure_falls_back_to_empty_without_repair(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "context_transport_failure")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        record = LlmCallRecord(
            call_id="call_context_failed",
            stage="rendering",
            operation_route=kwargs["operation_route"],
            provider="fake",
            model="fake",
            input_hash="sha256:fake",
            provider_status=ProviderStatus.error,
            created_at=datetime(2026, 7, 16, tzinfo=UTC),
        )
        raise LlmTransportError("offline failure", record=record, attempts=1)

    result = build_fact_contexts(
        [_contract(0), _contract(1)],
        config=config,
        store=store,
        output_language="zh-CN",
        call=fake_call,
    )
    assert len(calls) == 1
    assert calls[0]["operation_route"] == "render_fact_context_dsl"
    assert all(context.node_hints == [] for context in result.contexts)
    assert all(context.metadata["reason"] == "call_failed" for context in result.contexts)
    assert [report.kind for report in result.reports] == ["fact_context_call_failed"]
    assert result.reports[0].call_id == "call_context_failed"


def test_fact_context_length_limit_is_warning_not_fallback(tmp_path: Path) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    config = config.model_copy(
        update={
            "stage_budgets": {
                **config.stage_budgets,
                "rendering": {
                    **config.stage_budgets["rendering"],
                    "fact_context_ratio": 0.001,
                    "fact_context_min_tokens": 1,
                    "fact_context_max_tokens": 1,
                },
            }
        }
    )
    store = RunStore.for_run(tmp_path / "runs", "context_length_warning")
    calls = 0

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        nonlocal calls
        calls += 1
        return (
            "HINT C002N001 | 第一段计划已经建立了一个明确而且很长的状态。",
            LlmCallRecord(
                call_id="call_long_context",
                stage="rendering",
                operation_route=kwargs["operation_route"],
                provider="fake",
                model="fake",
                input_hash="sha256:fake",
                provider_status=ProviderStatus.success,
                created_at=datetime(2026, 7, 16, tzinfo=UTC),
            ),
        )

    result = build_fact_contexts(
        [_contract(0), _contract(1)],
        config=config,
        store=store,
        output_language="zh-CN",
        call=fake_call,
    )
    assert calls == 1
    assert result.contexts[1].node_hints[0].established
    assert result.contexts[1].metadata["context_length_warning"] is True
    assert [item.kind for item in result.reports] == ["fact_context_length_warning"]


def test_fact_context_single_chunk_with_multiple_nodes_skips_model_call(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "HINT C001N001 | 同一 chunk 的第一节点。\n"
            "HINT C001N002 | 同一 chunk 的第二节点。",
            LlmCallRecord(
                call_id="call_single_chunk",
                stage="rendering",
                operation_route=kwargs["operation_route"],
                provider="fake",
                model="fake",
                input_hash="sha256:fake",
                provider_status=ProviderStatus.success,
                created_at=datetime(2026, 7, 29, tzinfo=UTC),
            ),
        )

    result = build_fact_contexts(
        [_contract(0, two_nodes=True)],
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "context_single_chunk"),
        output_language="zh-CN",
        call=fake_call,
    )

    assert calls == []
    assert result.contexts[0].node_hints == []
    assert result.contexts[0].metadata["reason"] == "no_earlier_node"
    assert result.reports == []


def test_fact_context_single_selected_node_skips_model_call(tmp_path: Path) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "context_single_node")
    calls = 0

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        nonlocal calls
        calls += 1
        return "", LlmCallRecord(
            call_id="must-not-call",
            stage="rendering",
            operation_route=kwargs["operation_route"],
            provider="fake",
            model="fake",
            input_hash="sha256:fake",
            provider_status=ProviderStatus.success,
            created_at=datetime(2026, 7, 16, tzinfo=UTC),
        )

    result = build_fact_contexts(
        [_contract(0)],
        config=config,
        store=store,
        output_language="zh-CN",
        call=fake_call,
    )

    assert calls == 0
    assert len(result.contexts) == 1
    assert result.contexts[0].node_hints == []
    assert result.contexts[0].metadata["reason"] == "no_earlier_node"


@pytest.mark.parametrize(
    "locale, marker",
    [
        (ControlPromptLocale.zh, "【叙事提示｜…】"),
        (ControlPromptLocale.en, "[NARRATIVE NOTE | …]"),
    ],
)
def test_material_projection_is_one_block_with_inline_directives_only(
    locale: ControlPromptLocale, marker: str,
) -> None:
    contract = _contract(0, two_nodes=True)
    projected = project_chunk(contract, control_prompt_locale=locale)
    assert projected == "\n\n".join(
        (
            marker.replace("…", "先交代变化，不逐条复述"),
            "第1段原始材料甲。",
            marker.replace("…", "收束到结果"),
            "第1段原始材料乙。",
        )
    )
    for leaked in ("NODE", "ATOM", "TXT ", "role=", "op=", "speaker_turn", "N1"):
        assert leaked not in projected
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    actual_marker = projected.splitlines()[0].replace(contract.nodes[0].directive, "…")
    for loader in (load_system_prompt, load_user_prompt):
        prompt, _version = loader("render_chunk", config, control_prompt_locale=locale)
        assert actual_marker in prompt


def test_rendering_protocol_v13_contains_only_system_owned_render_rules() -> None:
    catalog = prompt_components.load_rendering_protocol_catalog()

    assert catalog.version == "rendering_protocol@v13"
    assert [key for key, _description in catalog.dynamic_blocks] == [
        "chunk_position_block",
        "host_thinking_block",
        "host_expression_block",
        "style_examples_block",
        "fact_context_block",
        "annotated_materials_block",
        "forbidden_block",
    ]
    assert set(catalog.segment_positions) == {
        "opening",
        "middle",
        "ending",
        "single",
    }
    assert catalog.style_example_protocol.selection
    assert catalog.style_example_protocol.usage
    for position in ("opening", "middle", "ending", "single"):
        assert catalog.segment_positions[position]
    for host_owned in (
        "default_viewpoint",
        "host_thinking_rules",
        "default_expression",
        "host_expression_rules",
        "style_examples_by_position",
    ):
        assert not hasattr(catalog, host_owned)


@pytest.mark.parametrize(
    "invalid_case, error_match",
    [
        ("missing_position", "segment_positions keys must match exactly"),
        ("empty_usage", "non-empty style_example_protocol.usage"),
    ],
)
def test_rendering_protocol_v13_rejects_invalid_system_catalogs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
    error_match: str,
) -> None:
    payload = json.loads(
        (REPO_ROOT / "prompt/rendering/components/protocol.zh.json").read_text(
            encoding="utf-8"
        )
    )
    if invalid_case == "missing_position":
        del payload["segment_positions"]["middle"]
    elif invalid_case == "empty_usage":
        payload["style_example_protocol"]["usage"] = []
    else:  # pragma: no cover - protects the parametrized fixture itself.
        raise AssertionError(f"unknown invalid case: {invalid_case}")

    invalid_catalog = tmp_path / f"{invalid_case}.json"
    invalid_catalog.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            prompt_components,
            "_RENDERING_PROTOCOL",
            invalid_catalog,
        )
        prompt_components.load_rendering_protocol_catalog.cache_clear()
        with pytest.raises(ValueError, match=error_match):
            prompt_components.load_rendering_protocol_catalog()
    prompt_components.load_rendering_protocol_catalog.cache_clear()


def test_v1_render_style_examples_keep_historical_two_per_position() -> None:
    catalog = prompt_components.load_rendering_protocol_catalog()
    persona = _render_persona()

    for position in ("opening", "middle", "ending", "single"):
        block = render_style_examples_block(persona, position, sample_key="ignored-for-v1")
        assert catalog.style_example_protocol.selection in block
        assert all(rule in block for rule in catalog.style_example_protocol.usage)
        assert "虚构材料" not in block
        assert block.count("[外部转换示例的输入｜不是当前材料]") == 2
        assert block.count("[外部转换示例的正文｜不是当前事实来源]") == 2
        assert block.count("[学习点]") == 2
        selected = persona.style_examples_by_position[position]
        assert all(
            value in block
            for example in selected
            for value in (example.source, example.render, example.lesson)
        )
        assert all(
            value not in block
            for other_position, examples in persona.style_examples_by_position.items()
            if other_position != position
            for example in examples
            for value in (example.source, example.render, example.lesson)
        )


def test_v2_normal_position_projects_one_stable_sample_and_varies_across_run_keys() -> None:
    persona = coerce_persona(_v2_host_with_pool())
    stable = render_style_examples_block(persona, "middle", sample_key="run-stable")

    assert stable == render_style_examples_block(
        persona,
        "middle",
        sample_key="run-stable",
    )
    assert stable.count("[外部转换示例的输入｜不是当前材料]") == 1
    assert stable.count("来源位置：middle") == 1
    assert (
        sum(
            example.source in stable
            for example in persona.style_examples_by_position["middle"]
        )
        == 1
    )

    blocks = {
        render_style_examples_block(persona, "middle", sample_key=f"run-{index}")
        for index in range(64)
    }
    assert len(blocks) > 1


def test_v2_single_projects_one_opening_and_one_ending_stably_per_key() -> None:
    persona = coerce_persona(_v2_host_with_pool())
    stable = render_style_examples_block(persona, "single", sample_key="run-stable")

    assert stable == render_style_examples_block(
        persona,
        "single",
        sample_key="run-stable",
    )
    assert stable.count("[外部转换示例的输入｜不是当前材料]") == 2
    assert stable.count("来源位置：opening") == 1
    assert stable.count("来源位置：ending") == 1
    assert all(
        sum(example.source in stable for example in persona.style_examples_by_position[position])
        == 1
        for position in ("opening", "ending")
    )
    assert all(
        example.source not in stable
        for example in persona.style_examples_by_position["middle"]
    )

    blocks = {
        render_style_examples_block(persona, "single", sample_key=f"run-{index}")
        for index in range(64)
    }
    assert len(blocks) > 1


def test_render_positions_derive_from_complete_ordered_contracts() -> None:
    contracts = [_contract(3), _contract(0), _contract(2), _contract(1)]
    positions = derive_render_chunk_positions(contracts)

    assert [
        (
            positions[f"chunk_{index}"].chunk_index,
            positions[f"chunk_{index}"].chunk_count,
            positions[f"chunk_{index}"].segment_position,
        )
        for index in range(1, 5)
    ] == [
        (1, 4, "opening"),
        (2, 4, "middle"),
        (3, 4, "middle"),
        (4, 4, "ending"),
    ]
    single = derive_render_chunk_positions([_contract(0)])["chunk_1"]
    assert (
        single.chunk_index,
        single.chunk_count,
        single.segment_position,
    ) == (1, 1, "single")

    duplicate = _contract(1).model_copy(update={"chunk_id": "chunk_1"})
    with pytest.raises(ValueError, match="unique chunk ids"):
        derive_render_chunk_positions([_contract(0), duplicate])

    duplicate_order = _contract(1).model_copy(update={"order_index": 0})
    with pytest.raises(ValueError, match="unique order_index values"):
        derive_render_chunk_positions([_contract(0), duplicate_order])


@pytest.mark.parametrize(
    "kwargs, error_match",
    [
        ({"chunk_index": True, "chunk_count": 1}, "must be integers"),
        ({"chunk_index": 1, "chunk_count": 0}, "at least 1"),
        ({"chunk_index": 0, "chunk_count": 2}, "within 1..chunk_count"),
        ({"chunk_index": 3, "chunk_count": 2}, "within 1..chunk_count"),
        (
            {
                "chunk_index": 1,
                "chunk_count": 2,
                "segment_position": "middle",
            },
            "does not match",
        ),
        (
            {
                "chunk_index": 1,
                "chunk_count": 1,
                "segment_position": "opening",
            },
            "does not match",
        ),
    ],
)
def test_render_position_block_rejects_invalid_explicit_scalars(
    kwargs: dict[str, Any],
    error_match: str,
) -> None:
    with pytest.raises(ValueError, match=error_match):
        render_chunk_position_block(**kwargs)


def test_render_blocks_have_fixed_v10_shape_and_fact_context() -> None:
    context = RenderFactContext(
        chunk_id="chunk_1",
        order_index=0,
        node_hints=[
            RenderFactContextNodeHint(
                source_node_id="cand_0_1",
                established="此前计划已经安排同一场袭击。",
            ),
            RenderFactContextNodeHint(
                source_node_id="cand_0_1",
                established="首次追问袭击原因的动作也已安排。",
            ),
        ],
    )
    blocks = build_render_blocks(
        contract=_contract(0),
        persona=_render_persona(),
        fact_context=context,
    )
    assert set(blocks) == {
        "chunk_position_block",
        "host_thinking_block",
        "host_expression_block",
        "style_examples_block",
        "fact_context_block",
        "annotated_materials_block",
        "forbidden_block",
    }
    assert blocks["chunk_position_block"].splitlines()[:3] == [
        "chunk_index: 1",
        "chunk_count: 1",
        "segment_position: single",
    ]
    assert blocks["fact_context_block"] == (
        "计划去重提示（非事实、非命令，不能授权删除当前材料新增量）：\n"
        "- 此前计划已经安排同一场袭击。\n"
        "- 首次追问袭击原因的动作也已安排。"
    )
    assert "cand_0_1" not in blocks["fact_context_block"]
    assert "第1段的主事实" not in blocks["fact_context_block"]
    assert "【叙事提示｜" not in blocks["fact_context_block"]
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    system_prompt, _ = load_system_prompt("render_chunk", config)
    assert (
        "fact context 是正文生成前从完整 ordered node SUM/DIRECTIVE "
        "编排得到的计划去重提示"
    ) in system_prompt
    assert "不能单独作为删除、省略或压缩当前材料的授权" in system_prompt


def test_render_block_overrides_allow_only_two_host_blocks() -> None:
    blocks = build_render_blocks(
        contract=_contract(0),
        persona=_render_persona(),
    )
    overridden = apply_render_block_overrides(
        blocks,
        {
            "host_thinking_block": "新的观察方式。",
            "host_expression_block": "新的表达方式。",
        },
    )
    assert overridden["host_thinking_block"] == "新的观察方式。"
    assert overridden["host_expression_block"] == "新的表达方式。"
    assert overridden["chunk_position_block"] == blocks["chunk_position_block"]
    assert overridden["style_examples_block"] == blocks["style_examples_block"]

    with pytest.raises(ValueError, match="unsupported: style_examples_block"):
        apply_render_block_overrides(
            blocks,
            {"style_examples_block": "不得跨 chunk 复用。"},
        )


def _name_resolution_for_named_contract() -> NameResolutionPlan:
    return NameResolutionPlan(
        plan_id="name-resolution-named",
        project_id="proj",
        output_language="zh-CN",
        input_hash="sha256:test",
        contract_ids=["contract_named"],
        rules=[
            NameResolutionRule(
                rule_id="rule_terra",
                aliases=["Terra"],
                stable_name="泰拉",
                evidence_chunk_ids=["chunk_named"],
            ),
            NameResolutionRule(
                rule_id="rule_locke",
                aliases=["Locke"],
                stable_name="洛克",
                evidence_chunk_ids=["chunk_named"],
            ),
        ],
    )


def _named_contract() -> RenderContract:
    materials = [
        RenderContractMaterial(
            material_ref="R001",
            source_material_id="mat_named_1",
            node_ref="N1",
            operation=NodeEditOperation.keep,
            content="Terra opens the gate. Terra waits for Locke.",
            language="en",
            content_kind=ContentKind.narration,
        )
    ]
    return RenderContract(
        contract_id="contract_named",
        chunk_id="chunk_named",
        order_index=0,
        project_id="proj",
        persona_id="host",
        nodes=[
            RenderContractNode(
                node_ref="N1",
                source_node_id="cand_named_1",
                order_in_chunk=0,
                role=Role.core,
                treatment=Treatment.paraphrase,
                material_refs=["R001"],
            )
        ],
        materials=materials,
    )


def test_render_blocks_apply_resolution_directly_without_a_name_hint_block() -> None:
    contract = _named_contract()
    raw = contract.model_dump(mode="json")
    blocks = build_render_blocks(
        contract=contract,
        persona=_render_persona(),
        name_resolution=_name_resolution_for_named_contract(),
    )
    assert "泰拉 opens the gate. 泰拉 waits for 洛克." in blocks[
        "annotated_materials_block"
    ]
    assert "name_reference_block" not in blocks
    assert contract.model_dump(mode="json") == raw


def test_render_user_prompt_has_seven_independent_blocks_without_name_hint_or_prior_body() -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    template, _version = load_user_prompt("render_chunk", config)
    rendered = render_chunk_user_prompt(
        template=template,
        contract=_contract(0),
        persona=_render_persona(),
        output_language="zh-CN",
        fact_context=RenderFactContext(
            chunk_id="chunk_1",
            order_index=0,
            node_hints=[
                RenderFactContextNodeHint(
                    source_node_id="cand_0_1",
                    established="此前计划状态",
                )
            ],
        ),
    )
    headings = [
        "## chunk position",
        "## host thinking",
        "## host expression",
        "## style examples",
        "## fact context",
        "## source materials",
        "## forbidden",
    ]
    assert [rendered.index(item) for item in headings] == sorted(
        rendered.index(item) for item in headings
    )
    assert "previous" not in rendered.casefold()
    assert "上一句。" not in rendered
    assert "name reference" not in rendered.casefold()
    assert "chunk_index: 1" in rendered
    assert "chunk_count: 1" in rendered
    assert "segment_position: single" in rendered


def test_render_chunks_run_independently_and_return_ordered_artifacts(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "parallel_render")
    barrier = Barrier(4, timeout=5)
    lock = Lock()
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        with lock:
            calls.append(kwargs)
            index = len(calls)
        barrier.wait()
        unit = kwargs["work_unit"]
        return f"{unit.unit_id} 正文。", LlmCallRecord(
            call_id=f"parallel-call-{index}",
            stage="rendering",
            operation_route=kwargs["operation_route"],
            provider="fake",
            model="fake",
            input_hash="sha256:fake",
            provider_status=ProviderStatus.success,
            created_at=datetime(2026, 7, 16, tzinfo=UTC),
        )

    contracts = [_contract(0), _contract(1), _contract(2), _contract(3)]
    result = render_chunks(
        list(reversed(contracts)),
        persona=_host(),
        config=config,
        store=store,
        call=fake_call,
        name_resolution=_empty_name_resolution(contracts),
        output_language="zh-CN",
    )
    assert len(calls) == 4
    assert [item.chunk_id for item in result.artifacts] == [
        "chunk_1",
        "chunk_2",
        "chunk_3",
        "chunk_4",
    ]
    assert [item.text for item in result.artifacts] == [
        "chunk_1 正文。",
        "chunk_2 正文。",
        "chunk_3 正文。",
        "chunk_4 正文。",
    ]
    assert all("previous" not in call["messages"][1]["content"].casefold() for call in calls)
    expected_positions = {
        "chunk_1": (1, 4, "opening"),
        "chunk_2": (2, 4, "middle"),
        "chunk_3": (3, 4, "middle"),
        "chunk_4": (4, 4, "ending"),
    }
    calls_by_chunk = {call["work_unit"].unit_id: call for call in calls}
    artifacts_by_chunk = {artifact.chunk_id: artifact for artifact in result.artifacts}
    for chunk_id, (chunk_index, chunk_count, segment_position) in (
        expected_positions.items()
    ):
        unit = calls_by_chunk[chunk_id]["work_unit"]
        assert unit.metadata["chunk_index"] == chunk_index
        assert unit.metadata["chunk_count"] == chunk_count
        assert unit.metadata["segment_position"] == segment_position
        prompt = calls_by_chunk[chunk_id]["messages"][1]["content"]
        assert f"chunk_index: {chunk_index}" in prompt
        assert f"chunk_count: {chunk_count}" in prompt
        assert f"segment_position: {segment_position}" in prompt
        artifact_metadata = artifacts_by_chunk[chunk_id].metadata
        assert artifact_metadata["chunk_index"] == chunk_index
        assert artifact_metadata["chunk_count"] == chunk_count
        assert artifact_metadata["segment_position"] == segment_position


def test_renderer_rejects_fact_hint_mounted_to_node_outside_chunk(
    tmp_path: Path,
) -> None:
    contract = _contract(0)
    context = RenderFactContext(
        chunk_id=contract.chunk_id,
        order_index=contract.order_index,
        node_hints=[
            RenderFactContextNodeHint(
                source_node_id="candidate_from_another_chunk",
                established="另一节点的动作已在更早计划安排。",
            )
        ],
    )
    calls = 0

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        nonlocal calls
        calls += 1
        return "must not render", _fake_record(kwargs, "must-not-call")

    with pytest.raises(ValueError, match="nodes outside"):
        render_chunks(
            [contract],
            persona=_host(),
            config=RuntimeConfig.load(
                REPO_ROOT / "configs" / "runtime.example.json",
                load_env=False,
            ),
            store=RunStore.for_run(tmp_path / "runs", "foreign-render-hint"),
            call=fake_call,
            name_resolution=_empty_name_resolution([contract]),
            output_language="zh-CN",
            fact_contexts=[context],
        )
    assert calls == 0


# ---------------------------------------------------------------------------
# Degenerate-repetition detector (tracecast.rendering.degenerate)
# ---------------------------------------------------------------------------


def test_longest_repetition_run_detects_long_single_character_run() -> None:
    text = "正常开场白。" + "啊" * 500 + "正常收尾。"
    assert longest_repetition_run(text) >= 500
    assert is_degenerate_repetition(text) is True


def test_is_degenerate_repetition_ignores_short_stylistic_chinese_repeats() -> None:
    # Plausible narrated-prose repeats (onomatopoeia, ellipsis runs) must not
    # trip the detector -- these are well under the ~30-char threshold.
    for sample in ("啊啊啊啊啊啊", "……", "哈哈哈哈哈", "呜呜呜呜呜呜呜"):
        assert is_degenerate_repetition(sample) is False, sample


def test_is_degenerate_repetition_ignores_ordinary_narrative_prose() -> None:
    prose = (
        "这是一段普通的旁白，讲述主角穿过城市，回忆起过去的种种经历，"
        "情绪起伏之间夹杂着淡淡的忧伤和对未来的期待。"
    ) * 4
    assert is_degenerate_repetition(prose) is False


def test_longest_repetition_run_detects_multi_character_period_repeats() -> None:
    # Period-2 unit ("啊哈") and period-8 unit ("12345678"), not just
    # single-character loops -- the detector claims to scan periods 1..8.
    two_char_period = "啊哈" * 20
    eight_char_period = "12345678" * 6
    assert is_degenerate_repetition(two_char_period) is True
    assert is_degenerate_repetition(eight_char_period) is True


def test_is_degenerate_repetition_boundary_at_min_run_threshold() -> None:
    assert is_degenerate_repetition("啊" * 29) is False
    assert is_degenerate_repetition("啊" * 30) is True


# ---------------------------------------------------------------------------
# render_chunks degenerate-output retry behavior
# ---------------------------------------------------------------------------


def _fake_record(kwargs: dict[str, Any], call_id: str) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage="rendering",
        operation_route=kwargs["operation_route"],
        provider="fake",
        model="fake",
        input_hash="sha256:fake",
        provider_status=ProviderStatus.success,
        created_at=datetime(2026, 7, 16, tzinfo=UTC),
    )


def test_render_chunks_retries_once_on_degenerate_output_then_recovers(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "degenerate_recovers")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        attempt = kwargs["work_unit"].metadata["attempt"]
        text = "啊" * 200 if attempt == 1 else "chunk_1 正文（已恢复）。"
        return text, _fake_record(kwargs, f"call_{len(calls)}")

    contracts = [_contract(0)]
    result = render_chunks(
        contracts,
        persona=_host(),
        config=config,
        store=store,
        call=fake_call,
        name_resolution=_empty_name_resolution(contracts),
        output_language="zh-CN",
    )

    assert len(calls) == 2
    assert result.artifacts[0].text == "chunk_1 正文（已恢复）。"
    degenerate_reports = [r for r in result.reports if r.kind == "render_degenerate_output"]
    assert len(degenerate_reports) == 1
    report = degenerate_reports[0]
    assert report.level == "warning"
    assert report.usable is True
    assert report.metadata == {"attempts": 2}


def test_render_chunks_exhausts_retries_and_ships_last_degenerate_text_with_error_report(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "degenerate_exhausted")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        attempt = kwargs["work_unit"].metadata["attempt"]
        # Each attempt's body is distinguishable (still degenerate, since the
        # long repeated run stays intact) so the assertions below can prove
        # the *last* attempt's text ships, not the first.
        text = f"{'啊' * 300}#attempt{attempt}"
        return text, _fake_record(kwargs, f"call_{len(calls)}")

    contracts = [_contract(0)]
    result = render_chunks(
        contracts,
        persona=_host(),
        config=config,
        store=store,
        call=fake_call,
        name_resolution=_empty_name_resolution(contracts),
        output_language="zh-CN",
    )

    assert len(calls) == RENDER_DEGENERATE_MAX_ATTEMPTS
    expected_last_text = f"{'啊' * 300}#attempt{RENDER_DEGENERATE_MAX_ATTEMPTS}"
    assert result.artifacts[0].text == expected_last_text
    assert is_degenerate_repetition(result.artifacts[0].text) is True
    degenerate_reports = [r for r in result.reports if r.kind == "render_degenerate_output"]
    assert len(degenerate_reports) == 1
    report = degenerate_reports[0]
    assert report.level == "error"
    assert report.usable is False
    assert report.metadata == {"attempts": RENDER_DEGENERATE_MAX_ATTEMPTS}


def test_render_chunks_clean_first_attempt_produces_no_degenerate_report(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "degenerate_happy_path")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        unit = kwargs["work_unit"]
        return f"{unit.unit_id} 正常正文。", _fake_record(kwargs, f"call_{len(calls)}")

    contracts = [_contract(0), _contract(1)]
    result = render_chunks(
        contracts,
        persona=_host(),
        config=config,
        store=store,
        call=fake_call,
        name_resolution=_empty_name_resolution(contracts),
        output_language="zh-CN",
    )

    assert len(calls) == 2
    assert [item.text for item in result.artifacts] == [
        "chunk_1 正常正文。",
        "chunk_2 正常正文。",
    ]
    assert not any(r.kind == "render_degenerate_output" for r in result.reports)


def test_render_chunks_degenerate_retry_reuses_identical_route_and_messages(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "degenerate_retry_identical_request")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        attempt = kwargs["work_unit"].metadata["attempt"]
        text = "啊" * 200 if attempt == 1 else "chunk_1 正文（已恢复）。"
        return text, _fake_record(kwargs, f"call_{len(calls)}")

    contracts = [_contract(0)]
    render_chunks(
        contracts,
        persona=_host(),
        config=config,
        store=store,
        call=fake_call,
        name_resolution=_empty_name_resolution(contracts),
        output_language="zh-CN",
    )

    assert len(calls) == 2
    first_call, retry_call = calls
    assert first_call["operation_route"] == retry_call["operation_route"]
    assert first_call["messages"] == retry_call["messages"]
    assert first_call["config"] == retry_call["config"]
    first_unit, retry_unit = first_call["work_unit"], retry_call["work_unit"]
    assert first_unit.unit_id == retry_unit.unit_id
    assert first_unit.order_index == retry_unit.order_index
    assert first_unit.input_refs == retry_unit.input_refs
    # Only the attempt counter in metadata is allowed to differ -- this is a
    # plain re-ask, not a repair-prompt variant.
    assert first_unit.metadata["attempt"] == 1
    assert retry_unit.metadata["attempt"] == 2
    assert {k: v for k, v in first_unit.metadata.items() if k != "attempt"} == {
        k: v for k, v in retry_unit.metadata.items() if k != "attempt"
    }


def test_render_chunks_mixed_degenerate_and_clean_chunks_render_independently_in_order(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.load(REPO_ROOT / "configs" / "runtime.example.json", load_env=False)
    store = RunStore.for_run(tmp_path / "runs", "degenerate_mixed_chunks")
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        unit = kwargs["work_unit"]
        attempt = unit.metadata["attempt"]
        with lock:
            calls.append(kwargs)
            index = len(calls)
        if attempt == 1:
            # Both chunks' first attempts must be in flight together, proving
            # this chunk's retry doesn't block the other chunk's initial call.
            barrier.wait()
        if unit.unit_id == "chunk_2" and attempt == 1:
            text = "啊" * 200
        else:
            text = f"{unit.unit_id} 正文。"
        return text, _fake_record(kwargs, f"parallel-call-{index}")

    contracts = [_contract(0), _contract(1)]
    result = render_chunks(
        list(reversed(contracts)),
        persona=_host(),
        config=config,
        store=store,
        call=fake_call,
        name_resolution=_empty_name_resolution(contracts),
        output_language="zh-CN",
    )

    assert len(calls) == 3
    assert [item.chunk_id for item in result.artifacts] == ["chunk_1", "chunk_2"]
    assert [item.text for item in result.artifacts] == [
        "chunk_1 正文。",
        "chunk_2 正文。",
    ]
    degenerate_reports = [r for r in result.reports if r.kind == "render_degenerate_output"]
    assert len(degenerate_reports) == 1
    assert degenerate_reports[0].artifact_ref == "chunk_2"
    assert degenerate_reports[0].metadata == {"attempts": 2}
