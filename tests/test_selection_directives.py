"""Independent regression tests for the path/DIR split in Selection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracecast.domain import (
    CandidateGraph,
    CandidateNode,
    LlmCallRecord,
    ProviderStatus,
    SelectionStatus,
    SourceSpan,
)
from tracecast.llm import LlmTransportError
from tracecast.planning.selection_plan import build_selection_plan
from tracecast.planning.vocab import (
    SELECTOR_DIRECTIVE_ROUTE,
    SELECTOR_ROUTE,
)
from tracecast.storage import RunStore, RuntimeConfig


def _node(index: int) -> CandidateNode:
    return CandidateNode(
        candidate_id=f"cand_{index}",
        source_window="window",
        summary=f"节点 {index} 摘要",
        material_ids=[f"mat_{index}"],
        narrative_function="state_change",
        source_span=SourceSpan(start=index, end=index),
        metadata={"source_ref": "script"},
    )


def _graph(count: int = 3) -> CandidateGraph:
    return CandidateGraph(
        graph_id="graph-directives",
        project_id="proj",
        nodes=[_node(index) for index in range(1, count + 1)],
    )


def _record(
    kwargs: dict[str, Any],
    call_id: str,
    *,
    status: ProviderStatus = ProviderStatus.success,
) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage="selection",
        operation_route=kwargs["operation_route"],
        provider="offline",
        model="fake",
        input_hash=f"hash-{call_id}",
        provider_status=status,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        prompt_version=kwargs.get("prompt_version"),
        work_unit=kwargs.get("work_unit"),
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    templates = {
        SELECTOR_ROUTE: (
            "path system",
            "{{source_token_estimate_line}}\n{{target_output_estimate_line}}\n"
            "{{source_aliases}}\n{{persona_block}}\n"
            "{{narrative_function_descriptions}}\n{{graph_blocks}}",
        ),
        SELECTOR_DIRECTIVE_ROUTE: (
            "directive system",
            "PERSONA\n{{persona_block}}\nPATH\n{{path_blocks}}",
        ),
    }
    routes: dict[str, dict[str, Any]] = {}
    for route, (system, user) in templates.items():
        system_path = prompt_dir / f"{route}.system.md"
        user_path = prompt_dir / f"{route}.user.md"
        system_path.write_text(
            f"<!-- version: {route}_system@test -->\n{system}",
            encoding="utf-8",
        )
        user_path.write_text(
            f"<!-- version: {route}_user@test -->\n{user}",
            encoding="utf-8",
        )
        routes[route] = {
            "stage": "selection",
            "provider": "offline",
            "model": "fake",
            "system_prompt": str(system_path),
            "user_prompt": str(user_path),
        }
    return RuntimeConfig.model_validate({"routes": routes})


def _accepted(output: Any) -> list[Any]:
    return [
        decision
        for decision in output.plan.decisions
        if decision.status == SelectionStatus.accepted
    ]


def test_global_directives_map_fresh_path_aliases_without_changing_path(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        route = kwargs["operation_route"]
        if route == SELECTOR_ROUTE:
            return (
                "P C003 | core | q\nP C001 | bridge | s",
                _record(kwargs, "path-call"),
            )
        assert route == SELECTOR_DIRECTIVE_ROUTE
        return (
            "D P001 | 用第三个节点形成开场疑问。\nD P002 | -",
            _record(kwargs, "dir-call"),
        )

    output = build_selection_plan(
        _graph(),
        persona={"persona_id": "host", "expression": "克制"},
        config=_config(tmp_path),
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )

    assert output.plan.selected_sequence == ["cand_3", "cand_1"]
    assert [row.candidate_id for row in _accepted(output)] == ["cand_3", "cand_1"]
    assert [row.directive for row in _accepted(output)] == [
        "用第三个节点形成开场疑问。",
        None,
    ]
    directive_call = calls[1]
    directive_prompt = directive_call["messages"][1]["content"]
    assert "P P001 | role=core | treatment=quote" in directive_prompt
    assert "SUM 节点 3 摘要" in directive_prompt
    assert "P P002 | role=bridge | treatment=summarize" in directive_prompt
    assert "SUM 节点 1 摘要" in directive_prompt
    assert "mat_3" not in directive_prompt
    assert directive_call["work_unit"].kind == "selection_directives"
    assert directive_call["work_unit"].input_refs == ["cand_3", "cand_1"]
    assert directive_call["work_unit"].metadata == {"path_call_id": "path-call"}
    assert output.plan.metadata["directive_call_id"] == "dir-call"
    assert output.plan.metadata["directive_protocol"].endswith(
        "selector_directive_dsl_user@test"
    )
    assert all(row.metadata["directive_call_id"] == "dir-call" for row in _accepted(output))


def test_duplicate_missing_and_unknown_aliases_fall_back_to_null(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        route = kwargs["operation_route"]
        if route == SELECTOR_ROUTE:
            return (
                "P C001 | core | p\nP C002 | bridge | s\nP C003 | color | p",
                _record(kwargs, "path-call"),
            )
        if route == SELECTOR_DIRECTIVE_ROUTE:
            return (
                "D P001 | 开场已锁定。\n"
                "D P002 | 第一次重复。\n"
                "D P002 | 第二次重复。\n"
                "D P999 | 不存在。",
                _record(kwargs, "dir-call"),
            )
        raise AssertionError(f"unexpected route {route}")

    output = build_selection_plan(
        _graph(),
        persona={
            "persona_id": "host",
            "viewpoint": "优先追踪人物能动性的变化",
            "expression": "克制但有洞察力",
            "viewpoint_profile": {
                "human_directive": ["让听众注意每次选择的代价"],
            },
        },
        config=_config(tmp_path),
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )

    assert [call["operation_route"] for call in calls] == [
        SELECTOR_ROUTE,
        SELECTOR_DIRECTIVE_ROUTE,
    ]
    assert output.plan.selected_sequence == ["cand_1", "cand_2", "cand_3"]
    assert [row.directive for row in _accepted(output)] == [
        "开场已锁定。",
        None,
        None,
    ]
    kinds = {report.kind for report in output.reports}
    assert {
        "directive_duplicate_alias",
        "directive_unknown_alias",
        "directive_missing_alias",
    } <= kinds


def test_directive_transport_failure_keeps_locked_path_with_null_directives(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs["operation_route"])
        if kwargs["operation_route"] == SELECTOR_ROUTE:
            return "P C002 | core | p", _record(kwargs, "path-call")
        failed = _record(kwargs, "dir-failed", status=ProviderStatus.error)
        raise LlmTransportError("offline failure", record=failed, attempts=1)

    output = build_selection_plan(
        _graph(),
        persona={"persona_id": "host"},
        config=_config(tmp_path),
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )

    assert calls == [SELECTOR_ROUTE, SELECTOR_DIRECTIVE_ROUTE]
    assert output.plan.selected_sequence == ["cand_2"]
    assert _accepted(output)[0].directive is None
    assert output.plan.metadata["directive_call_id"] == "dir-failed"
    failure = next(
        report
        for report in output.reports
        if report.kind == "selection_directive_failed"
    )
    assert failure.call_id == "dir-failed"
    assert failure.usable is True
