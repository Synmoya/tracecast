from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPT_ROOT = REPO_ROOT / "prompt"
LOCALES = ("zh", "en")
PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z0-9_]+)\s*}}")
VERSION_RE = re.compile(r"^<!--\s*version:\s*([^>]+?)\s*-->$")


DSL_LITERALS: dict[str, tuple[str, ...]] = {
    "host_builder/host_profile_distill.user": (
        "OUTPUT_LANGUAGE",
        "VIEWPOINT",
        "PREFER",
        "AVOID",
        "THINKING_RULE",
        "EXPRESSION",
        "EXPRESSION_RULE",
    ),
    "host_builder/host_style_example_reverse.user": (
        "EXAMPLE",
        "SOURCE",
        "ENDSOURCE",
        "ENDEXAMPLE",
    ),
    "ingestion/atom_boundary_dsl.system": ("content_kind", "tags", "piece", "atom"),
    "ingestion/atom_boundary_dsl.user": ("A |", "p0001-p0003", "[NL]"),
    "ingestion/atom_boundary_dsl.repair": ("A", "content_kind"),
    "intake/source_profile_probe.system": ("SRC", "TITLE", "NOTE", "REASON"),
    "intake/source_profile_probe.user": ("SRC", "TITLE", "NOTE", "REASON", "ENDSRC"),
    "name_resolution/name_table.system": (
        "aliases",
        "stable_name",
        "protected_surfaces",
        "SELECTED MATERIALS",
    ),
    "name_resolution/name_table.user": (
        "replacements",
        "aliases",
        "stable_name",
        "protected_surfaces",
        "SELECTED MATERIALS",
    ),
    "node_edit/node_edit.system": ("K", "C", "O", "TXT", "DIR"),
    "node_edit/node_edit.user": ("NODE", "K", "C", "TXT", "O", "ENDNODE"),
    "node_edit/node_edit_block_repair.user": ("NODE", "ENDNODE", "K", "C", "O"),
    "planning/planner_graph_dsl.system": ("TXT",),
    "planning/planner_graph_dsl.user": ("M001-M006,M010-M014", "N N1", "fn=", "mats="),
    "planning/planner_node_compress_dsl.system": ("SUM", "m", "sp"),
    "planning/planner_node_compress_dsl.user": (
        "C001-C006,C010-C014",
        "N N1",
        "kids=",
    ),
    "rendering/render_chunk.system": ("SUM", "DIRECTIVE"),
    "rendering/render_fact_context_dsl.system": (
        "CHUNK",
        "ENDCHUNK",
        "NODE",
        "ENDNODE",
        "CONTENT_SCHEDULED_HERE",
        "ACTION_SCHEDULED_HERE",
        "HINT CxxxNxxx |",
    ),
    "rendering/render_fact_context_dsl.user": ("HINT CxxxNxxx |",),
    "selection/selector_directive_dsl.user": ("P <path_alias>", "D <path_alias>"),
    "selection/selector_path_dsl.system": ("SUM", "role", "treatment"),
    "selection/selector_path_dsl.user": (
        "P <alias> | <role> | <treatment_code>",
        "core",
        "bridge",
        "context",
        "color",
        "q/p/s",
    ),
}


def _locale_files(locale: str, suffix: str) -> dict[str, Path]:
    marker = f".{locale}.{suffix}"
    return {
        path.relative_to(PROMPT_ROOT).as_posix().removesuffix(marker): path
        for path in PROMPT_ROOT.rglob(f"*.{locale}.{suffix}")
    }


def _normalized_version(version: str) -> str:
    return re.sub(r"\.(?:zh|en)(?=\.|@)", "", version)


def _json_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in value.items() if key != "version"}
    if isinstance(value, list):
        return [_json_shape(item) for item in value]
    return type(value).__name__


def test_prompt_markdown_variant_file_sets_placeholders_versions_and_dsl_match() -> None:
    by_locale = {locale: _locale_files(locale, "md") for locale in LOCALES}
    assert set(by_locale["zh"]) == set(by_locale["en"])

    for route in sorted(by_locale["zh"]):
        texts = {
            locale: by_locale[locale][route].read_text(encoding="utf-8")
            for locale in LOCALES
        }
        assert set(PLACEHOLDER_RE.findall(texts["zh"])) == set(
            PLACEHOLDER_RE.findall(texts["en"])
        ), route

        versions: dict[str, str] = {}
        for locale, text in texts.items():
            first_line = text.splitlines()[0]
            match = VERSION_RE.fullmatch(first_line)
            assert match is not None, f"{route}.{locale} lacks a first-line version header"
            versions[locale] = match.group(1)
        assert len(set(versions.values())) == len(LOCALES), route
        assert len({_normalized_version(version) for version in versions.values()}) == 1, route

        for literal in DSL_LITERALS.get(route, ()):
            for locale, text in texts.items():
                assert literal in text, f"{route}.{locale} lost DSL literal {literal!r}"


def test_prompt_json_variant_file_sets_keys_shapes_and_versions_match() -> None:
    by_locale = {locale: _locale_files(locale, "json") for locale in LOCALES}
    assert set(by_locale["zh"]) == set(by_locale["en"])

    for component in sorted(by_locale["zh"]):
        payloads = {
            locale: json.loads(by_locale[locale][component].read_text(encoding="utf-8"))
            for locale in LOCALES
        }
        assert _json_shape(payloads["zh"]) == _json_shape(payloads["en"])

        versions = {locale: payload["version"] for locale, payload in payloads.items()}
        assert len(set(versions.values())) == len(LOCALES), component
        assert len({_normalized_version(version) for version in versions.values()}) == 1, component


def test_removed_neutral_prompt_assets_are_not_present() -> None:
    assert not tuple(PROMPT_ROOT.rglob("*.neutral.md"))
    assert not tuple(PROMPT_ROOT.rglob("*.neutral.json"))


def test_name_table_prompt_uses_unfenced_compact_json_protocol() -> None:
    for locale in LOCALES:
        system = (
            PROMPT_ROOT / f"name_resolution/name_table.system.{locale}.md"
        ).read_text(encoding="utf-8")
        user = (
            PROMPT_ROOT / f"name_resolution/name_table.user.{locale}.md"
        ).read_text(encoding="utf-8")

        assert "```" not in system
        assert "```" not in user
        assert '{"replacements":[],"protected_surfaces":[]}' in user
        assert "24" in user
        assert "{{max_rules}}" in user
        assert "{{max_aliases_per_rule}}" in user
        assert "{{materials_block}}" in user
