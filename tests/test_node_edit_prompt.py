"""Focused tests for node-edit prompt projection and user-template wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    SourceSpan,
    Treatment,
    TrustTier,
)
from tracecast.node_edit import build_node_edit_outputs
from tracecast.node_edit.projection import (
    BatchNode,
    project_batch,
    render_node_edit_user_prompt,
)
from tracecast.node_edit.vocab import default_max_nodes_per_batch
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.storage import RunStore, RuntimeConfig


def _atom(
    material_id: str,
    *,
    source_ref: str,
    source_order: int,
    content: str,
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
            source_type="script",
            source_class="canonical",
            source_content_hash=f"hash_{source_ref}",
            piece_refs=[f"{source_ref}_piece_{source_order}"],
            extractor_version="test",
        ),
        tags=["scene_boundary"],
    )


def _decision(candidate_id: str) -> SelectionDecision:
    return SelectionDecision(
        candidate_id=candidate_id,
        status=SelectionStatus.accepted,
        role=Role.core,
        treatment=Treatment.paraphrase,
        directive="keep the state change",
    )


def _node(candidate_id: str, material_ids: list[str], *, start: int, end: int) -> CandidateNode:
    return CandidateNode(
        candidate_id=candidate_id,
        source_window=f"win_{candidate_id}",
        summary="final compressed summary",
        material_ids=material_ids,
        narrative_function="state_change",
        source_span=SourceSpan(start=start, end=end),
        metadata={"source_ref": "script"},
    )


def test_node_edit_user_prompt_omits_summary_and_keeps_directive() -> None:
    atoms = [
        _atom("mat_a", source_ref="script", source_order=10, content="Line one\nLine two"),
        _atom("mat_b", source_ref="guide", source_order=7, content="Guide line"),
    ]
    batch = [
        BatchNode(
            "cand_a",
            _decision("cand_a"),
            _node("cand_a", ["mat_a"], start=10, end=10),
            [atoms[0]],
        ),
        BatchNode(
            "cand_b",
            _decision("cand_b"),
            _node("cand_b", ["mat_b"], start=7, end=7),
            [atoms[1]],
        ),
    ]

    projection = project_batch(batch)
    rendered = render_node_edit_user_prompt(
        template=(
            "candidates {{candidate_alias_range}}\n"
            "materials {{material_alias_range}}\n"
            "sources\n{{source_aliases}}\n"
            "fields\n{{field_notes}}\n"
            "blocks\n{{node_blocks}}"
        ),
        projection=projection,
    )

    assert "- S1: script" in rendered
    assert "- S2: guide" in rendered
    assert (
        "NODE C001 | role=core | treatment=paraphrase | fn=state_change | sp=S1:10-10"
        in rendered
    )
    assert "NODE C002 | role=core | treatment=paraphrase | fn=state_change | sp=S2:7-7" in rendered
    assert "M M001 | so=10 | k=narration | tg=scene_boundary" in rendered
    assert (
        "`DIR`: node-level controls 中优先，但服从事实边界，"
        "也不能充当其他材料已覆盖的依据"
    ) in rendered
    assert (
        "`role`: 节点级大致叙述功能；影响材料贡献的判断阈值，"
        "不决定单条材料 K/C/O"
    ) in rendered
    assert "core=节点整体支撑状态、因果、身份、目标或 reveal" in rendered
    assert "paraphrase=节点整体适合正常叙述" in rendered
    assert "state_change=被追踪的状态变了" in rendered
    assert "narration=来源中的旁白" in rendered
    assert "TXT Line one Line two" in rendered
    assert "DIR keep the state change" in rendered
    assert "final compressed summary" not in rendered
    assert "\nSUM " not in rendered
    assert "src=" not in rendered
    assert "{{" not in rendered


def test_build_node_edit_plan_uses_system_and_user_prompt_versions(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompt" / "node_edit"
    prompt_dir.mkdir(parents=True)
    system_prompt = prompt_dir / "node_edit.system.zh.md"
    user_prompt = prompt_dir / "node_edit.user.zh.md"
    repair_user_prompt = prompt_dir / "node_edit.repair.user.zh.md"
    system_prompt.write_text(
        "<!-- version: node_edit@test -->\nnode edit system",
        encoding="utf-8",
    )
    user_prompt.write_text(
        "<!-- version: node_edit_user@test -->\n"
        "candidates {{candidate_alias_range}}\n"
        "materials {{material_alias_range}}\n"
        "sources\n{{source_aliases}}\n"
        "fields\n{{field_notes}}\n"
        "blocks\n{{node_blocks}}",
        encoding="utf-8",
    )
    repair_user_prompt.write_text(
        "<!-- version: node_edit_repair_user@test -->\n"
        "{{initial_failure}}\n{{node_edit_prompt}}",
        encoding="utf-8",
    )
    config = RuntimeConfig.model_validate(
        {
            "routes": {
                    "node_edit": {
                    "stage": "node_editor",
                    "provider": "gateway",
                    "model": "node-edit-model",
                    "system_prompt": str(system_prompt),
                        "user_prompt": str(user_prompt),
                    },
                    "node_edit_block_repair": {
                        "stage": "node_editor",
                        "provider": "gateway",
                        "model": "node-edit-model",
                        "system_prompt": str(system_prompt),
                        "user_prompt": str(repair_user_prompt),
                    }
            },
        }
    )
    atom = _atom("mat_a", source_ref="script", source_order=10, content="Line one\nLine two")
    graph = CandidateGraph(
        graph_id="graph_test",
        project_id="proj",
        nodes=[_node("cand_a", ["mat_a"], start=10, end=10)],
    )
    selection = SelectionPlan(
        plan_id="sel_test",
        project_id="proj",
        persona_id="host",
        selected_sequence=["cand_a"],
        decisions=[_decision("cand_a")],
    )
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "NODE C001\nK M001\nENDNODE",
            LlmCallRecord(
                call_id="call_node_edit",
                stage="node_editor",
                operation_route=kwargs["operation_route"],
                provider="gateway",
                model="node-edit-model",
                input_hash="input_hash",
                provider_status=ProviderStatus.success,
                created_at=datetime(2026, 7, 4, tzinfo=UTC),
                prompt_version=kwargs["prompt_version"],
            ),
        )

    output = build_node_edit_outputs(
        selection,
        graph,
        [atom],
        config=config,
        store=RunStore(tmp_path / "run"),
        call=fake_call,
    )

    assert output.plan.metadata["node_edit_protocol"] == (
        "node_edit@test+node_edit_user@test+"
        "node_edit_field_notes@v2+ingestion_content_kinds@v2+planning_protocol_vocab@v1"
    )
    assert output.plan.metadata["max_nodes_per_batch"] == 5
    assert calls[0]["prompt_version"] == output.plan.metadata["node_edit_protocol"]
    assert calls[0]["messages"][0]["content"] == "node edit system"
    user_message = calls[0]["messages"][1]["content"]
    assert "- S1: script" in user_message
    assert (
        "`DIR`: node-level controls 中优先，但服从事实边界，"
        "也不能充当其他材料已覆盖的依据"
    ) in user_message
    assert "M M001 | so=10 | k=narration | tg=scene_boundary" in user_message
    assert "DIR keep the state change" in user_message
    assert "final compressed summary" not in user_message
    assert "\nSUM " not in user_message
    assert "src=" not in user_message
    assert output.edited_materials == [atom]
    assert output.reports == []
    assert output.stage_reports == []


def test_production_node_edit_v7_exposes_stable_dsl_contract() -> None:
    config = RuntimeConfig.load(
        Path(__file__).resolve().parents[1] / "configs" / "runtime.example.json",
        load_env=False,
    )
    system_prompt, system_version = load_system_prompt("node_edit", config)
    user_prompt, user_version = load_user_prompt("node_edit", config)
    repair_user_prompt, repair_user_version = load_user_prompt(
        "node_edit_block_repair", config
    )

    assert system_version == "node_edit@v7"
    assert user_version == "node_edit_user@v6"
    assert repair_user_version == "node_edit_block_repair_user@v2"
    assert "`K`" in system_prompt
    assert "`C`" in system_prompt
    assert "`O`" in system_prompt
    assert "本阶段不产生中间翻译" in system_prompt
    assert "DIR <Selection" in user_prompt
    assert "NODE <C>" in user_prompt
    assert "K <M或连续区间>" in user_prompt
    assert "C <连续M区间>" in user_prompt
    assert "O <M或连续区间>" in user_prompt
    assert "SUM <" not in user_prompt
    assert "本次投影中的 alias 是唯一有效 alias" in repair_user_prompt
    assert "{{initial_failure}}" in repair_user_prompt
    assert "{{node_edit_prompt}}" in repair_user_prompt
    assert user_prompt.index("{{node_blocks}}") < user_prompt.index(
        "以上为全部材料。"
    )
    for placeholder in (
        "{{candidate_alias_range}}",
        "{{material_alias_range}}",
        "{{source_aliases}}",
        "{{field_notes}}",
        "{{node_blocks}}",
    ):
        assert placeholder in user_prompt


def test_node_edit_fallback_and_example_default_to_five_nodes_per_batch() -> None:
    assert default_max_nodes_per_batch() == 5

    repo_root = Path(__file__).resolve().parents[1]
    example = json.loads(
        (repo_root / "configs" / "runtime.example.json").read_text(encoding="utf-8")
    )
    assert example["stage_budgets"]["node_edit"]["max_nodes_per_batch"] == 5
