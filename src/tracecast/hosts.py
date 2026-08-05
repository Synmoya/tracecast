"""Operator-authored, reusable host profiles.

A host is selected as one atomic JSON document.  Selection and Rendering read
different projections from that document, but the storage identity, version and
content hash stay shared so a run cannot silently combine two hosts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tracecast.storage import content_hash

SegmentPosition = Literal["opening", "middle", "ending", "single"]
StyleExamplePosition = Literal["opening", "middle", "ending"]
SEGMENT_POSITIONS: tuple[SegmentPosition, ...] = (
    "opening",
    "middle",
    "ending",
    "single",
)
STYLE_EXAMPLE_POSITIONS: tuple[StyleExamplePosition, ...] = (
    "opening",
    "middle",
    "ending",
)


class _HostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HostThinking(_HostModel):
    """Host reasoning and Selection preferences; never a fact source."""

    viewpoint: str
    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    human_directive: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)

    @field_validator("viewpoint")
    @classmethod
    def _viewpoint_non_empty(cls, value: str) -> str:
        return _required_text(value, "host_thinking.viewpoint")

    @field_validator("prefer", "avoid", "human_directive", "rules")
    @classmethod
    def _thinking_lists_non_empty(cls, value: list[str]) -> list[str]:
        return _clean_text_list(value)


class HostExpression(_HostModel):
    """Renderer-facing voice description and stable expression rules."""

    description: str
    rules: list[str] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _description_non_empty(cls, value: str) -> str:
        return _required_text(value, "host_expression.description")

    @field_validator("rules")
    @classmethod
    def _expression_rules_non_empty(cls, value: list[str]) -> list[str]:
        return _clean_text_list(value)


class HostStyleExample(_HostModel):
    """One transformation demonstration: a plain input and the host's prose.

    ``lesson`` belongs to ``tracecast-host/1`` only.  A v1 profile is an
    operator-authored snapshot whose lesson states the position's normative
    duty.  v2 profiles are compiled from a preference corpus, where a
    per-example lesson can only restate a delta the Renderer already sees in
    full.  ``HostProfile`` enforces which version may carry it.
    """

    source: str
    render: str
    lesson: str | None = None

    @field_validator("source", "render")
    @classmethod
    def _example_text_non_empty(cls, value: str) -> str:
        return _required_text(value, "host style example field")

    @field_validator("lesson")
    @classmethod
    def _lesson_non_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _required_text(value, "host style example lesson")


class HostProfile(_HostModel):
    """One reusable host, loaded from ``hosts/<host-id>.json``."""

    schema_version: Literal["tracecast-host/1", "tracecast-host/2"] = "tracecast-host/1"
    host_id: str
    display_name: str
    version: str
    output_language: str
    host_thinking: HostThinking
    host_expression: HostExpression
    style_examples_by_position: dict[
        SegmentPosition,
        tuple[HostStyleExample, ...],
    ]

    @field_validator("host_id", "display_name", "version", "output_language")
    @classmethod
    def _non_empty_scalar(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host scalar fields must be non-empty")
        return value

    @model_validator(mode="after")
    def _all_positions_present(self) -> HostProfile:
        actual = set(self.style_examples_by_position)
        expected = (
            set(SEGMENT_POSITIONS)
            if self.schema_version == "tracecast-host/1"
            else set(STYLE_EXAMPLE_POSITIONS)
        )
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            details: list[str] = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if extra:
                details.append("extra: " + ", ".join(extra))
            raise ValueError(
                "style_examples_by_position must contain exactly "
                + ", ".join(sorted(expected))
                + f" ({'; '.join(details)})"
            )
        counts = {
            position: len(examples)
            for position, examples in self.style_examples_by_position.items()
        }
        if self.schema_version == "tracecast-host/1":
            invalid = sorted(position for position, count in counts.items() if count != 2)
            if invalid:
                raise ValueError(
                    "tracecast-host/1 style-example positions must contain two entries: "
                    + ", ".join(invalid)
                )
        else:
            empty = sorted(position for position, count in counts.items() if count < 1)
            if empty:
                raise ValueError(
                    "tracecast-host/2 style-example positions must be non-empty: "
                    + ", ".join(empty)
                )
        self._check_lesson_ownership()
        return self

    def _check_lesson_ownership(self) -> None:
        """Reject a lesson on the version that does not own the field."""

        v1 = self.schema_version == "tracecast-host/1"
        offenders = sorted(
            position
            for position, examples in self.style_examples_by_position.items()
            if any((example.lesson is None) is v1 for example in examples)
        )
        if not offenders:
            return
        if v1:
            raise ValueError(
                "tracecast-host/1 style examples must carry a non-empty lesson: "
                + ", ".join(offenders)
            )
        raise ValueError(
            "tracecast-host/2 no longer accepts a style-example lesson "
            "(remove the field or rebuild the host): " + ", ".join(offenders)
        )

    def style_examples_for(
        self,
        position: SegmentPosition,
        *,
        sample_key: str | None = None,
    ) -> tuple[tuple[StyleExamplePosition | Literal["single"], HostStyleExample], ...]:
        """Resolve Renderer examples while preserving Host v1 snapshots.

        Host v1 owns explicit ``single`` examples. Host v2 deliberately stores
        only opening/middle/ending. With a sample key, v2 chooses one stable
        example from the current position pool; a single-chunk render chooses
        one opening and one ending example so the prompt demonstrates both
        boundary rights. Omitting the key keeps the full pool for legacy tooling.
        """

        if position != "single" or "single" in self.style_examples_by_position:
            examples = self.style_examples_by_position[position]
            if self.schema_version == "tracecast-host/2" and sample_key is not None:
                examples = (_pick_style_example(examples, sample_key, position),)
            return tuple((position, example) for example in examples)
        opening = _pick_style_example(
            self.style_examples_by_position["opening"], sample_key, "opening"
        )
        ending = _pick_style_example(
            self.style_examples_by_position["ending"], sample_key, "ending"
        )
        return (("opening", opening), ("ending", ending))

    def selection_persona(self) -> dict[str, Any]:
        """Compatibility projection consumed by the existing Selection boundary."""

        thinking = self.host_thinking
        return {
            "persona_id": self.host_id,
            "viewpoint": thinking.viewpoint,
            "expression": self.host_expression.description,
            "viewpoint_profile": {
                "prefer": list(thinking.prefer),
                "avoid": list(thinking.avoid),
                "human_directive": list(thinking.human_directive),
            },
            "thinking_rules": list(thinking.rules),
            "host_version": self.version,
            "host_content_hash": self.snapshot()["content_hash"],
        }

    def snapshot(self) -> dict[str, Any]:
        """Canonical run-manifest snapshot, including a whole-profile hash."""

        data = self.model_dump(mode="json")
        return {
            "host_id": self.host_id,
            "version": self.version,
            "content_hash": content_hash(data),
            "profile": data,
        }


def load_host(hosts_dir: str | Path, host_id: str) -> HostProfile:
    """Load and validate ``hosts/<host-id>.json`` without path traversal."""

    path = host_path(hosts_dir, host_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    host = HostProfile.model_validate(data)
    if host.host_id != host_id:
        raise ValueError(
            f"host file {path} declares host_id {host.host_id!r}, expected {host_id!r}"
        )
    return host


def host_path(hosts_dir: str | Path, host_id: str) -> Path:
    """Return the safe catalog path for ``host_id`` without touching disk."""

    if not host_id or "/" in host_id or "\\" in host_id or host_id in {".", ".."}:
        raise ValueError(f"unsafe host id: {host_id!r}")
    return Path(hosts_dir) / f"{host_id}.json"


def list_hosts(hosts_dir: str | Path) -> list[HostProfile]:
    """Load every valid host file in stable filename order.

    Invalid profiles fail loudly: a partially broken host catalog must not let a
    run picker present a silently incomplete set.
    """

    root = Path(hosts_dir)
    if not root.is_dir():
        return []
    hosts: list[HostProfile] = []
    seen: set[str] = set()
    for path in sorted(root.glob("*.json")):
        host = load_host(root, path.stem)
        if host.host_id in seen:
            raise ValueError(f"duplicate host_id {host.host_id!r}")
        seen.add(host.host_id)
        hosts.append(host)
    return hosts


def require_host_language(host: HostProfile, output_language: str) -> None:
    """Reject a language-specific host used for a different output language."""

    if host.output_language != output_language:
        raise ValueError(
            f"host {host.host_id!r} targets {host.output_language!r}, "
            f"project targets {output_language!r}"
        )


def _required_text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _clean_text_list(values: list[str]) -> list[str]:
    return [_required_text(value, "host list entry") for value in values]


def _pick_style_example(
    examples: tuple[HostStyleExample, ...],
    sample_key: str | None,
    position: StyleExamplePosition,
) -> HostStyleExample:
    if sample_key is None or len(examples) == 1:
        return examples[0]
    digest = content_hash({"sample_key": sample_key, "position": position})
    return examples[int(digest[:16], 16) % len(examples)]


__all__ = [
    "HostExpression",
    "HostProfile",
    "HostStyleExample",
    "HostThinking",
    "SEGMENT_POSITIONS",
    "STYLE_EXAMPLE_POSITIONS",
    "SegmentPosition",
    "StyleExamplePosition",
    "host_path",
    "list_hosts",
    "load_host",
    "require_host_language",
]
