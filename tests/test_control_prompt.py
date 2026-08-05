from __future__ import annotations

from pathlib import Path

import pytest

from tracecast.control_prompt import (
    CONTROL_PROMPT_POLICY_VERSION,
    ControlPromptDecision,
    ControlPromptLocale,
    ControlPromptSample,
    SourceLanguageClass,
    coerce_control_prompt_locale,
    control_prompt_locale_from_record,
    resolve_control_prompt_locale,
)
from tracecast.prompts import load_system_prompt
from tracecast.storage import RuntimeConfig


def _sample(text: str, language_tag: str | None) -> ControlPromptSample:
    return ControlPromptSample(text=text, language_tag=language_tag)


def test_control_prompt_locale_uses_weighted_zh_corpus() -> None:
    decision = resolve_control_prompt_locale(
        [
            _sample("这是一段中文主材料。" * 9, "zh-CN"),
            _sample("small English appendix", "en-US"),
        ]
    )

    assert decision.source_language is SourceLanguageClass.zh
    assert decision.variant is ControlPromptLocale.zh
    assert decision.confidence >= 0.8
    assert decision.policy_version == CONTROL_PROMPT_POLICY_VERSION


def test_control_prompt_locale_uses_weighted_en_corpus() -> None:
    decision = resolve_control_prompt_locale(
        [
            _sample("A long English source passage. " * 12, "en-US"),
            _sample("短中文附录", "zh-CN"),
        ]
    )

    assert decision.source_language is SourceLanguageClass.en
    assert decision.variant is ControlPromptLocale.en
    assert decision.distribution["declared_en"] >= 0.8
    assert decision.distribution["script_latin"] >= 0.8


@pytest.mark.parametrize(
    ("dominant", "minority", "expected_source_language", "expected_variant"),
    [
        ("中" * 8, "a" * 2, SourceLanguageClass.zh, ControlPromptLocale.zh),
        ("中" * 799, "a" * 201, SourceLanguageClass.mixed, ControlPromptLocale.en),
        ("a" * 8, "中" * 2, SourceLanguageClass.en, ControlPromptLocale.en),
        ("a" * 799, "中" * 201, SourceLanguageClass.mixed, ControlPromptLocale.en),
    ],
)
def test_control_prompt_locale_threshold_is_inclusive_at_exactly_080(
    dominant: str,
    minority: str,
    expected_source_language: SourceLanguageClass,
    expected_variant: ControlPromptLocale,
) -> None:
    dominant_tag = "zh-CN" if dominant.startswith("中") else "en-US"
    minority_tag = "en-US" if dominant_tag == "zh-CN" else "zh-CN"

    decision = resolve_control_prompt_locale(
        [_sample(dominant, dominant_tag), _sample(minority, minority_tag)]
    )

    assert decision.source_language is expected_source_language
    assert decision.variant is expected_variant


@pytest.mark.parametrize(
    ("samples", "expected_source_language"),
    [
        ([_sample("中英 mixed text", None)], SourceLanguageClass.unknown),
        ([_sample("中文内容", "en-US")], SourceLanguageClass.mixed),
        ([_sample("日本語の資料", "ja-JP")], SourceLanguageClass.other),
        (
            [
                _sample("中文材料" * 5, "zh-CN"),
                _sample("English source" * 5, "en-US"),
            ],
            SourceLanguageClass.mixed,
        ),
        ([], SourceLanguageClass.unknown),
    ],
)
def test_non_chinese_source_classes_use_english_control_prompt(
    samples: list[ControlPromptSample],
    expected_source_language: SourceLanguageClass,
) -> None:
    decision = resolve_control_prompt_locale(samples)

    assert decision.source_language is expected_source_language
    assert decision.variant is ControlPromptLocale.en


def test_control_prompt_locale_only_exposes_zh_and_en_assets() -> None:
    assert tuple(ControlPromptLocale) == (
        ControlPromptLocale.zh,
        ControlPromptLocale.en,
    )
    with pytest.raises(ValueError):
        ControlPromptLocale("neutral")


def test_legacy_neutral_records_normalize_to_english_without_selecting_an_asset() -> None:
    assert coerce_control_prompt_locale("neutral") is ControlPromptLocale.en
    assert (
        control_prompt_locale_from_record({"locale": "neutral", "variant": "neutral"})
        is ControlPromptLocale.en
    )

    upgraded = ControlPromptDecision.model_validate(
        {
            "locale": "neutral",
            "confidence": 0.4,
            "distribution": {
                "declared_zh": 0.5,
                "declared_en": 0.5,
                "declared_other": 0.0,
                "declared_missing": 0.0,
                "script_cjk": 0.5,
                "script_latin": 0.5,
            },
            "policy_version": "control_prompt_locale@v1",
            "variant": "neutral",
        }
    )
    assert upgraded.source_language is SourceLanguageClass.unknown
    assert upgraded.variant is ControlPromptLocale.en


def test_prompt_loader_expands_locale_marker_and_keeps_old_paths_compatible(
    tmp_path: Path,
) -> None:
    prompt_dir = tmp_path / "prompt" / "test"
    prompt_dir.mkdir(parents=True)
    (prompt_dir / "route.system.en.md").write_text(
        "<!-- version: route_en@test -->\nEnglish control prompt\n",
        encoding="utf-8",
    )
    (prompt_dir / "route.system.zh.md").write_text(
        "<!-- version: route_zh@test -->\n中文控制 prompt\n",
        encoding="utf-8",
    )
    config = RuntimeConfig.model_validate(
        {
            "routes": {
                "route": {
                    "stage": "test",
                    "provider": "gateway",
                    "model": "model",
                    "system_prompt": "prompt/test/route.system.{locale}.md",
                }
            }
        }
    )

    en_body, en_version = load_system_prompt(
        "route",
        config,
        control_prompt_locale=ControlPromptLocale.en,
        base_dir=tmp_path,
    )
    zh_body, zh_version = load_system_prompt("route", config, base_dir=tmp_path)

    assert (en_body, en_version) == ("English control prompt", "route_en@test")
    assert (zh_body, zh_version) == ("中文控制 prompt", "route_zh@test")


def test_control_prompt_decision_rejects_variant_or_distribution_drift() -> None:
    valid = resolve_control_prompt_locale([_sample("English source", "en-US")])

    with pytest.raises(ValueError, match="variant must be zh only"):
        ControlPromptDecision.model_validate(
            {
                **valid.model_dump(),
                "variant": "zh",
            }
        )
    with pytest.raises(ValueError, match="must be in"):
        ControlPromptDecision.model_validate(
            {
                **valid.model_dump(),
                "distribution": {
                    **valid.distribution,
                    "declared_en": float("nan"),
                },
            }
        )
