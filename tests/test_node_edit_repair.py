"""Focused tests for Node Edit's one-wave per-node repair behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, Lock
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
            piece_refs=[f"{source_ref}_{source_order}"],
        ),
    )


def _decision(candidate_id: str) -> SelectionDecision:
    return SelectionDecision(
        candidate_id=candidate_id,
        status=SelectionStatus.accepted,
        role=Role.core,
        treatment=Treatment.paraphrase,
        directive="用这一节点建立当前状态。",
    )


def _node(candidate_id: str, material_ids: list[str], *, start: int) -> CandidateNode:
    return CandidateNode(
        candidate_id=candidate_id,
        source_window=f"win_{candidate_id}",
        summary="compressed summary",
        material_ids=material_ids,
        narrative_function="state_change",
        source_span=SourceSpan(start=start, end=start),
    )


def _config(tmp_path: Path, *, max_nodes_per_batch: int = 3) -> RuntimeConfig:
    prompt_dir = tmp_path / "prompt" / "node_edit"
    prompt_dir.mkdir(parents=True)
    system_prompt = prompt_dir / "node_edit.system.zh.md"
    user_prompt = prompt_dir / "node_edit.user.zh.md"
    repair_prompt = prompt_dir / "node_edit.repair.user.zh.md"
    system_prompt.write_text(
        "<!-- version: node_edit@test -->\nnode edit system",
        encoding="utf-8",
    )
    user_prompt.write_text(
        "<!-- version: node_edit_user@test -->\n"
        "{{candidate_alias_range}}\n"
        "{{material_alias_range}}\n"
        "{{source_aliases}}\n"
        "{{field_notes}}\n"
        "{{node_blocks}}",
        encoding="utf-8",
    )
    repair_prompt.write_text(
        "<!-- version: node_edit_repair_user@test -->\n"
        "{{initial_failure}}\n{{node_edit_prompt}}",
        encoding="utf-8",
    )
    return RuntimeConfig.model_validate(
        {
            "routes": {
                "node_edit": {
                    "stage": "node_editor",
                    "provider": "gateway",
                    "model": "node-edit-model",
                    "system_prompt": str(system_prompt),
                    "user_prompt": str(user_prompt),
                    "max_concurrency": 4,
                },
                "node_edit_block_repair": {
                    "stage": "node_editor",
                    "provider": "gateway",
                    "model": "node-edit-model",
                    "system_prompt": str(system_prompt),
                    "user_prompt": str(repair_prompt),
                    "max_concurrency": 4,
                }
            },
            "stage_budgets": {
                "node_edit": {
                    "batch_max_input_tokens": 32768,
                    "max_nodes_per_batch": max_nodes_per_batch,
                }
            },
        }
    )


def _record(call_id: str, kwargs: dict[str, Any]) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=call_id,
        stage="node_editor",
        operation_route=kwargs["operation_route"],
        provider="gateway",
        model="node-edit-model",
        input_hash="input_hash",
        provider_status=ProviderStatus.success,
        created_at=datetime(2026, 7, 5, tzinfo=UTC),
        prompt_version=kwargs["prompt_version"],
    )


def test_failed_nodes_receive_one_concurrent_repair_wave_then_local_fallback(
    tmp_path: Path,
) -> None:
    atoms = [
        _atom(
            f"mat_{index}",
            source_ref="script",
            source_order=index,
            content=f"Source line {index}.",
        )
        for index in range(3)
    ]
    graph = CandidateGraph(
        graph_id="graph_repair",
        project_id="proj",
        nodes=[
            _node(f"cand_{index}", [f"mat_{index}"], start=index)
            for index in range(3)
        ],
    )
    selection = SelectionPlan(
        plan_id="sel_repair",
        project_id="proj",
        persona_id="host",
        selected_sequence=["cand_0", "cand_1", "cand_2"],
        decisions=[_decision(f"cand_{index}") for index in range(3)],
    )
    barrier = Barrier(2, timeout=5)
    lock = Lock()
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        with lock:
            calls.append(kwargs)
            call_id = f"call_{len(calls)}"
        work_unit = kwargs["work_unit"]
        if work_unit.kind == "node_edit_batch":
            text = (
                "NODE C001\nK M001\nENDNODE\n"
                "NODE C002\nK M002\nO M002 | duplicate\nENDNODE"
            )
        else:
            barrier.wait()
            if work_unit.unit_id == "repair:cand_1":
                text = "NODE C001\nK M001\nENDNODE"
            else:
                text = (
                    "NODE C001\n"
                    "K M001\n"
                    "O M001 | duplicate\n"
                    "ENDNODE"
                )
        return text, _record(call_id, kwargs)

    output = build_node_edit_outputs(
        selection,
        graph,
        atoms,
        config=_config(tmp_path),
        store=RunStore(tmp_path / "run_repair"),
        call=fake_call,
    )

    assert len(calls) == 3
    repair_calls = [
        call for call in calls if call["work_unit"].kind == "node_edit_block_repair"
    ]
    assert {call["work_unit"].unit_id for call in repair_calls} == {
        "repair:cand_1",
        "repair:cand_2",
    }
    repairs_by_unit = {call["work_unit"].unit_id: call for call in repair_calls}
    duplicate_prompt = repairs_by_unit["repair:cand_1"]["messages"][1]["content"]
    assert "失败类别：duplicate_material_assignment" in duplicate_prompt
    assert "NODE C001" in duplicate_prompt
    assert "M M001" in duplicate_prompt
    assert "M002" not in duplicate_prompt
    assert "失败类别：missing_node_block" in repairs_by_unit["repair:cand_2"][
        "messages"
    ][1]["content"]
    assert {
        call["prompt_version"] for call in repair_calls
    } == {
        "node_edit@test+node_edit_repair_user@test+node_edit_user@test+"
        "node_edit_field_notes@v2+ingestion_content_kinds@v2+"
        "planning_protocol_vocab@v1"
    }
    assert [result.candidate_id for result in output.plan.node_edits] == [
        "cand_0",
        "cand_1",
        "cand_2",
    ]
    by_id = {result.candidate_id: result for result in output.plan.node_edits}
    assert by_id["cand_0"].metadata["producer_unit_id"] == "batch:cand_0"
    assert by_id["cand_1"].metadata["producer_unit_id"] == "repair:cand_1"
    assert by_id["cand_2"].metadata["producer_unit_id"] == "repair:cand_2"
    assert "fallback_reason" in by_id["cand_2"].metadata
    assert [item.kind for item in output.reports] == [
        "node_block_repaired",
        "node_fallback_all_keep",
    ]
    assert output.reports[0].metadata["initial_failure_code"] == (
        "duplicate_material_assignment"
    )
    assert output.reports[1].metadata["initial_failure_code"] == "missing_node_block"
    assert output.reports[1].metadata["repair_failure_code"] == (
        "duplicate_material_assignment"
    )
    assert [atom.id for atom in output.edited_materials] == [
        "mat_0",
        "mat_1",
        "mat_2",
    ]
    assert [edit.source_material_ids for edit in by_id["cand_2"].edits] == [
        ["mat_2"]
    ]
    assert [edit.operation.value for edit in by_id["cand_2"].edits] == ["keep"]
    assert by_id["cand_2"].omitted == []


def test_primary_batches_assemble_in_selected_order_when_the_later_batch_finishes_first(
    tmp_path: Path,
) -> None:
    atoms = [
        _atom(
            f"mat_{index}",
            source_ref="script",
            source_order=index,
            content=f"Source line {index}.",
        )
        for index in range(4)
    ]
    graph = CandidateGraph(
        graph_id="graph_batch_order",
        project_id="proj",
        nodes=[
            _node(f"cand_{index}", [f"mat_{index}"], start=index)
            for index in range(4)
        ],
    )
    selection = SelectionPlan(
        plan_id="sel_batch_order",
        project_id="proj",
        persona_id="host",
        selected_sequence=["cand_0", "cand_1", "cand_2", "cand_3"],
        decisions=[_decision(f"cand_{index}") for index in range(4)],
    )
    later_batch_completed = Event()
    lock = Lock()
    completion_order: list[str] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        unit_id = kwargs["work_unit"].unit_id
        if unit_id == "batch:cand_0":
            assert later_batch_completed.wait(timeout=5)
            with lock:
                completion_order.append(unit_id)
        else:
            assert unit_id == "batch:cand_2"
            with lock:
                completion_order.append(unit_id)
            later_batch_completed.set()
        return (
            "NODE C001\nK M001\nENDNODE\n"
            "NODE C002\nK M002\nENDNODE",
            _record(f"call-{unit_id}", kwargs),
        )

    output = build_node_edit_outputs(
        selection,
        graph,
        atoms,
        config=_config(tmp_path, max_nodes_per_batch=2),
        store=RunStore(tmp_path / "run_batch_order"),
        call=fake_call,
    )

    assert completion_order == ["batch:cand_2", "batch:cand_0"]
    assert [result.candidate_id for result in output.plan.node_edits] == (
        selection.selected_sequence
    )
    assert [atom.id for atom in output.edited_materials] == [
        "mat_0",
        "mat_1",
        "mat_2",
        "mat_3",
    ]
    assert [
        result.metadata["producer_unit_id"] for result in output.plan.node_edits
    ] == [
        "batch:cand_0",
        "batch:cand_0",
        "batch:cand_2",
        "batch:cand_2",
    ]
    assert output.plan.metadata["batch_count"] == 2
    assert output.reports == []
