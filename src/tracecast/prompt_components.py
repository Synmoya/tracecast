"""Human-auditable prompt component catalogs.

These files are the review surface for model-facing label descriptions that are
injected into route user templates. Code owns loading, validation and rendering;
the prose lives under each stage's ``prompt/<stage>/components/`` so prompt wording can be reviewed
without editing Python constants.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import ContentKind
from tracecast.hosts import SEGMENT_POSITIONS, SegmentPosition
from tracecast.prompt_resources import prompt_component_path

_INGESTION_CONTENT_KINDS = prompt_component_path("ingestion", "content_kinds.zh.json")
_INGESTION_TAGS = prompt_component_path("ingestion", "tags.zh.json")
_PLANNING_PROTOCOL = prompt_component_path("planning", "protocol.zh.json")
_RENDERING_PROTOCOL = prompt_component_path("rendering", "protocol.zh.json")


def _component_path(stage: str, stem: str, locale: ControlPromptLocale | str) -> Path:
    resolved = coerce_control_prompt_locale(locale)
    if resolved is ControlPromptLocale.zh:
        return {
            ("ingestion", "content_kinds"): _INGESTION_CONTENT_KINDS,
            ("ingestion", "tags"): _INGESTION_TAGS,
            ("planning", "protocol"): _PLANNING_PROTOCOL,
            ("rendering", "protocol"): _RENDERING_PROTOCOL,
        }[(stage, stem)]
    return prompt_component_path(stage, f"{stem}.{resolved.value}.json")


@dataclass(frozen=True)
class IngestionContentKindCatalog:
    version: str
    descriptions: dict[ContentKind, str]


@dataclass(frozen=True)
class IngestionTagCatalog:
    version: str
    descriptions: dict[str, str]


@dataclass(frozen=True)
class PlanningProtocolCatalog:
    version: str
    narrative_function_descriptions: tuple[tuple[str, str], ...]
    edge_type_descriptions: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class RenderingStyleExampleProtocol:
    selection: str
    usage: tuple[str, ...]


@dataclass(frozen=True)
class RenderingProtocolCatalog:
    version: str
    dynamic_blocks: tuple[tuple[str, str], ...]
    segment_positions: dict[SegmentPosition, str]
    style_example_protocol: RenderingStyleExampleProtocol


def _read_component(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"prompt component {path} must be a JSON object")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"prompt component {path} is missing a non-empty version")
    return data


@cache
def load_ingestion_content_kind_catalog(
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
) -> IngestionContentKindCatalog:
    path = _component_path("ingestion", "content_kinds", control_prompt_locale)
    data = _read_component(path)
    raw = data.get("content_kind_descriptions")
    if not isinstance(raw, dict):
        raise ValueError(f"{path} needs content_kind_descriptions")
    descriptions: dict[ContentKind, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"content_kind {key!r} needs a non-empty description")
        descriptions[ContentKind(key)] = value
    return IngestionContentKindCatalog(version=data["version"], descriptions=descriptions)


@cache
def load_ingestion_tag_catalog(
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
) -> IngestionTagCatalog:
    path = _component_path("ingestion", "tags", control_prompt_locale)
    data = _read_component(path)
    raw = data.get("tag_descriptions")
    if not isinstance(raw, dict):
        raise ValueError(f"{path} needs tag_descriptions")
    descriptions: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"ingestion tag {key!r} needs a non-empty description")
        descriptions[str(key)] = value
    return IngestionTagCatalog(version=data["version"], descriptions=descriptions)


@cache
def load_planning_protocol_catalog(
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
) -> PlanningProtocolCatalog:
    path = _component_path("planning", "protocol", control_prompt_locale)
    data = _read_component(path)
    narrative = data.get("narrative_function_descriptions")
    edges = data.get("edge_type_descriptions")
    if not isinstance(narrative, dict):
        raise ValueError(f"{path} needs narrative_function_descriptions")
    if not isinstance(edges, dict):
        raise ValueError(f"{path} needs edge_type_descriptions")

    narrative_items: list[tuple[str, str]] = []
    for key, value in narrative.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"narrative_function {key!r} needs a non-empty description")
        narrative_items.append((str(key), value))

    edge_items: list[tuple[str, tuple[str, ...]]] = []
    for key, values in edges.items():
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(item, str) or not item for item in values)
        ):
            raise ValueError(f"edge_type {key!r} needs non-empty description strings")
        edge_items.append((str(key), tuple(values)))

    return PlanningProtocolCatalog(
        version=data["version"],
        narrative_function_descriptions=tuple(narrative_items),
        edge_type_descriptions=tuple(edge_items),
    )


@cache
def load_rendering_protocol_catalog(
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
) -> RenderingProtocolCatalog:
    path = _component_path("rendering", "protocol", control_prompt_locale)
    data = _read_component(path)
    dynamic_blocks = data.get("dynamic_blocks")
    segment_positions = data.get("segment_positions")
    style_example_protocol = data.get("style_example_protocol")
    if not isinstance(dynamic_blocks, dict):
        raise ValueError(f"{path} needs dynamic_blocks")
    _require_exact_keys(
        segment_positions,
        SEGMENT_POSITIONS,
        "segment_positions",
        path=path,
    )
    _require_exact_keys(
        style_example_protocol,
        ("selection", "usage"),
        "style_example_protocol",
        path=path,
    )

    block_items: list[tuple[str, str]] = []
    for key, value in dynamic_blocks.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{path} has an empty dynamic block key")
        if not isinstance(value, str) or not value:
            raise ValueError(f"dynamic block {key!r} needs a non-empty description")
        block_items.append((key, value))

    parsed_positions: dict[SegmentPosition, str] = {}
    for position in SEGMENT_POSITIONS:
        position_description = segment_positions[position]
        if not isinstance(position_description, str) or not position_description:
            raise ValueError(
                f"{path} needs a non-empty segment_positions.{position}"
            )
        parsed_positions[position] = position_description

    selection = style_example_protocol["selection"]
    usage = style_example_protocol["usage"]
    if not isinstance(selection, str) or not selection:
        raise ValueError(
            f"{path} needs style_example_protocol.selection"
        )
    if (
        not isinstance(usage, list)
        or not usage
        or any(not isinstance(item, str) or not item for item in usage)
    ):
        raise ValueError(
            f"{path} needs non-empty style_example_protocol.usage"
        )

    return RenderingProtocolCatalog(
        version=data["version"],
        dynamic_blocks=tuple(block_items),
        segment_positions=parsed_positions,
        style_example_protocol=RenderingStyleExampleProtocol(
            selection=selection,
            usage=tuple(usage),
        ),
    )


def _require_exact_keys(
    value: Any,
    expected: tuple[str, ...],
    label: str,
    *,
    path: Path,
) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{path} needs {label}")
    actual = set(value)
    required = set(expected)
    if actual != required:
        missing = sorted(required - actual)
        extra = sorted(actual - required)
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra: " + ", ".join(extra))
        raise ValueError(
            f"{path} {label} keys must match exactly "
            f"({'; '.join(details)})"
        )
