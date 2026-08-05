"""Classify source language evidence and choose a control-prompt language.

The decision is deliberately independent from the requested output language.
Source classification remains auditable, while the actual prompt asset uses
one of two supported languages: Chinese for confidently Chinese material and
English for every other classification.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from enum import StrEnum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTROL_PROMPT_POLICY_VERSION = "control_prompt_locale@v2"
CONTROL_PROMPT_LOCALE_THRESHOLD = 0.80
CONTROL_PROMPT_DISTRIBUTION_KEYS = frozenset(
    {
        "declared_zh",
        "declared_en",
        "declared_other",
        "declared_missing",
        "script_cjk",
        "script_latin",
    }
)


class ControlPromptLocale(StrEnum):
    """Available, semantically equivalent control-prompt asset languages."""

    zh = "zh"
    en = "en"


class SourceLanguageClass(StrEnum):
    """Auditable classification of the corpus used to choose a variant."""

    zh = "zh"
    en = "en"
    mixed = "mixed"
    other = "other"
    unknown = "unknown"


class ControlPromptSample(BaseModel):
    """One corpus member used by the locale resolver."""

    model_config = ConfigDict(frozen=True)

    text: str
    language_tag: str | None = None


class ControlPromptDecision(BaseModel):
    """Auditable root decision recorded outside fact artifacts."""

    model_config = ConfigDict(frozen=True)

    source_language: SourceLanguageClass
    confidence: float = Field(ge=0.0, le=1.0)
    distribution: dict[str, float]
    policy_version: str = CONTROL_PROMPT_POLICY_VERSION
    variant: ControlPromptLocale

    @model_validator(mode="before")
    @classmethod
    def _read_legacy_locale(cls, value: object) -> object:
        """Read v1 manifests without preserving the removed neutral variant."""

        if not isinstance(value, dict) or "source_language" in value:
            return value
        upgraded = dict(value)
        legacy_locale = str(upgraded.pop("locale", "unknown"))
        if legacy_locale in {"zh", "en"}:
            upgraded["source_language"] = legacy_locale
        else:
            upgraded["source_language"] = SourceLanguageClass.unknown.value
        upgraded["variant"] = coerce_control_prompt_locale(
            upgraded.get("variant", legacy_locale)
        ).value
        return upgraded

    @field_validator("distribution")
    @classmethod
    def _validate_distribution(cls, value: dict[str, float]) -> dict[str, float]:
        actual = set(value)
        if actual != CONTROL_PROMPT_DISTRIBUTION_KEYS:
            raise ValueError(
                "control prompt distribution keys must match policy schema "
                f"(missing={sorted(CONTROL_PROMPT_DISTRIBUTION_KEYS - actual)}, "
                f"extra={sorted(actual - CONTROL_PROMPT_DISTRIBUTION_KEYS)})"
            )
        for key, share in value.items():
            if not isfinite(share) or not 0.0 <= share <= 1.0:
                raise ValueError(f"control prompt distribution {key!r} must be in [0, 1]")
        return value

    @model_validator(mode="after")
    def _variant_matches_source_policy(self) -> ControlPromptDecision:
        expected = (
            ControlPromptLocale.zh
            if self.source_language is SourceLanguageClass.zh
            else ControlPromptLocale.en
        )
        if self.variant is not expected:
            raise ValueError(
                "control prompt variant must be zh only for confidently Chinese "
                "source material and en otherwise"
            )
        return self


def resolve_control_prompt_locale(
    samples: Sequence[ControlPromptSample],
    *,
    threshold: float = CONTROL_PROMPT_LOCALE_THRESHOLD,
) -> ControlPromptDecision:
    """Classify a corpus and select one ``zh``/``en`` prompt variant.

    Source declarations are weighted by each sample's non-whitespace character
    count.  A Chinese or English declaration must reach ``threshold`` and the
    observed CJK/Latin script distribution must independently agree at the same
    threshold.  Missing, mixed, conflicting and other-language corpora retain a
    distinct audit classification while all selecting the English variant.
    """

    if not 0.5 <= threshold <= 1.0:
        raise ValueError("control prompt locale threshold must be between 0.5 and 1.0")

    declared = {"zh": 0, "en": 0, "other": 0, "missing": 0}
    scripts = {"cjk": 0, "latin": 0}
    total_chars = 0
    for raw_sample in samples:
        sample = ControlPromptSample.model_validate(raw_sample)
        weight = sum(1 for char in sample.text if not char.isspace())
        if weight <= 0:
            continue
        total_chars += weight
        declared[_language_family(sample.language_tag)] += weight
        for char in sample.text:
            family = _script_family(char)
            if family is not None:
                scripts[family] += 1

    declared_distribution = _shares(declared, total_chars)
    script_total = scripts["cjk"] + scripts["latin"]
    script_distribution = _shares(scripts, script_total)
    distribution = {
        "declared_zh": declared_distribution["zh"],
        "declared_en": declared_distribution["en"],
        "declared_other": declared_distribution["other"],
        "declared_missing": declared_distribution["missing"],
        "script_cjk": script_distribution["cjk"],
        "script_latin": script_distribution["latin"],
    }

    source_language = SourceLanguageClass.unknown
    confidence = 1.0
    if total_chars > 0 and script_total > 0:
        zh_evidence = min(distribution["declared_zh"], distribution["script_cjk"])
        en_evidence = min(distribution["declared_en"], distribution["script_latin"])
        if zh_evidence >= threshold:
            source_language = SourceLanguageClass.zh
            confidence = zh_evidence
        elif en_evidence >= threshold:
            source_language = SourceLanguageClass.en
            confidence = en_evidence
        elif distribution["declared_other"] >= threshold:
            source_language = SourceLanguageClass.other
            confidence = distribution["declared_other"]
        elif distribution["declared_missing"] >= threshold:
            source_language = SourceLanguageClass.unknown
            confidence = distribution["declared_missing"]
        else:
            source_language = SourceLanguageClass.mixed
            confidence = 1.0 - max(
                zh_evidence,
                en_evidence,
                distribution["declared_other"],
                distribution["declared_missing"],
            )
    elif total_chars > 0 and distribution["declared_other"] >= threshold:
        source_language = SourceLanguageClass.other
        confidence = distribution["declared_other"]

    variant = (
        ControlPromptLocale.zh
        if source_language is SourceLanguageClass.zh
        else ControlPromptLocale.en
    )
    decision = ControlPromptDecision(
        source_language=source_language,
        confidence=round(confidence, 6),
        distribution={key: round(value, 6) for key, value in distribution.items()},
        variant=variant,
    )
    return decision


def coerce_control_prompt_locale(value: ControlPromptLocale | str) -> ControlPromptLocale:
    """Normalize a prompt asset language, mapping legacy neutral to English."""

    if str(value) == "neutral":
        return ControlPromptLocale.en
    return ControlPromptLocale(value)


def control_prompt_locale_from_record(
    decision: Mapping[str, object] | None,
    *,
    default: ControlPromptLocale = ControlPromptLocale.zh,
) -> ControlPromptLocale:
    """Read the selected asset language from current or v1 manifest data."""

    if not decision:
        return default
    raw = decision.get("variant", decision.get("locale", default.value))
    return coerce_control_prompt_locale(str(raw))


def _language_family(language_tag: str | None) -> str:
    if language_tag is None or not language_tag.strip():
        return "missing"
    primary = language_tag.strip().replace("_", "-").split("-", 1)[0].casefold()
    if primary == "zh":
        return "zh"
    if primary == "en":
        return "en"
    return "other"


def _script_family(char: str) -> str | None:
    codepoint = ord(char)
    if (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    ):
        return "cjk"
    if "LATIN" in unicodedata.name(char, ""):
        return "latin"
    return None


def _shares(counts: dict[str, int], total: int) -> dict[str, float]:
    if total <= 0:
        return {key: 0.0 for key in counts}
    return {key: value / total for key, value in counts.items()}


__all__ = [
    "CONTROL_PROMPT_LOCALE_THRESHOLD",
    "CONTROL_PROMPT_POLICY_VERSION",
    "CONTROL_PROMPT_DISTRIBUTION_KEYS",
    "ControlPromptDecision",
    "ControlPromptLocale",
    "ControlPromptSample",
    "SourceLanguageClass",
    "coerce_control_prompt_locale",
    "control_prompt_locale_from_record",
    "resolve_control_prompt_locale",
]
