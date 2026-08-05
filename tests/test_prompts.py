"""Tests for versioned route prompt loading and atom user-template rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from tracecast.domain import ContentKind, SourcePiece, piece_id
from tracecast.ingestion.dsl import (
    WindowParseError,
    build_manifest,
    build_window_blocks,
    parse_window_response,
    render_window_user_prompt,
)
from tracecast.ingestion.inputs import SourceProfile
from tracecast.prompt_components import load_ingestion_content_kind_catalog
from tracecast.prompts import (
    extract_prompt_blocks,
    load_system_prompt,
    load_user_prompt,
    substitute_prompt_blocks,
)
from tracecast.storage import RuntimeConfig


def _config(system_path: str, user_path: str | None = None) -> RuntimeConfig:
    route = {
        "stage": "atomizer",
        "provider": "gateway",
        "model": "atomizer-model",
        "system_prompt": system_path,
    }
    if user_path is not None:
        route["user_prompt"] = user_path
    return RuntimeConfig.model_validate({"routes": {"atom_boundary_dsl": route}})


def test_load_system_prompt_reads_stage_subdirectory_path(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt" / "ingestion" / "atom_boundary_dsl.system.zh.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("<!-- version: atom_boundary_dsl@test -->\n系统提示\n", encoding="utf-8")

    body, version = load_system_prompt(
        "atom_boundary_dsl",
        _config("prompt/ingestion/atom_boundary_dsl.system.zh.md"),
        base_dir=tmp_path,
    )

    assert body == "系统提示"
    assert version == "atom_boundary_dsl@test"


def test_load_user_prompt_reads_template_path(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt" / "ingestion" / "atom_boundary_dsl.user.zh.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text(
        "<!-- version: atom_boundary_dsl_user@test -->\n用户模板 {{piece_lines}}\n",
        encoding="utf-8",
    )

    body, version = load_user_prompt(
        "atom_boundary_dsl",
        _config(
            "prompt/ingestion/atom_boundary_dsl.system.zh.md",
            "prompt/ingestion/atom_boundary_dsl.user.zh.md",
        ),
        base_dir=tmp_path,
    )

    assert body == "用户模板 {{piece_lines}}"
    assert version == "atom_boundary_dsl_user@test"


def test_bundled_prompt_loading_is_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = RuntimeConfig.load(
        repo_root / "configs" / "runtime.example.json", load_env=False
    )
    monkeypatch.chdir(tmp_path)

    body, version = load_system_prompt("atom_boundary_dsl", config)

    assert body
    assert version.startswith("atom_boundary_dsl@")


def test_load_system_prompt_requires_first_line_version(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt" / "ingestion" / "atom_boundary_dsl.system.zh.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("没有版本头\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing"):
        load_system_prompt(
            "atom_boundary_dsl",
            _config("prompt/ingestion/atom_boundary_dsl.system.zh.md"),
            base_dir=tmp_path,
        )


def test_render_window_user_prompt_fills_only_dynamic_slots() -> None:
    ids = [piece_id("src", 0), piece_id("src", 1)]
    manifest = build_manifest(ids)
    pieces = {
        ids[0]: SourcePiece(
            piece_id=ids[0],
            source_id="src",
            order_index=0,
            text="第一段\n对白",
            char_count=5,
        ),
        ids[1]: SourcePiece(
            piece_id=ids[1],
            source_id="src",
            order_index=1,
            text="第二段",
            char_count=3,
        ),
    }
    profile = SourceProfile.build(
        allowed_content_kinds=[ContentKind.dialogue, ContentKind.narration],
        allowed_tags=["speaker_turn", "scene_boundary"],
    )
    template = (
        "aliases: {{alias_range}}\n"
        "kinds:\n{{allowed_content_kinds}}\n"
        "kind descriptions:\n{{content_kind_descriptions}}\n"
        "tags:\n{{allowed_tags}}\n"
        "tag descriptions:\n{{tag_descriptions}}\n"
        "pieces:\n{{piece_lines}}"
    )

    rendered = render_window_user_prompt(
        template=template,
        manifest=manifest,
        piece_by_id=pieces,
        profile=profile,
    )

    assert "- dialogue" in rendered
    assert "aliases: p0001 到 p0002" in rendered
    assert "- narration" in rendered
    assert "- dialogue：" in rendered
    assert "- speaker_turn" in rendered
    assert "- scene_boundary：场景、地点或叙事段落发生明确切换。" in rendered
    assert "- speaker_turn：对话中的说话人轮次或回应关系。" in rendered
    assert "- state_change：" not in rendered
    assert "p0001 | 第一段 [NL] 对白" in rendered
    assert "p0002 | 第二段" in rendered


def test_build_window_blocks_requires_tag_description_placeholder() -> None:
    ids = [piece_id("src", 0)]
    manifest = build_manifest(ids)
    pieces = {
        ids[0]: SourcePiece(
            piece_id=ids[0],
            source_id="src",
            order_index=0,
            text="第一段",
            char_count=3,
        )
    }
    profile = SourceProfile.build(
        allowed_content_kinds=[ContentKind.narration],
        allowed_tags=["speaker_turn", "scene_boundary"],
    )

    blocks = build_window_blocks(
        manifest=manifest,
        piece_by_id=pieces,
        profile=profile,
    )

    assert blocks["allowed_tags"] == "- scene_boundary\n- speaker_turn"
    assert blocks["tag_descriptions"] == (
        "- scene_boundary：场景、地点或叙事段落发生明确切换。\n"
        "- speaker_turn：对话中的说话人轮次或回应关系。"
    )
    template_without_tag_descriptions = (
        "{{alias_range}}\n"
        "{{allowed_content_kinds}}\n"
        "{{content_kind_descriptions}}\n"
        "{{allowed_tags}}\n"
        "{{piece_lines}}"
    )
    with pytest.raises(ValueError, match="unexpected blocks.*tag_descriptions"):
        substitute_prompt_blocks(
            template_without_tag_descriptions,
            blocks,
            prompt_name="atom",
        )


def test_parse_atom_line_accepts_exactly_four_fields_with_empty_tags() -> None:
    ids = [piece_id("src", 0)]
    manifest = build_manifest(ids)
    profile = SourceProfile.build(allowed_content_kinds=[ContentKind.assertion])

    parsed = parse_window_response(
        "A | p0001 | assertion | \n",
        manifest=manifest,
        profile=profile,
        window_piece_ids=ids,
        source_id="src",
        window_id="win",
    )

    assert parsed.atoms[0].content_kind == ContentKind.assertion
    assert parsed.atoms[0].tags == ()


@pytest.mark.parametrize(
    "legacy_row",
    [
        "A | p0001 | opinion | | attr:Author",
        "A | p0001 | opinion | interpretation | attr:Author",
    ],
)
def test_parse_atom_line_rejects_legacy_attribution_field(legacy_row: str) -> None:
    ids = [piece_id("src", 0)]
    manifest = build_manifest(ids)
    profile = SourceProfile.build(
        allowed_content_kinds=[ContentKind.opinion],
        allowed_tags=["interpretation"],
    )

    with pytest.raises(WindowParseError, match="malformed atom row"):
        parse_window_response(
            legacy_row,
            manifest=manifest,
            profile=profile,
            window_piece_ids=ids,
            source_id="src",
            window_id="win",
        )


def test_opinion_content_kind_description_does_not_require_attribution() -> None:
    description = load_ingestion_content_kind_catalog().descriptions[ContentKind.opinion]

    assert description == "来源中的观点、评价或解释。"
    assert "attr:" not in description


def test_prompt_blocks_round_trip_and_require_exact_placeholder_set() -> None:
    template = "开头\n{{first}}\n中间\n{{second}}\n结尾"
    blocks = {"first": "第一块\n两行", "second": "第二块"}
    rendered = substitute_prompt_blocks(template, blocks, prompt_name="test")
    assert extract_prompt_blocks(template, rendered) == blocks
    with pytest.raises(ValueError, match="missing blocks"):
        substitute_prompt_blocks(template, {"first": "only"}, prompt_name="test")
    with pytest.raises(ValueError, match="unexpected blocks"):
        substitute_prompt_blocks(
            template,
            {**blocks, "audit_metadata": "must not leak"},
            prompt_name="test",
        )


def test_prompt_block_values_preserve_literal_placeholders_without_reprocessing() -> None:
    template = "{{first}}\n---\n{{second}}"
    rendered = substitute_prompt_blocks(
        template,
        {
            "first": "源码示例 {{literal_name}}\n也不要展开 {{second}}",
            "second": "第二块",
        },
        prompt_name="test",
    )

    assert rendered == "源码示例 {{literal_name}}\n也不要展开 {{second}}\n---\n第二块"
