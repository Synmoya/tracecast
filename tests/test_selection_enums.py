"""Independent tests for deterministic path enum compilation before DIR generation."""

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
    Treatment,
)
from tracecast.planning.selection_plan import build_selection_plan
from tracecast.planning.vocab import (
    SELECTOR_DIRECTIVE_ROUTE,
    SELECTOR_ROUTE,
)
from tracecast.storage import RunStore, RuntimeConfig


def _graph(count: int) -> CandidateGraph:
    return CandidateGraph(
        graph_id="graph-selection",
        project_id="proj",
        nodes=[
            CandidateNode(
                candidate_id=f"cand_{index}",
                source_window="window",
                summary=f"节点 {index}",
                material_ids=[f"mat_{index}"],
                metadata={"source_ref": "script"},
            )
            for index in range(1, count + 1)
        ],
    )


def _record(kwargs: dict[str, Any], call_id: str) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage="selection",
        operation_route=kwargs["operation_route"],
        provider="offline",
        model="fake",
        input_hash=f"hash-{call_id}",
        provider_status=ProviderStatus.success,
        created_at=datetime(2026, 7, 27, tzinfo=UTC),
        work_unit=kwargs.get("work_unit"),
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    route_templates = {
        SELECTOR_ROUTE: (
            "{{source_token_estimate_line}}\n{{target_output_estimate_line}}\n"
            "{{source_aliases}}\n{{persona_block}}\n"
            "{{narrative_function_descriptions}}\n{{graph_blocks}}"
        ),
        SELECTOR_DIRECTIVE_ROUTE: "{{persona_block}}\n{{path_blocks}}",
    }
    routes: dict[str, dict[str, str]] = {}
    for route, template in route_templates.items():
        system = prompt_dir / f"{route}.system.md"
        user = prompt_dir / f"{route}.user.md"
        system.write_text(f"<!-- version: {route}@test -->\nsystem", encoding="utf-8")
        user.write_text(
            f"<!-- version: {route}_user@test -->\n{template}",
            encoding="utf-8",
        )
        routes[route] = {
            "stage": "selection",
            "provider": "offline",
            "model": "fake",
            "system_prompt": str(system),
            "user_prompt": str(user),
        }
    return RuntimeConfig.model_validate({"routes": routes})


def _accepted(output: Any) -> list[Any]:
    return [
        row
        for row in output.plan.decisions
        if row.status == SelectionStatus.accepted
    ]


def test_qps_and_known_aliases_compile_before_directives(tmp_path: Path) -> None:
    tokens = ["q", "p", "s", "quote", "para", "paraphase", "sum", "summarize"]
    expected = [
        Treatment.quote,
        Treatment.paraphrase,
        Treatment.summarize,
        Treatment.quote,
        Treatment.paraphrase,
        Treatment.paraphrase,
        Treatment.summarize,
        Treatment.summarize,
    ]
    calls: list[str] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        route = kwargs["operation_route"]
        calls.append(route)
        if route == SELECTOR_ROUTE:
            rows = [
                f"P C{index:03d} | context | {token}"
                for index, token in enumerate(tokens, start=1)
            ]
            return "\n".join(rows), _record(kwargs, "path-call")
        assert route == SELECTOR_DIRECTIVE_ROUTE
        rows = [f"D P{index:03d} | -" for index in range(1, len(tokens) + 1)]
        return "\n".join(rows), _record(kwargs, "dir-call")

    output = build_selection_plan(
        _graph(len(tokens)),
        persona={"persona_id": "host"},
        config=_config(tmp_path),
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )

    assert calls == [SELECTOR_ROUTE, SELECTOR_DIRECTIVE_ROUTE]
    assert [row.treatment for row in _accepted(output)] == expected
    assert [row.directive for row in _accepted(output)] == [None] * len(tokens)
    normalized = [
        report
        for report in output.reports
        if report.kind == "selection_treatment_normalized"
    ]
    assert len(normalized) == 5


def test_unknown_enums_omit_only_their_rows_before_directives(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        route = kwargs["operation_route"]
        calls.append(kwargs)
        if route == SELECTOR_ROUTE:
            return (
                "P C002 | mystery | p\n"
                "P C001 | core | detail\n"
                "P C003 | bridge | s",
                _record(kwargs, "path-call"),
            )
        assert route == SELECTOR_DIRECTIVE_ROUTE
        return "D P001 | -", _record(kwargs, "dir-call")

    output = build_selection_plan(
        _graph(3),
        persona={"persona_id": "host"},
        config=_config(tmp_path),
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )

    assert [call["operation_route"] for call in calls] == [
        SELECTOR_ROUTE,
        SELECTOR_DIRECTIVE_ROUTE,
    ]
    assert output.plan.selected_sequence == ["cand_3"]
    [accepted] = _accepted(output)
    assert (accepted.role.value, accepted.treatment) == (
        "bridge",
        Treatment.summarize,
    )
    directive_prompt = calls[1]["messages"][1]["content"]
    assert "SUM 节点 3" in directive_prompt
    assert "节点 1" not in directive_prompt and "节点 2" not in directive_prompt
    assert {report.kind for report in output.reports} >= {
        "invalid_role",
        "invalid_treatment",
    }
