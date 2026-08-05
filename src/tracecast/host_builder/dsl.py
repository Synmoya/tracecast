"""Prompt projection and strict DSL parsing for Host Builder calls."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from tracecast.hosts import STYLE_EXAMPLE_POSITIONS, HostStyleExample, StyleExamplePosition
from tracecast.prompts import substitute_prompt_blocks

from .models import DistilledHostProfile
from .sampling import PreferencePiece


class HostBuildParseError(ValueError):
    """A Host Builder response cannot be compiled without guessing."""


def render_profile_user_prompt(
    *,
    template: str,
    samples: Iterable[tuple[str, StyleExamplePosition, PreferencePiece]],
) -> str:
    rows: list[str] = []
    for alias, position, piece in samples:
        rows.extend(
            (
                f"SAMPLE {alias} | position={position}",
                piece.text,
                "ENDSAMPLE",
            )
        )
    return substitute_prompt_blocks(
        template,
        {"samples": "\n".join(rows)},
        prompt_name="host profile distill",
    )


def render_examples_user_prompt(
    *,
    template: str,
    samples: Iterable[tuple[str, StyleExamplePosition, PreferencePiece]],
) -> str:
    rows: list[str] = []
    for alias, position, piece in samples:
        rows.extend(
            (
                f"EXAMPLE_INPUT {alias} | position={position}",
                piece.text,
                "ENDEXAMPLE_INPUT",
            )
        )
    return substitute_prompt_blocks(
        template,
        {"examples": "\n".join(rows)},
        prompt_name="host style example reverse",
    )


def parse_profile_response(text: str) -> DistilledHostProfile:
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {
        "PREFER": [],
        "AVOID": [],
        "THINKING_RULE": [],
        "EXPRESSION": [],
        "EXPRESSION_RULE": [],
    }
    scalar_fields = {"OUTPUT_LANGUAGE", "VIEWPOINT"}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        key, separator, value = line.partition(" ")
        value = value.strip()
        if not separator or not value:
            raise HostBuildParseError(f"invalid profile line {line_number}")
        if key in scalar_fields:
            if key in scalars:
                raise HostBuildParseError(f"duplicate profile field {key}")
            scalars[key] = value
        elif key in lists:
            lists[key].append(value)
        else:
            raise HostBuildParseError(f"unknown profile field {key!r}")
    missing = sorted(scalar_fields - set(scalars))
    if not lists["EXPRESSION"]:
        missing.append("EXPRESSION")
    if missing:
        raise HostBuildParseError("missing profile fields: " + ", ".join(missing))
    return DistilledHostProfile(
        output_language=scalars["OUTPUT_LANGUAGE"],
        viewpoint=scalars["VIEWPOINT"],
        prefer=lists["PREFER"],
        avoid=lists["AVOID"],
        thinking_rules=lists["THINKING_RULE"],
        expression_description=" ".join(lists["EXPRESSION"]),
        expression_rules=lists["EXPRESSION_RULE"],
    )


def parse_examples_response(
    text: str,
    *,
    expected: Mapping[str, tuple[StyleExamplePosition, PreferencePiece]],
) -> dict[str, HostStyleExample]:
    blocks: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if not line.startswith("EXAMPLE "):
            raise HostBuildParseError(
                f"unexpected example output at line {index + 1}"
            )
        alias = line.removeprefix("EXAMPLE ").strip()
        if not alias or " " in alias:
            raise HostBuildParseError(f"invalid example alias {alias!r}")
        if alias in blocks:
            raise HostBuildParseError(f"duplicate example alias {alias!r}")
        index += 1
        source, index = _parse_multiline_field(lines, index, "SOURCE", "ENDSOURCE")
        if index >= len(lines) or lines[index].strip() != "ENDEXAMPLE":
            raise HostBuildParseError(f"example {alias!r} is missing ENDEXAMPLE")
        blocks[alias] = source
        index += 1

    actual = set(blocks)
    expected_aliases = set(expected)
    if actual != expected_aliases:
        missing = sorted(expected_aliases - actual)
        extra = sorted(actual - expected_aliases)
        details: list[str] = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unknown: " + ", ".join(extra))
        raise HostBuildParseError("example alias coverage mismatch (" + "; ".join(details) + ")")

    compiled: dict[str, HostStyleExample] = {}
    for alias, source in blocks.items():
        _position, piece = expected[alias]
        if source.strip() == piece.text.strip():
            raise HostBuildParseError(f"example {alias!r} source equals render")
        compiled[alias] = HostStyleExample(source=source, render=piece.text)
    return compiled


def selected_example_rows(
    selected: Mapping[StyleExamplePosition, tuple[PreferencePiece, ...]],
) -> list[tuple[str, StyleExamplePosition, PreferencePiece]]:
    rows: list[tuple[str, StyleExamplePosition, PreferencePiece]] = []
    number = 1
    for position in STYLE_EXAMPLE_POSITIONS:
        for piece in selected[position]:
            rows.append((f"E{number:03d}", position, piece))
            number += 1
    return rows


def _parse_multiline_field(
    lines: list[str], index: int, opener: str, closer: str
) -> tuple[str, int]:
    if index >= len(lines) or lines[index].strip() != opener:
        raise HostBuildParseError(f"expected {opener}")
    index += 1
    values: list[str] = []
    while index < len(lines) and lines[index].strip() != closer:
        values.append(lines[index])
        index += 1
    if index >= len(lines):
        raise HostBuildParseError(f"missing {closer}")
    value = "\n".join(values).strip()
    if not value:
        raise HostBuildParseError(f"{opener} must be non-empty")
    return value, index + 1


__all__ = [
    "HostBuildParseError",
    "parse_examples_response",
    "parse_profile_response",
    "render_examples_user_prompt",
    "render_profile_user_prompt",
    "selected_example_rows",
]
