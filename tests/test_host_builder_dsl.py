"""Strict two-call Host Builder DSL parsing and projection tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from tracecast.host_builder import (
    HostBuildParseError,
    PreferencePiece,
    parse_examples_response,
    parse_profile_response,
    render_examples_user_prompt,
    render_profile_user_prompt,
    selected_example_rows,
)


def _piece(candidate_id: str, text: str) -> PreferencePiece:
    return PreferencePiece(
        candidate_id=candidate_id,
        input_path=f"/{candidate_id}.txt",
        input_content_hash=f"sha256:{candidate_id}",
        span_start=3,
        span_end=3 + len(text),
        text=text,
        estimated_tokens=len(text),
    )


def _selected() -> dict:
    return {
        "opening": (_piece("open-1", "开头原文一。"), _piece("open-2", "开头原文二。")),
        "middle": (_piece("mid-1", "中段原文一。"), _piece("mid-2", "中段原文二。")),
        "ending": (_piece("end-1", "结尾原文一。"), _piece("end-2", "结尾原文二。")),
    }


def _expected() -> dict:
    return {
        alias: (position, piece)
        for alias, position, piece in selected_example_rows(_selected())
    }


def _example_response(expected: dict | None = None) -> str:
    rows = []
    for alias in (expected or _expected()):
        rows.extend(
            (
                f"EXAMPLE {alias}",
                "SOURCE",
                f"{alias} 的朴素内容骨架。",
                "ENDSOURCE",
                "ENDEXAMPLE",
            )
        )
    return "\n".join(rows)


def test_profile_parser_requires_complete_fields_and_keeps_optional_lists() -> None:
    profile = parse_profile_response(
        "\n".join(
            (
                "OUTPUT_LANGUAGE zh-CN",
                "VIEWPOINT 关注人物选择与后果。",
                "PREFER 具体行动与转折",
                "PREFER 可验证的矛盾",
                "AVOID 空泛总结",
                "THINKING_RULE 先区分事实与判断。",
                "EXPRESSION 自然、克制、清楚的中文口语。",
                "EXPRESSION_RULE 句子长短交替。",
            )
        )
    )

    assert profile.output_language == "zh-CN"
    assert profile.prefer == ["具体行动与转折", "可验证的矛盾"]
    assert profile.avoid == ["空泛总结"]
    assert profile.thinking_rules == ["先区分事实与判断。"]
    assert profile.expression_description == "自然、克制、清楚的中文口语。"
    assert profile.expression_rules == ["句子长短交替。"]


def test_profile_parser_combines_repeated_expression_fragments_in_order() -> None:
    profile = parse_profile_response(
        "\n".join(
            (
                "OUTPUT_LANGUAGE zh-CN",
                "VIEWPOINT 从个人疑问切入，再展开机制与叙事。",
                "EXPRESSION 用口语化表达传递情绪。",
                "EXPRESSION 用简短过渡句衔接段落。",
                "EXPRESSION 用固定告别语收束。",
                "EXPRESSION_RULE 先给生活化类比，再回到材料本身。",
            )
        )
    )

    assert profile.expression_description == (
        "用口语化表达传递情绪。 用简短过渡句衔接段落。 用固定告别语收束。"
    )
    assert profile.expression_rules == ["先给生活化类比，再回到材料本身。"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("VIEWPOINT 角度\nEXPRESSION 表达", "missing profile fields"),
        (
            "OUTPUT_LANGUAGE zh-CN\nOUTPUT_LANGUAGE zh\nVIEWPOINT 角度\nEXPRESSION 表达",
            "duplicate profile field",
        ),
        (
            "OUTPUT_LANGUAGE zh-CN\nVIEWPOINT 角度\nVIEWPOINT 另一角度\nEXPRESSION 表达",
            "duplicate profile field",
        ),
        ("OUTPUT_LANGUAGE zh-CN\nVIEWPOINT 角度", "missing profile fields"),
        (
            "OUTPUT_LANGUAGE zh-CN\nVIEWPOINT 角度\nEXPRESSION 表达\nTOPIC 游戏",
            "unknown profile field",
        ),
        (
            "OUTPUT_LANGUAGE zh-CN\nVIEWPOINT 角度\nEXPRESSION 表达\n额外说明",
            "invalid profile line",
        ),
    ],
)
def test_profile_parser_rejects_missing_duplicate_unknown_and_format_text(
    response: str,
    message: str,
) -> None:
    with pytest.raises(HostBuildParseError, match=message):
        parse_profile_response(response)


def test_example_parser_rebuilds_render_from_original_literal_piece() -> None:
    expected = _expected()
    compiled = parse_examples_response(_example_response(expected), expected=expected)

    assert set(compiled) == set(expected)
    for alias, example in compiled.items():
        assert example.render == expected[alias][1].text
        assert example.source != example.render
        assert example.source == f"{alias} 的朴素内容骨架。"
        assert example.lesson is None


@pytest.mark.parametrize("case", ["missing", "unknown", "duplicate"])
def test_example_parser_requires_exactly_six_unique_program_aliases(case: str) -> None:
    expected = _expected()
    response = _example_response(expected)
    first_block, remainder = response.split("\nEXAMPLE E002", 1)
    if case == "missing":
        response = "EXAMPLE E002" + remainder
    elif case == "unknown":
        response = response.replace("EXAMPLE E006", "EXAMPLE E999", 1)
    else:
        response += "\n" + first_block

    with pytest.raises(HostBuildParseError):
        parse_examples_response(response, expected=expected)


@pytest.mark.parametrize(
    "response",
    [
        "preface\n" + _example_response(),
        _example_response().replace("ENDSOURCE", "", 1),
        _example_response().replace("ENDEXAMPLE", "", 1),
        # A lesson is no longer part of the protocol, so emitting one is
        # out-of-format output rather than an extra field the parser may drop.
        _example_response().replace(
            "ENDSOURCE", "ENDSOURCE\nLESSON\n位置学习点。\nENDLESSON", 1
        ),
    ],
)
def test_example_parser_rejects_format_outside_complete_blocks(response: str) -> None:
    with pytest.raises(HostBuildParseError):
        parse_examples_response(response, expected=_expected())


def test_example_parser_rejects_source_equal_to_render() -> None:
    expected = _expected()
    response = _example_response(expected).replace(
        "E001 的朴素内容骨架。",
        expected["E001"][1].text,
        1,
    )

    with pytest.raises(HostBuildParseError, match="source equals render"):
        parse_examples_response(response, expected=expected)


def test_selected_aliases_and_prompt_projection_are_stable_and_positioned() -> None:
    selected = _selected()
    rows = selected_example_rows(selected)

    assert [(alias, position) for alias, position, _ in rows] == [
        ("E001", "opening"),
        ("E002", "opening"),
        ("E003", "middle"),
        ("E004", "middle"),
        ("E005", "ending"),
        ("E006", "ending"),
    ]
    profile_prompt = render_profile_user_prompt(
        template="{{samples}}",
        samples=[("P001", "opening", rows[0][2]), ("P002", "middle", rows[2][2])],
    )
    example_prompt = render_examples_user_prompt(template="{{examples}}", samples=rows)
    assert "SAMPLE P001 | position=opening" in profile_prompt
    assert "SAMPLE P002 | position=middle" in profile_prompt
    assert profile_prompt.count("ENDSAMPLE") == 2
    assert example_prompt.count("EXAMPLE_INPUT E") == 6
    assert example_prompt.count("ENDEXAMPLE_INPUT") == 6


def test_parser_never_mutates_expected_piece_mapping() -> None:
    expected = _expected()
    before = deepcopy(expected)

    parse_examples_response(_example_response(expected), expected=expected)

    assert expected == before
