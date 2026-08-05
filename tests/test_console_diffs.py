"""Unit tests for the console's pure diff helpers (``tracecast.console.diffs``).

These are stdlib-only pure functions -- no FastAPI, no IO, no LLM. Selection
diffs must align by ``candidate_id`` (stable across reruns), never by
``plan_id`` (it folds in the ordered path, so it changes whenever the path
does).
"""

from __future__ import annotations

from tracecast.console.diffs import (
    diff_blocks,
    diff_prompt_requests,
    diff_selection,
    diff_texts,
    split_sentences,
)
from tracecast.domain import Role, SelectionDecision, SelectionPlan, SelectionStatus, Treatment


def _accepted(
    candidate_id: str,
    *,
    role=Role.core,
    treatment=Treatment.paraphrase,
    directive="按当前节点功能推进叙事。",
) -> SelectionDecision:
    return SelectionDecision(
        candidate_id=candidate_id,
        status=SelectionStatus.accepted,
        role=role,
        treatment=treatment,
        directive=directive,
    )


def _plan(plan_id: str, sequence: list[str], decisions: list[SelectionDecision]) -> SelectionPlan:
    return SelectionPlan(
        plan_id=plan_id,
        project_id="proj",
        persona_id="host",
        selected_sequence=sequence,
        decisions=decisions,
        metadata={"diagnostics": []},
    )


# --- sentence split / text diff ---------------------------------------------


def test_split_sentences_keeps_delimiters_and_tail() -> None:
    text = "第一句。第二句！还有一个问题？收尾没有标点"
    assert split_sentences(text) == ["第一句。", "第二句！", "还有一个问题？", "收尾没有标点"]


def test_split_sentences_ignores_blank_segments() -> None:
    assert split_sentences("。。\n\n好句。") == ["。", "。", "好句。"]


def test_diff_texts_identical_is_single_equal_op() -> None:
    text = "同一句。再一句。"
    result = diff_texts(text, text)
    assert [op["op"] for op in result["ops"]] == ["equal"]
    assert result["ratio"] == 1.0


def test_diff_texts_reports_replaced_sentences() -> None:
    result = diff_texts("开场。中段旧说法。结尾。", "开场。中段新说法。结尾。")
    replaced = [op for op in result["ops"] if op["op"] == "replace"]
    assert len(replaced) == 1
    assert replaced[0]["a"] == ["中段旧说法。"]
    assert replaced[0]["b"] == ["中段新说法。"]
    assert 0 < result["ratio"] < 1


# --- block / prompt diffs ----------------------------------------------------


def test_diff_blocks_flags_only_changed_keys() -> None:
    blocks_a = {"annotated_materials_block": "材料 x", "forbidden_block": "none"}
    blocks_b = {"annotated_materials_block": "材料 y", "forbidden_block": "none"}
    result = {entry["key"]: entry for entry in diff_blocks(blocks_a, blocks_b)}
    assert result["annotated_materials_block"]["changed"] is True
    assert any(
        line.startswith("+材料 y")
        for line in result["annotated_materials_block"]["diff"]
    )
    assert result["forbidden_block"]["changed"] is False
    assert "diff" not in result["forbidden_block"]


def test_diff_blocks_reports_one_sided_keys() -> None:
    result = {e["key"]: e for e in diff_blocks({"only_a": "x"}, {"only_b": "y"})}
    assert result["only_a"]["in_a"] and not result["only_a"]["in_b"]
    assert result["only_b"]["in_b"] and not result["only_b"]["in_a"]


def test_diff_prompt_requests_meta_and_messages() -> None:
    request_a = {
        "model": "m1",
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "旧输入。"},
        ],
    }
    request_b = {
        "model": "m2",
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "新输入。"},
        ],
    }
    result = diff_prompt_requests(request_a, request_b)
    assert result["meta_changed"] == {"model": ["m1", "m2"]}
    assert result["messages"][0]["changed"] is False
    assert result["messages"][1]["changed"] is True
    assert result["messages"][1]["role"] == "user"


# --- selection path diff -----------------------------------------------------


def test_diff_selection_added_removed_with_labels() -> None:
    plan_a = _plan("plan-a", ["c1", "c2"], [_accepted("c1"), _accepted("c2")])
    plan_b = _plan("plan-b", ["c1", "c3"], [_accepted("c1"), _accepted("c3")])
    result = diff_selection(plan_a, plan_b, labels={"c2": "旧节点", "c3": "新节点"})
    assert [e["candidate_id"] for e in result["added"]] == ["c3"]
    assert result["added"][0]["summary"] == "新节点"
    assert result["added"][0]["position_b"] == 1
    assert [e["candidate_id"] for e in result["removed"]] == ["c2"]
    assert result["removed"][0]["summary"] == "旧节点"
    assert result["unchanged"] == 1


def test_diff_selection_detects_reorder_as_moved() -> None:
    decisions = [_accepted("c1"), _accepted("c2"), _accepted("c3")]
    plan_a = _plan("plan-a", ["c1", "c2", "c3"], decisions)
    plan_b = _plan("plan-b", ["c2", "c1", "c3"], decisions)
    result = diff_selection(plan_a, plan_b)
    moved_ids = {e["candidate_id"] for e in result["moved"]}
    assert moved_ids  # at least one of the swapped pair reported
    assert moved_ids <= {"c1", "c2"}
    assert not result["added"] and not result["removed"]


def test_diff_selection_field_changes_use_enum_values() -> None:
    plan_a = _plan(
        "plan-a", ["c1"], [_accepted("c1", role=Role.core, directive="平铺直叙")]
    )
    plan_b = _plan(
        "plan-b", ["c1"], [_accepted("c1", role=Role.color, directive="加一点悬念")]
    )
    result = diff_selection(plan_a, plan_b)
    assert len(result["changed"]) == 1
    fields = result["changed"][0]["fields"]
    assert fields["role"] == ["core", "color"]
    assert fields["directive"] == ["平铺直叙", "加一点悬念"]
    assert result["unchanged"] == 0


def test_diff_selection_identical_plans_all_unchanged() -> None:
    decisions = [_accepted("c1"), _accepted("c2")]
    plan = _plan("plan-a", ["c1", "c2"], decisions)
    result = diff_selection(plan, plan)
    assert not result["added"] and not result["removed"]
    assert not result["moved"] and not result["changed"]
    assert result["unchanged"] == 2
    assert result["plan_a"]["selected"] == 2
