"""Deterministic Host Builder corpus preparation and sampling regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracecast.domain import estimate_tokens
from tracecast.host_builder.sampling import (
    build_position_pools,
    build_profile_sample,
    expand_input_paths,
    is_bcp47_language_tag,
    language_script_family,
    prepare_corpus,
    select_style_examples,
    split_preference_spans,
    trim_platform_boilerplate,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _three_piece_text(char: str) -> str:
    return f"{char * 4}。{char * 5}！{char * 6}？"


def _selected_ids(selected: dict) -> tuple[str, ...]:
    return tuple(
        row.candidate_id
        for position in ("opening", "middle", "ending")
        for row in selected[position]
    )


def test_expand_inputs_is_non_recursive_filename_sorted_and_deduplicated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    nested = _write(root / "nested" / "ignored.txt", "不会递归读取")
    second = _write(root / "b.txt", "乙")
    first = _write(root / "A.txt", "甲")
    _write(root / "ignored.md", "不读取")

    expanded = expand_input_paths([root, second])

    assert expanded == [first.resolve(), second.resolve()]
    assert nested.resolve() not in expanded


def test_split_prefers_paragraph_then_sentence_then_clause_boundaries() -> None:
    paragraph_text = "甲" * 8 + "。\n\n" + "乙" * 12
    paragraph_spans = split_preference_spans(paragraph_text, max_tokens=14)
    assert paragraph_text[slice(*paragraph_spans[0])] == "甲" * 8 + "。\n\n"

    sentence_text = "甲" * 4 + "，" + "乙" * 4 + "。" + "丙" * 12
    sentence_spans = split_preference_spans(sentence_text, max_tokens=13)
    assert sentence_text[slice(*sentence_spans[0])].endswith("。")

    clause_text = "甲" * 4 + "，" + "乙" * 12
    clause_spans = split_preference_spans(clause_text, max_tokens=9)
    assert clause_text[slice(*clause_spans[0])].endswith("，")


def test_split_uses_newlines_for_subtitle_rows_and_hard_cuts_unbroken_text() -> None:
    subtitles = "第一行字幕\n第二行字幕\n第三行字幕"
    subtitle_spans = split_preference_spans(subtitles, max_tokens=8)
    assert subtitles[slice(*subtitle_spans[0])] == "第一行字幕\n"
    assert "".join(subtitles[slice(*span)] for span in subtitle_spans) == subtitles

    unbroken = "甲" * 27
    hard_spans = split_preference_spans(unbroken, max_tokens=10)
    assert "".join(unbroken[slice(*span)] for span in hard_spans) == unbroken
    assert all(estimate_tokens(unbroken[slice(*span)]) <= 10 for span in hard_spans)


def test_piece_spans_are_literal_trimmed_substrings_with_token_hard_limit(
    tmp_path: Path,
) -> None:
    text = "  甲乙丙丁。\n\n戊己庚辛壬癸。  "
    path = _write(tmp_path / "sample.txt", text)

    corpus = prepare_corpus([path], piece_max_tokens=7, seed=9)
    [document] = corpus.documents

    for piece in document.pieces:
        assert piece.text == text[piece.span_start : piece.span_end]
        assert piece.text == piece.text.strip()
        assert piece.estimated_tokens <= 7


def test_boilerplate_cleanup_removes_only_contiguous_edges() -> None:
    text = (
        "大家好，欢迎来到频道。\n"
        "我是小明。\n"
        "这是正文开头。\n"
        "正文引用观众的一句‘记得点赞’，随后继续分析。\n"
        "这是正文结尾。\n"
        "感谢观看，下期再见。\n"
    )

    start, end = trim_platform_boilerplate(text)
    trimmed = text[start:end]

    assert trimmed.startswith("这是正文开头。")
    assert "记得点赞" in trimmed
    assert trimmed.endswith("这是正文结尾。")
    assert text[:start].startswith("大家好")
    assert text[end:].strip() == "感谢观看，下期再见。"


def _subtitle_body(lines: int = 60) -> str:
    return "".join(f"这是第{index}句正文内容用来撑起文档主体。\n" for index in range(1, lines + 1))


def test_trailing_cleanup_reaches_a_cta_block_behind_a_non_cta_sign_off() -> None:
    """Subtitle-style corpora split one CTA sentence across several lines.

    A strictly contiguous scan stops at the first unmatched line, so a closing
    greeting shields the whole outro and the ending style example ships a
    platform CTA the renderer then imitates.
    """

    body = _subtitle_body()
    text = body + (
        "我是某某某UP主\n"
        "如果你觉得视频还不错的话\n"
        "请点个赞投个币支持一下\n"
        "当然能订阅关注那自然就更好了\n"
        "最后给大家拜个年\n"
        "祝大家新年快乐\n"
    )

    start, end = trim_platform_boilerplate(text)
    trimmed = text[start:end]
    removed = text[end:]

    assert trimmed.endswith("这是第60句正文内容用来撑起文档主体。")
    assert removed.strip().startswith("我是某某某UP主")
    for phrase in ("点个赞", "投个币", "订阅关注", "拜个年", "新年快乐"):
        assert phrase in removed
        assert phrase not in trimmed


def test_trailing_cleanup_stops_after_the_gap_budget_is_exhausted() -> None:
    body = _subtitle_body()
    filler = "这是一句正文甲\n这是一句正文乙\n"
    within_budget = body + "请大家点赞支持\n" + filler + "我们下期再见\n"
    beyond_budget = body + "请大家点赞支持\n" + filler + "这是一句正文丙\n我们下期再见\n"

    _start, end = trim_platform_boilerplate(within_budget)
    assert "请大家点赞支持" in within_budget[end:]

    start, end = trim_platform_boilerplate(beyond_budget)
    trimmed = beyond_budget[start:end]
    assert trimmed.endswith("这是一句正文丙")
    assert "请大家点赞支持" in trimmed


def test_trailing_cleanup_falls_back_to_contiguous_when_the_suffix_is_too_large() -> None:
    """An outro is a small tail; a quarter of the body is a false positive."""

    text = "正文引用观众的一句‘记得点赞’，随后继续分析。\n这是正文结尾。\n感谢观看，下期再见。\n"

    start, end = trim_platform_boilerplate(text)
    trimmed = text[start:end]

    assert "记得点赞" in trimmed
    assert trimmed.endswith("这是正文结尾。")
    assert text[end:].strip() == "感谢观看，下期再见。"


def test_trailing_cleanup_covers_latin_platform_outros() -> None:
    body = "".join(f"This is body sentence {index}.\n" for index in range(1, 41))
    text = body + (
        "I'm your host, back with another one.\n"
        "If you enjoyed this, like and subscribe.\n"
        "Ring the bell for more.\n"
        "Thanks for watching.\n"
        "See you next week.\n"
    )

    start, end = trim_platform_boilerplate(text)
    trimmed = text[start:end]
    removed = text[end:]

    assert trimmed.endswith("This is body sentence 40.")
    assert "subscribe" in removed
    assert "Thanks for watching" in removed
    assert "subscribe" not in trimmed


def test_position_pools_use_first_last_and_strictly_internal_pieces(
    tmp_path: Path,
) -> None:
    paths = [
        _write(tmp_path / f"{index}.txt", _three_piece_text(char))
        for index, char in enumerate("甲乙丙丁", start=1)
    ]
    corpus = prepare_corpus(paths, piece_max_tokens=7, seed=17)

    pools = build_position_pools(corpus)

    assert {row.candidate_id for row in pools["opening"]} == {
        document.pieces[0].candidate_id for document in corpus.documents
    }
    assert {row.candidate_id for row in pools["ending"]} == {
        document.pieces[-1].candidate_id for document in corpus.documents
    }
    assert {row.candidate_id for row in pools["middle"]} == {
        piece.candidate_id
        for document in corpus.documents
        for piece in document.pieces[1:-1]
    }


def test_sampling_is_unique_source_balanced_and_seed_reproducible(
    tmp_path: Path,
) -> None:
    paths = [
        _write(tmp_path / f"{index}.txt", _three_piece_text(char))
        for index, char in enumerate("甲乙丙丁", start=1)
    ]
    first = prepare_corpus(paths, piece_max_tokens=7, seed=23)
    repeated = prepare_corpus(list(reversed(paths)), piece_max_tokens=7, seed=23)

    selected = select_style_examples(first)
    selected_again = select_style_examples(repeated)

    assert _selected_ids(selected) == _selected_ids(selected_again)
    assert len(set(_selected_ids(selected))) == 6
    assert all(
        len({row.input_path for row in selected[position]}) == 2
        for position in ("opening", "middle", "ending")
    )


def test_single_file_sampling_keeps_boundary_pools_and_uses_available_middle_rows(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "single.txt",
        "甲甲甲甲。乙乙乙乙！丙丙丙丙？丁丁丁丁。戊戊戊戊！",
    )
    corpus = prepare_corpus([path], piece_max_tokens=7, seed=19)

    selected = select_style_examples(corpus)

    assert {position: len(rows) for position, rows in selected.items()} == {
        "opening": 1,
        "middle": 2,
        "ending": 1,
    }
    assert 3 <= len(_selected_ids(selected)) <= 6
    assert len(set(_selected_ids(selected))) == len(_selected_ids(selected))
    [document] = corpus.documents
    assert selected["opening"][0].candidate_id == document.pieces[0].candidate_id
    assert selected["ending"][0].candidate_id == document.pieces[-1].candidate_id
    assert all(row in document.pieces[1:-1] for row in selected["middle"])


def test_default_seed_is_order_independent_and_content_derived(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path / f"{index}.txt", _three_piece_text(char))
        for index, char in enumerate("甲乙丙丁", start=1)
    ]
    original = prepare_corpus(paths, piece_max_tokens=7)
    reordered = prepare_corpus(list(reversed(paths)), piece_max_tokens=7)
    paths[0].write_text(_three_piece_text("戊"), encoding="utf-8")
    changed = prepare_corpus(paths, piece_max_tokens=7)

    assert original.seed == reordered.seed
    assert original.seed != changed.seed


def test_profile_sample_keeps_all_selected_rows_and_respects_extra_budget(
    tmp_path: Path,
) -> None:
    paths = [
        _write(tmp_path / f"{index}.txt", _three_piece_text(char) * 3)
        for index, char in enumerate("甲乙丙丁", start=1)
    ]
    corpus = prepare_corpus(paths, piece_max_tokens=7, seed=31)
    selected = select_style_examples(corpus)
    selected_rows = [row for rows in selected.values() for row in rows]
    selected_tokens = sum(row.estimated_tokens for row in selected_rows)

    sample = build_profile_sample(
        corpus,
        selected,
        token_budget=selected_tokens + 14,
    )

    assert {row.candidate_id for row in selected_rows} <= {
        row.candidate_id for row in sample
    }
    assert sum(row.estimated_tokens for row in sample) <= selected_tokens + 14


def test_explicit_seed_can_change_selection_without_long_file_domination(
    tmp_path: Path,
) -> None:
    long_text = "".join(f"长段{index}。" for index in range(30))
    paths = [
        _write(tmp_path / "long.txt", long_text),
        _write(tmp_path / "a.txt", _three_piece_text("甲")),
        _write(tmp_path / "b.txt", _three_piece_text("乙")),
        _write(tmp_path / "c.txt", _three_piece_text("丙")),
    ]
    selections = {
        _selected_ids(select_style_examples(prepare_corpus(paths, piece_max_tokens=7, seed=seed)))
        for seed in range(1, 6)
    }

    assert len(selections) > 1
    for seed in range(1, 6):
        middle = select_style_examples(
            prepare_corpus(paths, piece_max_tokens=7, seed=seed)
        )["middle"]
        assert len({row.input_path for row in middle}) == 2


def test_insufficient_middle_candidates_fail_deterministically(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path / "one.txt", "只有一段。"),
        _write(tmp_path / "two.txt", "另外一段。"),
    ]
    corpus = prepare_corpus(paths, piece_max_tokens=180, seed=3)

    with pytest.raises(ValueError, match="not enough unique middle candidates"):
        select_style_examples(corpus)


def test_language_detection_requires_sixty_percent_and_excludes_foreign_files(
    tmp_path: Path,
) -> None:
    zh_paths = [
        _write(tmp_path / "zh1.txt", _three_piece_text("甲") * 3),
        _write(tmp_path / "zh2.txt", _three_piece_text("乙") * 3),
    ]
    english = _write(tmp_path / "english.txt", "This file is clearly English only.")
    corpus = prepare_corpus([*zh_paths, english], piece_max_tokens=7, seed=1)

    assert corpus.dominant_script == "cjk"
    english_record = next(row for row in corpus.input_records if row.path == str(english.resolve()))
    assert english_record.eligible is False
    assert english_record.exclusion_reason
    assert all(document.path != str(english.resolve()) for document in corpus.documents)

    ambiguous = _write(tmp_path / "ambiguous.txt", "甲乙丙丁abcd")
    with pytest.raises(ValueError, match="at or above 60%"):
        prepare_corpus([ambiguous])


def test_latin_dominant_corpus_prepares_as_latin(tmp_path: Path) -> None:
    paths = [
        _write(
            tmp_path / f"english-{index}.txt",
            "A concrete opening. A detailed middle passage. A concise ending.",
        )
        for index in range(1, 4)
    ]

    corpus = prepare_corpus(paths, piece_max_tokens=8, seed=5)

    assert corpus.dominant_script == "latin"
    assert all(row.eligible for row in corpus.input_records)
    assert all(document.pieces for document in corpus.documents)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("zh-CN", "cjk"),
        ("ja", "cjk"),
        ("ko-KR", "cjk"),
        ("en", "latin"),
        ("fr-FR", "latin"),
        ("ar", None),
    ],
)
def test_language_script_family(language: str, expected: str | None) -> None:
    assert language_script_family(language) == expected


@pytest.mark.parametrize("language", ["zh-CN", "en", "sr-Latn-RS", "de-DE-1996"])
def test_bcp47_language_tag_accepts_common_valid_shapes(language: str) -> None:
    assert is_bcp47_language_tag(language) is True


@pytest.mark.parametrize("language", ["", " zh-CN", "zh-CN extra", "zh_CN", "zh-"])
def test_bcp47_language_tag_rejects_non_tag_text(language: str) -> None:
    assert is_bcp47_language_tag(language) is False
    assert language_script_family(language) is None
