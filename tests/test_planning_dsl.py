"""Planning DSL parser tests."""

from __future__ import annotations

from tracecast.planning.dsl import (
    parse_node_compression_dsl,
    parse_planner_dsl,
    parse_selector_directive_dsl,
    parse_selector_dsl,
)
from tracecast.planning.policy import target_node_count


def test_planner_mats_accepts_alias_ranges() -> None:
    parsed = parse_planner_dsl(
        "N N1 | fn=reveal | mats=M001-M003,M006 | grouped summary\n"
    )

    assert parsed.diagnostics == []
    assert parsed.nodes[0].material_aliases == ["M001", "M002", "M003", "M006"]


def test_planner_mats_accepts_shorthand_node_head() -> None:
    parsed = parse_planner_dsl(
        "N1 | fn=reveal | mats=M001-M003 | grouped summary\n"
    )

    assert parsed.diagnostics == []
    assert parsed.nodes[0].handle == "N1"
    assert parsed.nodes[0].material_aliases == ["M001", "M002", "M003"]


def test_planner_node_pass_reports_edges_without_accepting_them() -> None:
    parsed = parse_planner_dsl(
        "N N1 | fn=reveal | mats=M001 | summary\n"
        "E causal_link | cause=N1 | effect=N2 | no longer accepted\n"
    )

    assert [node.handle for node in parsed.nodes] == ["N1"]
    assert [item["kind"] for item in parsed.diagnostics] == ["unexpected_edge_in_node_pass"]


def test_node_compression_kids_accepts_alias_ranges() -> None:
    parsed = parse_node_compression_dsl(
        "N N1 | kids=C001-C002,C005 | macro summary\n"
    )

    assert parsed.diagnostics == []
    assert parsed.nodes[0].child_aliases == ["C001", "C002", "C005"]


def test_node_compression_kids_accepts_shorthand_node_head() -> None:
    parsed = parse_node_compression_dsl(
        "N1 | kids=C001-C002,C005 | macro summary\n"
    )

    assert parsed.diagnostics == []
    assert parsed.nodes[0].handle == "N1"
    assert parsed.nodes[0].child_aliases == ["C001", "C002", "C005"]


def test_node_compression_rejects_fn_field() -> None:
    parsed = parse_node_compression_dsl(
        "N N1 | fn=state_change | kids=C001-C002 | macro summary\n"
    )

    assert [item["kind"] for item in parsed.diagnostics] == ["compress_node_field_count"]
    assert parsed.nodes == []


def test_selector_path_accepts_exactly_three_fields() -> None:
    parsed = parse_selector_dsl(
        "P C001 | bridge | s\n"
        "P C002 | core | paraphrase\n"
    )

    assert parsed.diagnostics == []
    assert len(parsed.decisions) == 2
    assert [(row.alias, row.role, row.treatment) for row in parsed.decisions] == [
        ("C001", "bridge", "s"),
        ("C002", "core", "paraphrase"),
    ]


def test_selector_rejects_legacy_fourth_field() -> None:
    parsed = parse_selector_dsl("P C001 | core | p | \n")

    assert parsed.decisions == []
    assert [item["kind"] for item in parsed.diagnostics] == ["path_field_count"]


def test_selector_rejects_legacy_five_field_row() -> None:
    parsed = parse_selector_dsl(
        "P C001 | core | paraphrase | 悬置问题留待后文 | 旧版选择理由\n"
    )

    assert parsed.decisions == []
    assert [item["kind"] for item in parsed.diagnostics] == ["path_field_count"]


def test_selector_directive_parser_compiles_sentinel_and_preserves_text() -> None:
    directive = "先制造疑问，再把答案留给后续揭露。"
    parsed = parse_selector_directive_dsl(
        "D P001 | -\n"
        f"D P002 | {directive}\n"
    )

    assert parsed.diagnostics == []
    assert [(row.alias, row.directive) for row in parsed.directives] == [
        ("P001", None),
        ("P002", directive),
    ]


def test_selector_directive_parser_rejects_empty_or_extra_fields() -> None:
    parsed = parse_selector_directive_dsl(
        "D P001 | \n"
        "D P002 | 控制语 | 多余字段\n"
    )

    assert parsed.directives == []
    assert [item["kind"] for item in parsed.diagnostics] == [
        "directive_missing_value",
        "directive_field_count",
    ]


def test_default_planning_target_caps_window_ratio_total_at_selector_scale() -> None:
    assert target_node_count({}, 1) == 1
    assert target_node_count({}, 2) == 2
    assert target_node_count({}, 200) == 170


def test_explicit_positive_planning_target_precedes_ratio_but_nonpositive_falls_back() -> None:
    assert target_node_count({"target_final_node_count": 40}, 100) == 40
    assert target_node_count({"target_final_node_count": 400}, 1) == 170
    assert target_node_count({"target_final_node_count": 0}, 3) == 3
    assert target_node_count({"target_final_node_count": -1}, 3) == 3
