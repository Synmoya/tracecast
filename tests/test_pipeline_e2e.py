"""Offline full-chain regression for the canonical pipeline use case."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from tracecast.control_prompt import SourceLanguageClass
from tracecast.domain import (
    CandidateGraph,
    CandidateNode,
    ContentKind,
    LlmCallRecord,
    MaterialAtom,
    Provenance,
    ProviderStatus,
    Role,
    SelectionDecision,
    SelectionPlan,
    SelectionStatus,
    TrustTier,
)
from tracecast.host_builder import run_host_build
from tracecast.host_builder.pipeline import HOST_EXAMPLES_ROUTE, HOST_PROFILE_ROUTE
from tracecast.hosts import HostProfile
from tracecast.ingestion import IngestionError
from tracecast.name_resolution import (
    NAME_TABLE_ROUTE,
    NameResolutionMode,
)
from tracecast.node_edit import build_node_edit_outputs
from tracecast.pipeline import run_pipeline, run_render_tail
from tracecast.project import ProjectConfig, load_project
from tracecast.storage import (
    ARTIFACT_SPECS,
    ASSEMBLY_REPORT,
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    LLM_CALL_RECORDS,
    MATERIAL_ATOMS,
    NAME_RESOLUTION_PLAN,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SCRIPT,
    SELECTION_PLAN,
    SOURCE_PIECES,
    SOURCE_SNAPSHOTS,
    SOURCE_WINDOWS,
    RunStore,
    RuntimeConfig,
    sha256_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _host() -> HostProfile:
    payload = json.loads(
        (REPO_ROOT / "hosts" / "example_host_zh.json").read_text(encoding="utf-8")
    )
    payload.update(
        {
            "schema_version": "tracecast-host/1",
            "host_id": "host",
            "display_name": "Offline Test Host",
            "version": "offline_test_host@v1",
        }
    )
    payload["host_thinking"].update(
        {
            "viewpoint": "只讲材料明确支持的行动与结果。",
            "human_directive": [
                "务必纳入并比较 Vaelor 的路线行动与向导对路线变化的独立佐证。"
            ],
            "rules": ["离线测试 host 的思考规则。"],
        }
    )
    payload["host_expression"].update(
        {
            "description": "自然、克制的中文播客口吻。",
            "rules": ["离线测试 host 的表达规则。"],
        }
    )
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


def _v2_host() -> HostProfile:
    payload = _host().model_dump(mode="json")
    payload["schema_version"] = "tracecast-host/2"
    payload["style_examples_by_position"].pop("single", None)
    for examples in payload["style_examples_by_position"].values():
        for example in examples:
            example["lesson"] = None
    return HostProfile.model_validate(payload)


class LedgerFakeCall:
    """Provider seam that preserves the real call-ledger/raw-payload contract."""

    def __init__(self, response_overrides: dict[str, str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = Lock()
        self._response_overrides = dict(response_overrides or {})

    def __call__(self, **kwargs: Any) -> tuple[str, LlmCallRecord]:
        with self._lock:
            self.calls.append(kwargs)
            call_number = len(self.calls)
        route = kwargs["operation_route"]
        responses = {
            HOST_PROFILE_ROUTE: (
                "OUTPUT_LANGUAGE zh-CN\n"
                "VIEWPOINT 关注人物行动与后果。\n"
                "PREFER 可核验的转折\n"
                "AVOID 空泛结论\n"
                "THINKING_RULE 区分事实与判断。\n"
                "EXPRESSION 自然克制的中文播客口语。\n"
                "EXPRESSION_RULE 判断紧随证据。"
            ),
            HOST_EXAMPLES_ROUTE: "\n".join(
                "\n".join(
                    (
                        f"EXAMPLE E{index:03d}",
                        "SOURCE",
                        f"第{index}条朴素内容骨架。",
                        "ENDSOURCE",
                        "ENDEXAMPLE",
                    )
                )
                for index in range(1, 7)
            ),
            "atom_boundary_dsl": "A | p0001 | narration | scene_boundary",
            "planner_graph_dsl": (
                "N N1 | fn=setup | mats=M001 | Vaelor changes the route.\n"
                "E causal_link | cause=N1 | effect=N1 | stale edge in the node pass"
            ),
            "selector_path_dsl": (
                "P C001 | core | p\n"
                "P C002 | bridge | p"
            ),
            "selector_directive_dsl": (
                "D P001 | -\n"
                "D P002 | "
                "Frame the second source as corroboration, then return immediately "
                "to Vaelor's route."
            ),
            "node_edit": (
                "NODE C001\n"
                "K M001\n"
                "ENDNODE\n"
                "NODE C002\n"
                "K M002\n"
                "ENDNODE"
            ),
            "render_fact_context_dsl": (
                "HINT C001N002 | Vaelor改变路线的内容已在更早计划安排。"
            ),
            "name_table": json.dumps(
                {
                    "replacements": [
                        {
                            "aliases": ["Vaelor", "the Warden"],
                            "stable_name": "维洛尔",
                        }
                    ],
                    "protected_surfaces": [],
                },
                ensure_ascii=False,
            ),
            "render_chunk": "维洛尔继续沿着改变后的路线前进。",
        }
        responses.update(self._response_overrides)
        response = responses[route]
        store: RunStore = kwargs["store"]
        call_id = f"fake-call-{call_number}"
        request = {"messages": kwargs["messages"]}
        raw_request_path = store.write_raw_payload(call_id, "request", request)
        raw_response_path = store.write_raw_payload(call_id, "response", response)
        route_config = kwargs["config"].route(route)
        record = LlmCallRecord(
            call_id=call_id,
            stage=route_config.stage,
            operation_route=route,
            provider="offline-test",
            model=route_config.model,
            input_hash=f"sha256:{sha256_json(request)}",
            raw_request_path=raw_request_path,
            raw_response_path=raw_response_path,
            provider_status=ProviderStatus.success,
            created_at=datetime.now(tz=UTC),
            prompt_version=kwargs.get("prompt_version"),
            work_unit=kwargs.get("work_unit"),
        )
        store.append_record(LLM_CALL_RECORDS, record)
        return response, record


def _project(tmp_path: Path) -> tuple[Path, ProjectConfig, HostProfile]:
    project_dir = tmp_path / "projects" / "e2e"
    raw_dir = project_dir / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "source.txt").write_text(
        "Vaelor 强迫队伍改变原定路线，并沿着新的方向继续前进。队伍因此必须重新安排之后的行动。",
        encoding="utf-8",
    )
    (raw_dir / "guide.txt").write_text(
        "向导明确表示，the Warden 改变了原定路线，这一说法构成独立佐证，也解释了队伍为何转向。",
        encoding="utf-8",
    )
    (project_dir / "sources.json").write_text(
        json.dumps(
            {
                "project_id": "e2e",
                "output_language": "zh-CN",
                "name_resolution_mode": "name_table",
                "sources": [
                    {
                        "source_id": "script",
                        "path": "raw/source.txt",
                        "profile": "script_transcript",
                        "language": "zh-CN",
                        "source_type": "local_text",
                        "source_class": "script_transcript",
                        "trust_tier": "canonical",
                    },
                    {
                        "source_id": "guide",
                        "path": "raw/guide.txt",
                        "profile": "script_transcript",
                        "language": "zh-CN",
                        "source_type": "local_text",
                        "source_class": "script_transcript",
                        "trust_tier": "high",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project = load_project(
        project_dir,
        REPO_ROOT / "configs/source_profile_templates.example.json",
    )
    return project_dir, project, _host()


def test_run_pipeline_offline_full_chain_aggregates_reports_and_artifacts(
    tmp_path: Path,
) -> None:
    project_dir, project, host = _project(tmp_path)
    config = RuntimeConfig.load(REPO_ROOT / "configs/runtime.example.json", load_env=False)
    config = config.model_copy(
        update={
            "stage_budgets": {
                **config.stage_budgets,
                "rendering": {
                    **config.stage_budgets["rendering"],
                    "max_nodes_per_chunk": 2,
                },
            }
        }
    )
    fake_call = LedgerFakeCall()
    runs_root = tmp_path / "runs"

    result = run_pipeline(
        project,
        project_dir,
        host=host,
        config=config,
        runs_root=runs_root,
        run_id="e2e-run",
        call=fake_call,
        selection_human_directive=["本次只比较两条路线变化证据。"],
    )

    store = RunStore.for_run(runs_root, "e2e-run")
    assert all(store.exists(spec) for spec in ARTIFACT_SPECS)
    atoms = store.read_records(MATERIAL_ATOMS)
    assert atoms
    assert all("attribution" not in atom.model_dump() for atom in atoms)
    assert store.exists(SCRIPT)
    assert store.read_text(SCRIPT) == "维洛尔继续沿着改变后的路线前进。"
    [rendered_chunk] = store.read_records(CHUNK_ARTIFACTS)
    assert rendered_chunk.text == "维洛尔继续沿着改变后的路线前进。"
    assert {
        key: rendered_chunk.metadata[key]
        for key in ("chunk_index", "chunk_count", "segment_position")
    } == {
        "chunk_index": 1,
        "chunk_count": 1,
        "segment_position": "single",
    }
    expected_style_key = f"e2e-run:{rendered_chunk.contract_id}"
    assert rendered_chunk.metadata["style_example_sample_key"] == expected_style_key

    routes = [call["operation_route"] for call in fake_call.calls]
    assert Counter(routes) == Counter(
        {
            "atom_boundary_dsl": 2,
            "planner_graph_dsl": 2,
            "selector_path_dsl": 1,
            "selector_directive_dsl": 1,
            "node_edit": 1,
            "name_table": 1,
            "render_chunk": 1,
        }
    )
    assert "render_fact_context_dsl" not in routes
    ledger = store.read_records(LLM_CALL_RECORDS)
    assert len(ledger) == len(routes) == result.calls
    assert all(record.raw_request_path and record.raw_response_path for record in ledger)

    selector_call = next(
        call for call in fake_call.calls if call["operation_route"] == "selector_path_dsl"
    )
    assert selector_call["prompt_version"] == (
        "selector_path_dsl@v14+selector_path_dsl_user@v11"
    )
    selector_system_prompt = selector_call["messages"][0]["content"]
    selector_user_prompt = selector_call["messages"][1]["content"]
    selector_system_no_ws = "".join(selector_system_prompt.split())
    selector_user_no_ws = "".join(selector_user_prompt.split())
    assert "阶段一·资格判断" in selector_system_no_ws
    assert "通过资格判断只表示“可以使用”，不表示“必须入选”" in (
        selector_system_no_ws
    )
    assert "阶段二·编辑路径" in selector_system_no_ws
    assert "不设入选比例" in selector_system_no_ws
    assert "`color+summarize`不是“不确定就先收”的回收位" in (
        selector_system_no_ws
    )
    assert "另一项全局任务会读取完整有序路径" in selector_system_prompt
    assert "人工指令(human_directive):" in selector_user_prompt
    assert (
        "- 本次只比较两条路线变化证据。"
        in selector_user_prompt
    )
    assert "务必纳入并比较 Vaelor" not in selector_user_prompt
    assert host.host_thinking.human_directive == [
        "务必纳入并比较 Vaelor 的路线行动与向导对路线变化的独立佐证。"
    ]
    assert "human_directive 是 operator 对本轮路径的明确控制" in (
        selector_user_prompt
    )
    assert "逐项匹配相应 node" in selector_user_prompt
    assert "不能留给后续 directive task 代替" in selector_user_prompt
    assert "每个 accepted node 都输出一条以 `P ` 开头的物理行" in (
        selector_user_prompt
    )
    assert "即使路径很长也不能省略后续行的 `P `" in selector_user_prompt
    assert "规模作用于最终编辑路径" in selector_user_no_ws
    assert "逐个node做资格判断" in selector_user_no_ws
    assert "narrative_function 值不是 role" in selector_user_prompt
    assert "只讲材料明确支持的行动与结果。" in selector_user_prompt
    assert "离线测试 host 的思考规则。" in selector_user_prompt

    selection = store.read_model(SELECTION_PLAN)
    graph = store.read_model(CANDIDATE_GRAPH)
    assert graph.edges == []
    assert "edge_protocol" not in graph.metadata
    assert "edge_limits" not in graph.metadata
    assert not any(
        call["operation_route"] == "global_edge_dsl"
        for call in fake_call.calls
    )
    assert selection.metadata["selector_protocol"] == selector_call["prompt_version"]
    directive_call = next(
        call
        for call in fake_call.calls
        if call["operation_route"] == "selector_directive_dsl"
    )
    assert selection.metadata["directive_call_id"]
    directive_prompt = directive_call["messages"][1]["content"]
    assert "P P001 | role=core | treatment=paraphrase" in directive_prompt
    assert "P P002 | role=bridge | treatment=paraphrase" in directive_prompt
    assert "第一条负责播客开场" in directive_prompt
    accepted = [
        decision
        for decision in selection.decisions
        if decision.status == SelectionStatus.accepted
    ]
    assert [decision.candidate_id for decision in accepted] == selection.selected_sequence
    assert set(selection.selected_sequence) == {
        node.candidate_id for node in graph.nodes
    }
    assert [decision.role for decision in accepted] == [Role.core, Role.bridge]
    assert [decision.directive for decision in accepted] == [
        None,
        (
            "Frame the second source as corroboration, then return immediately "
            "to Vaelor's route."
        ),
    ]
    selector_record = next(
        record for record in ledger if record.operation_route == "selector_path_dsl"
    )
    selector_response = store.read_raw_payload(selector_record.call_id, "response")
    assert isinstance(selector_response, str)
    selector_response_lines = selector_response.splitlines()
    assert len(selector_response_lines) == len(accepted)
    assert all(
        line.startswith("P ") and len(line.split("|")) == 3
        for line in selector_response_lines
    )

    node_edit = store.read_model(NODE_EDIT_PLAN)
    assert [result.selection.directive for result in node_edit.node_edits] == [
        None,
        (
            "Frame the second source as corroboration, then return immediately "
            "to Vaelor's route."
        ),
    ]
    node_edit_prompts = [
        call["messages"][1]["content"]
        for call in fake_call.calls
        if call["operation_route"] == "node_edit"
    ]
    assert sum(
        (
            "DIR Frame the second source as corroboration, then return immediately "
            "to Vaelor's route."
        )
        in prompt
        for prompt in node_edit_prompts
    ) == 1
    assert any(
        "role=core | treatment=paraphrase" in prompt
        for prompt in node_edit_prompts
    )
    assert any(
        "role=bridge | treatment=paraphrase" in prompt
        for prompt in node_edit_prompts
    )
    assert all(
        "DIR -" not in prompt and "DIR None" not in prompt
        for prompt in node_edit_prompts
    )

    [render_contract] = store.read_records(RENDER_CONTRACTS)
    assert all(
        "attribution" not in material.model_dump()
        for material in render_contract.materials
    )
    assert [node.directive for node in render_contract.nodes] == [
        None,
        (
            "Frame the second source as corroboration, then return immediately "
            "to Vaelor's route."
        ),
    ]

    plan = store.read_model(NAME_RESOLUTION_PLAN)
    [rule] = plan.rules
    assert rule.stable_name == "维洛尔"
    assert rule.aliases == ["Vaelor", "the Warden"]
    assert plan.contract_ids == [
        contract.contract_id
        for contract in store.read_records(RENDER_CONTRACTS)
    ]
    table_calls = [
        call
        for call in fake_call.calls
        if call["operation_route"] == NAME_TABLE_ROUTE
    ]
    [table_call] = table_calls
    assert table_call["work_unit"].kind == "name_table"
    assert table_call["work_unit"].unit_id == "name-table"
    assert len(table_call["work_unit"].input_refs) == 2
    table_prompt = table_call["messages"][1]["content"]
    assert "Vaelor 强迫队伍改变原定路线" in table_prompt
    assert "the Warden 改变了原定路线" in table_prompt
    assert (
        "Frame the second source as corroboration, then return immediately "
        "to Vaelor's route."
    ) not in table_prompt
    assert "DIR -" not in table_prompt
    assert "None" not in table_prompt
    render_call = next(
        call for call in fake_call.calls if call["operation_route"] == "render_chunk"
    )
    render_prompt = render_call["messages"][1]["content"]
    assert {
        key: render_call["work_unit"].metadata[key]
        for key in ("chunk_index", "chunk_count", "segment_position")
    } == {
        "chunk_index": 1,
        "chunk_count": 1,
        "segment_position": "single",
    }
    assert (
        render_call["work_unit"].metadata["style_example_sample_key"]
        == expected_style_key
    )
    assert "chunk_index: 1" in render_prompt
    assert "chunk_count: 1" in render_prompt
    assert "segment_position: single" in render_prompt
    assert "维洛尔 强迫队伍改变原定路线" in render_prompt
    assert "the Warden" not in render_prompt
    assert "维洛尔" in render_prompt
    assert "自然、克制的中文播客口吻。" in render_prompt
    assert "离线测试 host 的表达规则。" in render_prompt
    assert "本次只比较两条路线变化证据。" not in render_prompt
    assert "务必纳入并比较 Vaelor" not in render_prompt
    single_example = host.style_examples_by_position["single"][0]
    assert single_example.source in render_prompt
    assert single_example.render in render_prompt
    assert single_example.lesson in render_prompt
    assert host.style_examples_by_position["opening"][0].source not in render_prompt
    assert "name reference" not in render_prompt.casefold()
    assert (
        render_prompt.count(
            "【叙事提示｜Frame the second source as corroboration, then return "
            "immediately to Vaelor's route.】"
        )
        == 1
    )
    assert "【叙事提示｜None】" not in render_prompt
    assert "【叙事提示｜-】" not in render_prompt
    assert "role=core" not in render_prompt
    assert "role=bridge" not in render_prompt
    assert "treatment=paraphrase" not in render_prompt

    audit = store.read_records(AUDIT_REPORT)
    [fact_context] = store.read_records(RENDER_FACT_CONTEXTS)
    assert fact_context.node_hints == []
    assert fact_context.metadata["reason"] == "no_earlier_node"
    assert not any(item.kind.startswith("fact_context_") for item in audit)
    assert "diagnostics" not in graph.metadata
    planning_finding = next(
        item for item in audit if item.kind == "unexpected_edge_in_node_pass"
    )
    assert planning_finding.stage == "planning"
    planner_call_ids = {
        record.call_id for record in ledger if record.operation_route == "planner_graph_dsl"
    }
    assert planning_finding.call_id in planner_call_ids
    assert result.upstream_has_issues is True

    assembly = store.read_model(ASSEMBLY_REPORT)
    assert assembly.validation_report_ref == AUDIT_REPORT.relpath
    assert assembly.assembled_with_issues is True
    assert assembly.assembled_chunk_ids == assembly.chunk_order

    with pytest.raises(FileExistsError):
        run_pipeline(
            project,
            project_dir,
            host=host,
            config=config,
            runs_root=runs_root,
            run_id="e2e-run",
            call=LedgerFakeCall(),
        )


def test_legacy_relation_scan_project_runs_with_zero_name_calls(
    tmp_path: Path,
) -> None:
    project_dir, _project_config, host = _project(tmp_path)
    spec_path = project_dir / "sources.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["name_resolution_mode"] = "relation_scan"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    project = load_project(
        project_dir,
        REPO_ROOT / "configs/source_profile_templates.example.json",
    )
    assert project.name_resolution_mode is NameResolutionMode.overrides_only
    fake_call = LedgerFakeCall()

    run_pipeline(
        project,
        project_dir,
        host=host,
        config=RuntimeConfig.load(
            REPO_ROOT / "configs/runtime.example.json", load_env=False
        ),
        runs_root=tmp_path / "runs",
        run_id="legacy-relation-scan",
        call=fake_call,
    )

    assert NAME_TABLE_ROUTE not in {
        call["operation_route"] for call in fake_call.calls
    }


def test_run_pipeline_failed_ingestion_stops_before_planning_and_atoms_publish(
    tmp_path: Path,
) -> None:
    project_dir, project, host = _project(tmp_path)
    config = RuntimeConfig.load(REPO_ROOT / "configs/runtime.example.json", load_env=False)
    fake_call = LedgerFakeCall(response_overrides={"atom_boundary_dsl": ""})
    runs_root = tmp_path / "runs"

    with pytest.raises(IngestionError) as captured:
        run_pipeline(
            project,
            project_dir,
            host=host,
            config=config,
            runs_root=runs_root,
            run_id="ingestion-failed-run",
            call=fake_call,
        )

    failure = captured.value
    assert not failure.result.ok
    assert failure.reports == failure.result.reports
    assert failure.reports
    assert all(report.stage == "ingestion" for report in failure.reports)
    assert all(report.usable is False for report in failure.reports)

    routes = [call["operation_route"] for call in fake_call.calls]
    assert routes == ["atom_boundary_dsl", "atom_boundary_dsl"]
    assert "planner_graph_dsl" not in routes

    store = RunStore.for_run(runs_root, "ingestion-failed-run")
    assert store.exists(SOURCE_SNAPSHOTS)
    assert store.exists(SOURCE_PIECES)
    assert store.exists(SOURCE_WINDOWS)
    assert not store.exists(MATERIAL_ATOMS)
    assert not store.exists(CANDIDATE_GRAPH)
    assert not store.exists(SELECTION_PLAN)
    assert store.read_records(AUDIT_REPORT) == failure.reports


@pytest.mark.parametrize(
    (
        "source_language_class",
        "expected_variant",
        "source_text",
        "guide_text",
        "source_language",
        "guide_language",
    ),
    [
        (
            "zh",
            "zh",
            "这是一段明确的中文材料，记录队伍改变路线以及随后采取的行动。",
            "另一段中文材料独立确认路线已经改变，并说明队伍继续前进。",
            "zh-CN",
            "zh-CN",
        ),
        (
            "en",
            "en",
            "This English source records a route change and the party's next action.",
            "Another English source independently confirms the changed route.",
            "en-US",
            "en-US",
        ),
        (
            "mixed",
            "en",
            "这段中文材料记录队伍改变路线，并继续执行后续行动。",
            "This English source independently confirms the changed route and next action.",
            "zh-CN",
            "en-US",
        ),
    ],
)
def test_offline_full_chain_audits_source_language_and_uses_one_control_variant(
    tmp_path: Path,
    source_language_class: str,
    expected_variant: str,
    source_text: str,
    guide_text: str,
    source_language: str,
    guide_language: str,
) -> None:
    project_dir, _project_config, host = _project(tmp_path)
    (project_dir / "raw" / "source.txt").write_text(source_text, encoding="utf-8")
    (project_dir / "raw" / "guide.txt").write_text(guide_text, encoding="utf-8")
    project_payload = json.loads((project_dir / "sources.json").read_text(encoding="utf-8"))
    project_payload["sources"][0]["language"] = source_language
    project_payload["sources"][1]["language"] = guide_language
    (project_dir / "sources.json").write_text(
        json.dumps(project_payload, ensure_ascii=False), encoding="utf-8"
    )
    project = load_project(
        project_dir, REPO_ROOT / "configs/source_profile_templates.example.json"
    )
    fake_call = LedgerFakeCall()
    run_id = f"locale-{source_language_class}"

    result = run_pipeline(
        project,
        project_dir,
        host=host,
        config=RuntimeConfig.load(
            REPO_ROOT / "configs/runtime.example.json", load_env=False
        ),
        runs_root=tmp_path / "runs",
        run_id=run_id,
        call=fake_call,
        name_resolution_mode=NameResolutionMode.overrides_only,
    )

    assert result.control_prompt_decision is not None
    assert result.control_prompt_decision.source_language is SourceLanguageClass(
        source_language_class
    )
    assert result.control_prompt_decision.variant.value == expected_variant
    if expected_variant == "zh":
        assert all(
            call["prompt_version"]
            and ".en@" not in call["prompt_version"]
            and ".neutral@" not in call["prompt_version"]
            for call in fake_call.calls
        )
    else:
        assert all(
            call["prompt_version"]
            and ".en@" in call["prompt_version"]
            and ".neutral@" not in call["prompt_version"]
            for call in fake_call.calls
        )
    if source_language_class == "mixed":
        atom_contents = {
            atom.content
            for atom in RunStore.for_run(tmp_path / "runs", run_id).read_records(
                MATERIAL_ATOMS
            )
        }
        rendered_material_contents = {
            material.content
            for contract in RunStore.for_run(tmp_path / "runs", run_id).read_records(
                RENDER_CONTRACTS
            )
            for material in contract.materials
        }
        assert atom_contents == {source_text, guide_text}
        assert rendered_material_contents == {source_text, guide_text}
    assert RunStore.for_run(tmp_path / "runs", run_id).read_text(SCRIPT) == (
        "维洛尔继续沿着改变后的路线前进。"
    )


def test_run_pipeline_offline_accepts_v2_host_single_chunk_fallback(
    tmp_path: Path,
) -> None:
    project_dir, project, _ = _project(tmp_path)
    host = _v2_host()
    config = RuntimeConfig.load(REPO_ROOT / "configs/runtime.example.json", load_env=False)
    fake_call = LedgerFakeCall()

    result = run_pipeline(
        project,
        project_dir,
        host=host,
        config=config,
        runs_root=tmp_path / "runs",
        run_id="v2-host-e2e",
        call=fake_call,
    )

    assert result.script_chars > 0
    [render_call] = [
        item for item in fake_call.calls if item["operation_route"] == "render_chunk"
    ]
    prompt = render_call["messages"][1]["content"]
    store = RunStore.for_run(tmp_path / "runs", "v2-host-e2e")
    [artifact] = store.read_records(CHUNK_ARTIFACTS)
    sample_key = artifact.metadata["style_example_sample_key"]
    assert sample_key == f"v2-host-e2e:{artifact.contract_id}"
    assert render_call["work_unit"].metadata["style_example_sample_key"] == sample_key
    selected = tuple(
        example
        for _position, example in host.style_examples_for(
            "single",
            sample_key=sample_key,
        )
    )
    assert all(
        value in prompt
        for example in selected
        for value in (example.source, example.render)
    )
    assert all(
        value not in prompt
        for examples in host.style_examples_by_position.values()
        for example in examples
        if example not in selected
        for value in (example.source, example.render)
    )
    assert "示例 1（来源位置：opening）" in prompt
    assert "示例 2（来源位置：ending）" in prompt
    # v2 examples carry no lesson, so the renderer projects the pair only.
    assert "[学习点]" not in prompt


def test_host_builder_output_runs_through_complete_offline_pipeline(
    tmp_path: Path,
) -> None:
    corpus_dir = tmp_path / "preference-corpus"
    corpus_dir.mkdir()
    for index, char in enumerate("甲乙丙丁", start=1):
        (corpus_dir / f"{index}.txt").write_text(
            f"{char * 4}。{char * 5}！{char * 6}？",
            encoding="utf-8",
        )
    config = RuntimeConfig.load(REPO_ROOT / "configs/runtime.example.json", load_env=False)
    fake_call = LedgerFakeCall()
    hosts_dir = tmp_path / "hosts"

    built = run_host_build(
        inputs=[corpus_dir],
        host_id="built-host",
        piece_max_tokens=7,
        seed=29,
        config=config,
        hosts_dir=hosts_dir,
        runs_root=tmp_path / "host-build-runs",
        run_id="host-build-e2e",
        call=fake_call,
    )
    project_dir, project, _ = _project(tmp_path)
    result = run_pipeline(
        project,
        project_dir,
        host=built.host,
        config=config,
        runs_root=tmp_path / "pipeline-runs",
        run_id="built-host-pipeline-e2e",
        call=fake_call,
    )

    assert built.host.schema_version == "tracecast-host/2"
    assert built.host.host_thinking.human_directive == []
    assert result.script_chars > 0
    assert Counter(item["operation_route"] for item in fake_call.calls)[
        HOST_PROFILE_ROUTE
    ] == 1
    assert Counter(item["operation_route"] for item in fake_call.calls)[
        HOST_EXAMPLES_ROUTE
    ] == 1
    [render_call] = [
        item for item in fake_call.calls if item["operation_route"] == "render_chunk"
    ]
    render_prompt = render_call["messages"][1]["content"]
    store = RunStore.for_run(tmp_path / "pipeline-runs", "built-host-pipeline-e2e")
    [artifact] = store.read_records(CHUNK_ARTIFACTS)
    sample_key = artifact.metadata["style_example_sample_key"]
    assert sample_key == f"built-host-pipeline-e2e:{artifact.contract_id}"
    assert render_call["work_unit"].metadata["style_example_sample_key"] == sample_key
    selected = built.host.style_examples_for("single", sample_key=sample_key)
    assert all(example.render in render_prompt for _position, example in selected)
    assert "示例 1（来源位置：opening）" in render_prompt
    assert "示例 2（来源位置：ending）" in render_prompt


def _wired_atom(
    material_id: str, content: str, *, source_ref: str, source_order: int
) -> MaterialAtom:
    return MaterialAtom(
        id=material_id,
        content=content,
        language="en",
        content_kind=ContentKind.narration,
        source_order=source_order,
        trust_tier=TrustTier.canonical,
        provenance=Provenance(
            source_ref=source_ref,
            source_type="script_transcript",
            source_class="script_transcript",
            source_content_hash=f"hash_{source_ref}",
            piece_refs=[f"{source_ref}:{source_order:06d}"],
        ),
    )


def test_run_render_tail_keeps_raw_fact_context_but_resolves_renderer_prompt(
    tmp_path: Path,
) -> None:
    """The offline tail exercises the required cross-stage ordering.

    Preflight reads raw contracts.  One Name Table call reads the complete
    selected material set, then only the Renderer prompt copy changes.
    Persisted contracts and fact contexts remain raw and Assembly ships the
    provider body verbatim.
    """
    graph = CandidateGraph(
        graph_id="g1",
        project_id="proj",
        nodes=[
            CandidateNode(
                candidate_id="a", source_window="w1", summary="Vaelor 出发", material_ids=["mat_a"]
            ),
            CandidateNode(
                candidate_id="b", source_window="w1", summary="向导登场", material_ids=["mat_b"]
            ),
            CandidateNode(
                candidate_id="c", source_window="w2", summary="Vael 归来", material_ids=["mat_c"]
            ),
        ],
    )
    atoms = [
        _wired_atom(
            "mat_a",
            "Vaelor forces the party to change its route.",
            source_ref="script",
            source_order=1,
        ),
        _wired_atom(
            "mat_b",
            "The guide says the Warden changes the planned route.",
            source_ref="guide",
            source_order=1,
        ),
        _wired_atom(
            "mat_c", "Vael returns to the village at last.", source_ref="script", source_order=2
        ),
    ]
    decisions = [
        SelectionDecision(
            candidate_id="a",
            status=SelectionStatus.accepted,
            role=Role.core,
            treatment="paraphrase",
            directive="Let Vaelor establish the episode's central pressure.",
        ),
        SelectionDecision(
            candidate_id="b",
            status=SelectionStatus.accepted,
            role=Role.bridge,
            treatment="paraphrase",
            directive="Use the guide as evidence about the same Vaelor.",
        ),
        SelectionDecision(
            candidate_id="c",
            status=SelectionStatus.accepted,
            role=Role.core,
            treatment="paraphrase",
            directive="让Vael的归途收束本段。",
        ),
    ]
    selection = SelectionPlan(
        plan_id="sel-1",
        project_id="proj",
        persona_id="host",
        selected_sequence=["a", "b", "c"],
        decisions=decisions,
    )
    config = RuntimeConfig.load(REPO_ROOT / "configs/runtime.example.json", load_env=False)
    config = config.model_copy(
        update={
            "stage_budgets": {
                **config.stage_budgets,
                "node_edit": {
                    **config.stage_budgets["node_edit"],
                    "max_nodes_per_batch": 1,
                },
                "rendering": {
                    **config.stage_budgets["rendering"],
                    # Groups nodes a+b (2) into chunk 1, leaving c alone in
                    # chunk 2 -- two chunks, so fact-context generation
                    # actually issues a real call instead of trivially
                    # skipping (which it does for a single-chunk run).
                    "max_nodes_per_chunk": 2,
                },
            }
        }
    )
    host = _host()
    captured: dict[str, list[dict[str, Any]]] = {}

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        route = kwargs["operation_route"]
        captured.setdefault(route, []).append(kwargs)
        call_id = f"fake-{route}-{len(captured[route])}"
        record = LlmCallRecord(
            call_id=call_id,
            stage=kwargs["config"].route(route).stage,
            operation_route=route,
            provider="offline-test",
            model="offline-model",
            input_hash=f"sha256:{call_id}",
            provider_status=ProviderStatus.success,
            created_at=datetime(2026, 7, 23, tzinfo=UTC),
            prompt_version=kwargs.get("prompt_version"),
            work_unit=kwargs.get("work_unit"),
        )
        if route == "node_edit":
            return "NODE C001\nK M001\nENDNODE", record
        if route == "render_fact_context_dsl":
            response = (
                "HINT C002N001 | "
                "更早计划中的Vaelor与当前计划的Vael有内容重叠。"
            )
            return response, record
        if route == NAME_TABLE_ROUTE:
            assert kwargs["work_unit"].kind == "name_table"
            return (
                json.dumps(
                    {
                        "replacements": [
                            {
                                "aliases": ["Vael", "Vaelor", "the Warden"],
                                "stable_name": "维洛尔",
                            }
                        ],
                        "protected_surfaces": [],
                    },
                    ensure_ascii=False,
                ),
                record,
            )
        if route == "render_chunk":
            return "占位正文，包含完整的两句话。继续。", record
        raise AssertionError(f"unexpected operation_route: {route}")

    store = RunStore.for_run(tmp_path / "runs", "wiring-check")
    node_edit = build_node_edit_outputs(
        selection,
        graph,
        atoms,
        config=config,
        store=store,
        call=fake_call,
    )
    result = run_render_tail(
        graph=graph,
        selection=selection,
        node_edit_plan=node_edit.plan,
        edited_materials=node_edit.edited_materials,
        host=host,
        output_language="zh-CN",
        name_overrides=(),
        name_resolution_mode=NameResolutionMode.name_table,
        config=config,
        store=store,
        call=fake_call,
    )

    assert len(captured["node_edit"]) == 3
    assert node_edit.edited_materials == atoms
    assert len(result.packing.contracts) == 2
    chunk_ids_by_order = {c.order_index: c.chunk_id for c in result.packing.contracts}

    [fact_context_call] = captured["render_fact_context_dsl"]
    fact_context_prompt = fact_context_call["messages"][1]["content"]
    assert "Vael的归途" in fact_context_prompt
    assert "Vaelor的归途" not in fact_context_prompt

    [name_table_call] = captured[NAME_TABLE_ROUTE]
    assert name_table_call["work_unit"].kind == "name_table"
    assert len(name_table_call["work_unit"].input_refs) == 3
    name_table_prompt = name_table_call["messages"][1]["content"]
    assert "Vaelor forces the party to change its route" in name_table_prompt
    assert "The guide says the Warden changes the planned route" in name_table_prompt
    assert "Vael returns to the village" in name_table_prompt
    assert "Let Vaelor establish" not in name_table_prompt
    assert "Use the guide as evidence" not in name_table_prompt
    assert "让Vael的归途收束本段" not in name_table_prompt
    assert "更早计划中的Vaelor与当前计划的Vael有内容重叠" not in name_table_prompt

    [rule] = result.name_resolution.rules
    assert rule.stable_name == "维洛尔"
    assert rule.aliases == ["Vael", "Vaelor", "the Warden"]

    render_prompts = [call["messages"][1]["content"] for call in captured["render_chunk"]]
    assert any("维洛尔 returns to the village" in prompt for prompt in render_prompts)
    assert all("Vael returns to the village" not in prompt for prompt in render_prompts)
    assert any("让Vael的归途收束本段" in prompt for prompt in render_prompts)
    assert any(
        "更早计划中的Vaelor与当前计划的Vael有内容重叠" in prompt
        for prompt in render_prompts
    )
    assert all("the Warden" not in prompt for prompt in render_prompts)
    render_calls_by_index = {
        call["work_unit"].metadata["chunk_index"]: call
        for call in captured["render_chunk"]
    }
    assert set(render_calls_by_index) == {1, 2}
    assert render_calls_by_index[1]["work_unit"].metadata["segment_position"] == "opening"
    assert render_calls_by_index[2]["work_unit"].metadata["segment_position"] == "ending"
    assert "segment_position: opening" in (
        render_calls_by_index[1]["messages"][1]["content"]
    )
    assert "segment_position: ending" in (
        render_calls_by_index[2]["messages"][1]["content"]
    )
    assert [
        (
            chunk.metadata["chunk_index"],
            chunk.metadata["chunk_count"],
            chunk.metadata["segment_position"],
        )
        for chunk in result.chunks
    ] == [(1, 2, "opening"), (2, 2, "ending")]

    fact_contexts_by_chunk = {fc.chunk_id: fc for fc in store.read_records(RENDER_FACT_CONTEXTS)}
    chunk_2_id = chunk_ids_by_order[1]
    [raw_hint] = fact_contexts_by_chunk[chunk_2_id].node_hints
    assert raw_hint.source_node_id == "c"
    assert raw_hint.established == "更早计划中的Vaelor与当前计划的Vael有内容重叠。"
    assert all(
        context.metadata["semantics"] == "sparse_node_established_v3"
        for context in fact_contexts_by_chunk.values()
    )
    assert result.assembly_report.assembled_chunk_ids == [
        chunk_ids_by_order[0],
        chunk_ids_by_order[1],
    ]
    assert result.script.count("占位正文，包含完整的两句话。继续。") == 2
