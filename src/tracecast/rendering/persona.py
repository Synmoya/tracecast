"""Renderer persona: the replaceable voice / expression layer.

The driver injects one persona config per run. One Host object stays consistent
across Selection and Rendering:

- ``persona_id`` -- stable handle for the host.
- ``viewpoint`` -- how the host *thinks*; primarily a Selection concern, exposed
  to the renderer only as an optional, secondary voice tendency.
- ``expression`` -- how the host writes in the configured output language. This
  is the renderer's main persona input.
- ``viewpoint_profile`` -- the Selection feedback layer (``prefer`` / ``avoid`` /
  ``human_directive``). Accepted here for cross-stage shape
  consistency, but it shapes the *path* upstream, not phrasing, so it is **not**
  projected into the renderer prompt.

Every field is voice-only. None of it is ever a fact source: it cannot add
people, places, events, numbers, causation or reveals.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from tracecast.hosts import (
    HostProfile,
    HostStyleExample,
    SegmentPosition,
    StyleExamplePosition,
)
from tracecast.storage import content_hash


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value)
    return (str(value),)


@dataclass(frozen=True)
class ViewpointProfile:
    """Selection-side feedback layer defined by the Host stage projection.

    Carried for cross-stage shape consistency; the renderer does not project
    ``prefer`` / ``avoid`` / ``human_directive`` into its prompt, because they
    steer path/role/treatment selection upstream rather than phrasing.
    """

    prefer: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    human_directive: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, object] | None) -> ViewpointProfile:
        if not data:
            return cls()
        return cls(
            prefer=_str_tuple(data.get("prefer")),
            avoid=_str_tuple(data.get("avoid")),
            human_directive=_str_tuple(data.get("human_directive")),
        )


@dataclass(frozen=True)
class RenderPersona:
    """A host's voice config, injected by the driver (never read from disk here).

    ``expression`` is the renderer's main input; ``viewpoint`` is an optional
    secondary voice tendency; ``viewpoint_profile`` is accepted but not projected.
    """

    persona_id: str
    viewpoint: str | None = None
    expression: str | None = None
    viewpoint_profile: ViewpointProfile = field(default_factory=ViewpointProfile)
    thinking_rules: tuple[str, ...] = ()
    expression_rules: tuple[str, ...] = ()
    style_examples_by_position: Mapping[
        SegmentPosition,
        tuple[HostStyleExample, ...],
    ] = field(default_factory=dict)
    host_schema_version: str | None = None
    host_version: str | None = None
    host_content_hash: str | None = None

    def style_examples_for(
        self,
        position: SegmentPosition,
        *,
        sample_key: str | None = None,
    ) -> tuple[tuple[StyleExamplePosition | str, HostStyleExample], ...]:
        """Resolve explicit v1 examples or a sampled v2 position projection."""

        if position != "single" or "single" in self.style_examples_by_position:
            examples = self.style_examples_by_position[position]
            is_v1 = (
                self.host_schema_version == "tracecast-host/1"
                or "single" in self.style_examples_by_position
            )
            if not is_v1 and sample_key is not None:
                examples = (_pick_style_example(examples, sample_key, position),)
            return tuple((position, example) for example in examples)
        opening = _pick_style_example(
            self.style_examples_by_position["opening"], sample_key, "opening"
        )
        ending = _pick_style_example(
            self.style_examples_by_position["ending"], sample_key, "ending"
        )
        return (("opening", opening), ("ending", ending))

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> RenderPersona:
        """Build a persona from a plain mapping (the driver's injected config)."""
        persona_id = data.get("persona_id")
        if not isinstance(persona_id, str) or not persona_id:
            raise ValueError("persona config requires a non-empty 'persona_id'")
        schema_version = _opt_str(data.get("schema_version"))
        style_examples = _style_examples_from_mapping(
            _as_mapping(data.get("style_examples_by_position"))
        )
        if schema_version == "tracecast-host/1" or "single" in style_examples:
            invalid = sorted(
                position for position, examples in style_examples.items() if len(examples) != 2
            )
            if invalid:
                raise ValueError(
                    "legacy host style-example positions must contain two entries: "
                    + ", ".join(invalid)
                )
        return cls(
            persona_id=persona_id,
            viewpoint=_opt_str(data.get("viewpoint")),
            expression=_opt_str(data.get("expression")),
            viewpoint_profile=ViewpointProfile.from_mapping(
                _as_mapping(data.get("viewpoint_profile"))
            ),
            thinking_rules=_str_tuple(data.get("thinking_rules")),
            expression_rules=_str_tuple(data.get("expression_rules")),
            style_examples_by_position=style_examples,
            host_schema_version=schema_version,
            host_version=_opt_str(data.get("host_version")),
            host_content_hash=_opt_str(data.get("host_content_hash")),
        )

    @classmethod
    def from_host(cls, host: HostProfile) -> RenderPersona:
        thinking = host.host_thinking
        return cls(
            persona_id=host.host_id,
            viewpoint=thinking.viewpoint,
            expression=host.host_expression.description,
            viewpoint_profile=ViewpointProfile(
                prefer=tuple(thinking.prefer),
                avoid=tuple(thinking.avoid),
                human_directive=tuple(thinking.human_directive),
            ),
            thinking_rules=tuple(thinking.rules),
            expression_rules=tuple(host.host_expression.rules),
            style_examples_by_position=host.style_examples_by_position,
            host_schema_version=host.schema_version,
            host_version=host.version,
            host_content_hash=str(host.snapshot()["content_hash"]),
        )


def _as_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _style_examples_from_mapping(
    data: Mapping[str, object] | None,
) -> dict[SegmentPosition, tuple[HostStyleExample, ...]]:
    if not data:
        return {}
    parsed: dict[SegmentPosition, tuple[HostStyleExample, ...]] = {}
    for position, raw_examples in data.items():
        if position not in {"opening", "middle", "ending", "single"}:
            raise ValueError(f"unknown host style-example position {position!r}")
        if not isinstance(raw_examples, Iterable) or isinstance(raw_examples, str):
            raise ValueError(f"host style examples for {position!r} must be a sequence")
        examples = tuple(HostStyleExample.model_validate(item) for item in raw_examples)
        if not examples:
            raise ValueError(f"host style examples for {position!r} must be non-empty")
        parsed[position] = examples
    return parsed


def _pick_style_example(
    examples: tuple[HostStyleExample, ...],
    sample_key: str | None,
    position: SegmentPosition,
) -> HostStyleExample:
    if sample_key is None or len(examples) == 1:
        return examples[0]
    digest = content_hash({"sample_key": sample_key, "position": position})
    return examples[int(digest[:16], 16) % len(examples)]


def coerce_persona(
    persona: RenderPersona | HostProfile | Mapping[str, object],
) -> RenderPersona:
    """Accept either a :class:`RenderPersona` or its raw mapping form."""
    if isinstance(persona, RenderPersona):
        return persona
    if isinstance(persona, HostProfile):
        return RenderPersona.from_host(persona)
    return RenderPersona.from_mapping(persona)
