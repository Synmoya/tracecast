"""Focused tests for selector prompt projection and user-template rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracecast.console.config_editor import editor_schema
from tracecast.domain import (
    CandidateEdge,
    CandidateGraph,
    CandidateNode,
    LlmCallRecord,
    ProviderStatus,
    SourceSpan,
    Treatment,
)
from tracecast.planning.persona import PersonaConfig, ViewpointProfile, render_persona_block
from tracecast.planning.projection import project_selector_graph, render_selector_user_prompt
from tracecast.planning.selection_plan import build_selection_plan
from tracecast.storage import RunStore, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


def _node(
    candidate_id: str,
    *,
    source_ref: str,
    start: int,
    end: int,
    summary: str,
    material_estimated_tokens: int | None = None,
) -> CandidateNode:
    return CandidateNode(
        candidate_id=candidate_id,
        source_window=f"win_{source_ref}",
        summary=summary,
        material_ids=[f"mat_{candidate_id}"],
        material_estimated_tokens=material_estimated_tokens,
        narrative_function="reveal",
        source_span=SourceSpan(start=start, end=end),
        metadata={"source_ref": source_ref},
    )


def test_selector_projection_exposes_material_tokens_and_legacy_unknown() -> None:
    nodes = [
        _node(
            "cand_a",
            source_ref="script",
            start=1,
            end=2,
            summary="known",
            material_estimated_tokens=777,
        ),
        _node(
            "cand_b",
            source_ref="script",
            start=3,
            end=4,
            summary="legacy",
        ),
    ]

    projection = project_selector_graph(nodes, [])

    assert "C C001 | fn=reveal | m=1 | mt=777 | sp=S1:1-2" in projection.text
    assert "C C002 | fn=reveal | m=1 | mt=? | sp=S1:3-4" in projection.text


def test_selector_projection_interleaves_edges_after_relevant_node() -> None:
    nodes = [
        _node("cand_a", source_ref="script", start=10, end=12, summary="setup"),
        _node("cand_b", source_ref="script", start=50, end=52, summary="payoff"),
        _node("cand_c", source_ref="commentary", start=3, end=4, summary="contrast"),
    ]
    edges = [
        CandidateEdge(
            edge_type="setup_payoff",
            endpoints={"setup": "cand_a", "payoff": "cand_b"},
            reason="later node resolves the setup",
        ),
        CandidateEdge(
            edge_type="contrasts",
            endpoints={"a": "cand_b", "b": "cand_c"},
            reason="commentary frames the event differently",
        ),
    ]

    projection = project_selector_graph(nodes, edges)

    assert projection.text.index("C C001") < projection.text.index("E setup_payoff")
    assert projection.text.index("E setup_payoff") < projection.text.index("C C002")
    assert projection.text.index("C C002") < projection.text.index("E contrasts")
    assert projection.text.index("E contrasts") < projection.text.index("C C003")


def test_selector_user_prompt_renders_source_aliases_and_persona_layers() -> None:
    nodes = [
        _node("cand_a", source_ref="script", start=1, end=2, summary="one"),
        _node("cand_b", source_ref="guide", start=5, end=8, summary="two"),
    ]
    projection = project_selector_graph(nodes, [])
    persona = PersonaConfig(
        persona_id="host",
        viewpoint="偏向人物状态变化",
        expression="克制但风趣",
        viewpoint_profile=ViewpointProfile(
            prefer=["选择 Terra 相关节点"],
            avoid=["攻略式路线流程"],
            human_directive=["务必纳入并比较 Terra、Celes、Shadow、Locke"],
        ),
    )

    rendered = render_selector_user_prompt(
        template=(
            "{{source_token_estimate_line}}\n"
            "{{target_output_estimate_line}}\n"
            "sources\n{{source_aliases}}\n"
            "persona\n{{persona_block}}\n"
            "{{narrative_function_descriptions}}\n"
            "graph\n{{graph_blocks}}"
        ),
        projection=projection,
        persona_block=render_persona_block(persona),
        source_token_estimate=1234,
        target_output_kchars=18,
    )

    assert "- 原始素材约 1234 token" in rendered
    assert "- 目标成文约 18 k字" in rendered
    assert "- S1: script" in rendered
    assert "- S2: guide" in rendered
    assert "观点层(viewpoint, 路径/解释偏向): 偏向人物状态变化" in rendered
    assert "表达偏好(expression, selection轻参考): 克制但风趣" in rendered
    assert "人工指令(human_directive):" in rendered
    assert "- 务必纳入并比较 Terra、Celes、Shadow、Locke" in rendered
    assert "## narrative_function 说明" in rendered
    assert "{{" not in rendered
    assert "C C001 | fn=reveal | m=1 | mt=? | sp=S1:1-2" in rendered


def test_repository_selector_prompts_define_an_editorial_path_with_soft_scale() -> None:
    prompt_dir = REPO_ROOT / "prompt" / "selection"
    system_prompt = (
        prompt_dir / "selector_path_dsl.system.zh.md"
    ).read_text(encoding="utf-8")
    user_prompt = (
        prompt_dir / "selector_path_dsl.user.zh.md"
    ).read_text(encoding="utf-8")
    user_prompt_no_ws = "".join(user_prompt.split())

    assert "<!-- version: selector_path_dsl@v14 -->" in system_prompt
    assert "<!-- version: selector_path_dsl_user@v11 -->" in user_prompt
    assert "总编和叙事导演" in system_prompt
    system_prompt_no_ws = "".join(system_prompt.split())
    assert "###阶段一·资格判断" in system_prompt_no_ws
    assert "通过资格判断只表示“可以使用”，不表示“必须入选”" in (
        system_prompt_no_ws
    )
    assert "###阶段二·编辑路径" in system_prompt_no_ws
    assert "候选具备信息价值，不等于它对本期节目有足够的编辑价值" in (
        system_prompt_no_ws
    )
    assert "对本期主问题的边际贡献很低" in system_prompt
    assert "是弱相关旁支" in system_prompt
    assert "与更有力的候选重复" in system_prompt
    assert "破坏整期节目的节奏或重心" in system_prompt
    assert "不设入选比例" in system_prompt
    assert "目标规模是软参照" in system_prompt
    assert "不是配额或逐节点字数表" in system_prompt
    assert "规模作用于最终编辑路径" in user_prompt
    assert "不是固定入选比例、硬性字数gate或逐节点额度" in user_prompt_no_ws
    assert "逐个node做资格判断" in user_prompt_no_ws
    assert "按主问题、边际贡献、连续性和软规模选择真正进入path的node" in (
        user_prompt_no_ws
    )


def test_repository_selector_prompt_treats_human_directive_as_operator_control() -> None:
    prompt_dir = REPO_ROOT / "prompt" / "selection"
    system_prompt = (
        prompt_dir / "selector_path_dsl.system.zh.md"
    ).read_text(encoding="utf-8")
    user_prompt = (
        prompt_dir / "selector_path_dsl.user.zh.md"
    ).read_text(encoding="utf-8")
    user_prompt_no_ws = "".join(user_prompt.split())

    assert "persona 是你的编辑意识和主持人立场" in system_prompt
    assert "它必须改变 path 本身、轻重分配和问题意识" in system_prompt
    assert "另一项全局任务会读取完整有序路径" in system_prompt
    assert "viewpoint/prefer/avoid是总编的编辑倾向" in user_prompt_no_ws
    assert "让它们实际改变path、轻重和问题意识" in user_prompt_no_ws
    assert "human_directive是operator对本轮路径的明确控制" in user_prompt_no_ws
    assert "必须落实到node的入选/排除、行序或轻重" in user_prompt_no_ws
    assert "不能留给后续directivetask代替" in user_prompt_no_ws
    assert "若明确列出多个对象，逐项匹配相应node" in user_prompt_no_ws
    assert "不能用一个相邻主题或概括node代替" in user_prompt_no_ws
    assert "明确要求纳入的内容在候选图有支持时必须进入" in user_prompt_no_ws
    assert "prefer会抬高相关候选的编辑价值" in user_prompt_no_ws
    assert "不要求把其余所有合格node一并收入" in user_prompt_no_ws


def test_repository_selector_prompt_requires_p_prefix_on_every_accepted_line() -> None:
    user_prompt = (
        REPO_ROOT / "prompt" / "selection" / "selector_path_dsl.user.zh.md"
    ).read_text(encoding="utf-8")
    user_prompt_no_ws = "".join(user_prompt.split())

    assert "每个acceptednode都输出一条以`P`开头的物理行" in user_prompt_no_ws
    assert "即使路径很长也不能省略后续行的`P`" in user_prompt_no_ws


def test_repository_selector_prompt_separates_role_from_narrative_function() -> None:
    user_prompt = (
        REPO_ROOT / "prompt" / "selection" / "selector_path_dsl.user.zh.md"
    ).read_text(encoding="utf-8")

    assert "**role**（叙述功能，只能逐字使用以下四项）" in user_prompt
    for role in ("core", "bridge", "context", "color"):
        assert f"- `{role}`：" in user_prompt
    assert "`fn=state_change`、`commentary` 等" in user_prompt
    assert "narrative_function 值不是 role，不能写进 role 字段" in user_prompt
    assert "treatment_code 只来自 `q/p/s`" in user_prompt


def test_repository_selector_prompts_limit_color_summarize_to_auditory_value() -> None:
    prompt_dir = REPO_ROOT / "prompt" / "selection"
    system_prompt = (
        prompt_dir / "selector_path_dsl.system.zh.md"
    ).read_text(encoding="utf-8")
    user_prompt = (
        prompt_dir / "selector_path_dsl.user.zh.md"
    ).read_text(encoding="utf-8")
    system_prompt_no_ws = "".join(system_prompt.split())
    user_prompt_no_ws = "".join(user_prompt.split())

    assert "`color+summarize`不是“不确定就先收”的回收位" in (
        system_prompt_no_ws
    )
    assert "确实带来人物质感、听觉变化、幽默、呼吸或评论回报" in (
        system_prompt_no_ws
    )
    assert "不能用来挽救一个本来没有足够编辑贡献的node" in (
        system_prompt_no_ws
    )
    assert "只有具备明确听觉回报的node才使用" in user_prompt_no_ws
    assert "不能把主线暂时用不上的边缘node默认放进这里" in (
        user_prompt_no_ws
    )
    assert "不是让低贡献node勉强入选的理由" in user_prompt_no_ws


def test_repository_selector_prompts_split_path_from_global_directives() -> None:
    prompt_dir = REPO_ROOT / "prompt" / "selection"
    path_system = (
        prompt_dir / "selector_path_dsl.system.zh.md"
    ).read_text(encoding="utf-8")
    path_user = (
        prompt_dir / "selector_path_dsl.user.zh.md"
    ).read_text(encoding="utf-8")
    directive_system = (
        prompt_dir / "selector_directive_dsl.system.zh.md"
    ).read_text(encoding="utf-8")
    directive_user = (
        prompt_dir / "selector_directive_dsl.user.zh.md"
    ).read_text(encoding="utf-8")

    assert "P <alias> | <role> | <treatment_code>" in path_user
    assert "不要输出 directive、理由、SUM 或正文" in path_user
    assert "另一项全局任务会读取完整有序路径" in path_system
    assert "<!-- version: selector_directive_dsl@v1 -->" in directive_system
    assert "<!-- version: selector_directive_dsl_user@v1 -->" in directive_user
    assert "一次看完整条 path" in directive_system
    assert "第一项承担整期节目的播客开场" in directive_system
    assert "最后一项承担收束" in directive_system
    assert "D <path_alias> | <directive_or_->" in directive_user


def test_runtime_example_and_config_editor_expose_only_primary_selection_routes() -> None:
    config = RuntimeConfig.load(
        REPO_ROOT / "configs" / "runtime.example.json",
        load_env=False,
    )

    assert "global_edge_dsl" not in config.routes
    assert {
        name for name, route in config.routes.items() if route.stage == "selector"
    } == {"selector_path_dsl", "selector_directive_dsl"}
    schema = editor_schema(config)
    routes = {
        item["name"]: item
        for item in schema["routes"]
    }
    assert "global_edge_dsl" not in routes
    assert not any(
        item["bucket"] == "planning" and item["key"].startswith("global_edge")
        for item in schema["budget_fields"]
    )
    assert routes["selector_path_dsl"]["surfaces"] == [
        "full_root",
        "derive_selection",
    ]
    assert config.route("selector_directive_dsl").model == "your-selection-model"
    assert routes["selector_directive_dsl"]["surfaces"] == [
        "full_root",
        "derive_selection",
    ]
    assert {
        name for name, route in routes.items()
        if "derive_selection" in route["surfaces"]
    } == {"selector_path_dsl", "selector_directive_dsl"}


def test_repository_has_no_selection_repair_prompts() -> None:
    prompt_dir = REPO_ROOT / "prompt" / "selection"
    assert not list(prompt_dir.glob("selector_*_repair.*.zh.md"))


def test_build_selection_plan_uses_selector_user_template(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompt" / "selection"
    prompt_dir.mkdir(parents=True)
    system_prompt = prompt_dir / "selector_path_dsl.system.zh.md"
    user_prompt = prompt_dir / "selector_path_dsl.user.zh.md"
    system_prompt.write_text(
        "<!-- version: selector_path_dsl@test -->\nselector system",
        encoding="utf-8",
    )
    user_prompt.write_text(
        "<!-- version: selector_path_dsl_user@test -->\n"
        "{{source_token_estimate_line}}\n"
        "{{target_output_estimate_line}}\n"
        "persona\n{{persona_block}}\n"
        "sources\n{{source_aliases}}\n"
        "{{narrative_function_descriptions}}\n"
        "graph\n{{graph_blocks}}",
        encoding="utf-8",
    )
    config = RuntimeConfig.model_validate(
        {
            "routes": {
                "selector_path_dsl": {
                    "stage": "selector",
                    "provider": "gateway",
                    "model": "selection-model",
                    "system_prompt": str(system_prompt),
                    "user_prompt": str(user_prompt),
                }
            },
            "stage_budgets": {"selection": {"target_output_kchars": 16}},
        }
    )
    graph = CandidateGraph(
        graph_id="graph_test",
        project_id="proj",
        nodes=[
            _node("cand_a", source_ref="script", start=1, end=3, summary="one"),
            _node("cand_b", source_ref="script", start=10, end=12, summary="two"),
        ],
        edges=[
            CandidateEdge(
                edge_type="setup_payoff",
                endpoints={"setup": "cand_a", "payoff": "cand_b"},
                reason="the second node resolves the first",
            )
        ],
        metadata={"input": {"material_estimated_tokens": 9000}},
    )
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "P C001 | core | p",
            LlmCallRecord(
                call_id="call_selector",
                stage="selector",
                operation_route=kwargs["operation_route"],
                provider="gateway",
                model="selection-model",
                input_hash="input_hash",
                provider_status=ProviderStatus.success,
                created_at=datetime(2026, 7, 4, tzinfo=UTC),
                prompt_version=kwargs["prompt_version"],
            ),
        )

    output = build_selection_plan(
        graph,
        persona={
            "persona_id": "host",
            "viewpoint": "偏角色状态",
            "viewpoint_profile": {
                "prefer": ["人物选择"],
                "avoid": ["攻略流程"],
                "human_directive": ["把失去能动性作为核心问题"],
            },
        },
        config=config,
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )
    plan = output.plan

    assert plan.selected_sequence == ["cand_a"]
    assert [report.kind for report in output.reports] == [
        "selection_directive_unavailable"
    ]
    accepted, not_selected = plan.decisions
    assert accepted.treatment == Treatment.paraphrase
    assert accepted.directive is None
    assert "reason" not in accepted.model_dump()
    assert not_selected.status.value == "not_selected"
    assert plan.metadata["selector_protocol"] == (
        "selector_path_dsl@test+selector_path_dsl_user@test"
    )
    assert calls[0]["prompt_version"] == "selector_path_dsl@test+selector_path_dsl_user@test"
    assert calls[0]["messages"][0]["content"] == "selector system"
    user_message = calls[0]["messages"][1]["content"]
    assert "观点层(viewpoint, 路径/解释偏向): 偏角色状态" in user_message
    assert "本轮更想选择(prefer):\n- 人物选择" in user_message
    assert "本轮应降低权重(avoid):\n- 攻略流程" in user_message
    assert "人工指令(human_directive):\n- 把失去能动性作为核心问题" in user_message
    assert "- 原始素材约 9000 token" in user_message
    assert "- 目标成文约 16 k字" in user_message
    assert "## narrative_function 说明" in user_message
    assert "- S1: script" in user_message
    assert "C C001 | fn=reveal | m=1 | mt=? | sp=S1:1-3" in user_message
    assert (
        "E setup_payoff | setup=C001 | payoff=C002 | "
        "the second node resolves the first"
    ) in user_message
    assert "C C002 | fn=reveal | m=1 | mt=? | sp=S1:10-12" in user_message
    assert "{{" not in user_message
