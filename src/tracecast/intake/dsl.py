"""Prompt projection and parser for the sampled source-profile probe DSL."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tracecast.domain import estimate_tokens

from .sampling import SourceProbe

IntakeStatus = Literal["ready", "needs_review"]
KnownLanguage = Literal["en", "zh", "unknown"]


class IntakeParseError(ValueError):
    """Raised when the source-profile probe DSL cannot be compiled."""


@dataclass(frozen=True)
class SourceProfileDecision:
    """One compiled ``SRC ... ENDSRC`` decision from the intake DSL."""

    source_alias: str
    profile: str
    language: KnownLanguage
    status: IntakeStatus
    title: str
    note: str
    reason: str


def render_profile_catalog(profile_templates: dict) -> str:
    """Render ``source_profile_templates`` into compact model-facing text."""
    profiles = profile_templates.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("source profile templates must contain a non-empty 'profiles' object")
    lines: list[str] = []
    for key, value in profiles.items():
        if not isinstance(value, dict):
            raise ValueError(f"profile {key!r} must be an object")
        description = str(value.get("description", "")).strip()
        source_class = str(value.get("default_source_class", "")).strip()
        trust_tier = str(value.get("default_trust_tier", "")).strip()
        kinds = ", ".join(str(item) for item in value.get("allowed_content_kinds", ()))
        tags = ", ".join(str(item) for item in value.get("allowed_tags", ()))
        lines.append(
            f"PROFILE {key} | class={source_class} | trust={trust_tier}\n"
            f"description: {description}\n"
            f"content_kinds: {kinds}\n"
            f"tags: {tags}"
        )
    return "\n\n".join(lines)


def render_probe_user_prompt(
    *,
    template: str,
    profile_templates: dict,
    probes: list[SourceProbe],
) -> str:
    """Fill the source-profile probe user template."""
    replacements = {
        "profile_catalog": render_profile_catalog(profile_templates),
        "source_probe_blocks": render_source_probe_blocks(probes),
    }
    rendered = template
    for key, value in replacements.items():
        placeholder = "{{" + key + "}}"
        if placeholder not in rendered:
            raise ValueError(f"source-profile probe template is missing {placeholder}")
        rendered = rendered.replace(placeholder, value)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("source-profile probe template contains unknown placeholder(s)")
    return rendered.strip()


def render_source_probe_blocks(probes: list[SourceProbe]) -> str:
    """Render sampled raw-source probes for the LLM call."""
    blocks: list[str] = []
    for probe in probes:
        stat_line = " | ".join(f"{key}={value}" for key, value in probe.stats.items())
        lines = [
            (
                f"SRC {probe.source_alias} | path={probe.path} "
                f"| source_id_guess={probe.source_id_guess} "
                f"| chars={probe.char_count} | lines={probe.line_count} "
                f"| est_tokens={probe.estimated_tokens}"
            ),
            f"STRUCT {stat_line}",
        ]
        for sample in probe.samples:
            lines.append(
                f"SAMPLE {sample.label} | piece_order={sample.order_index} "
                f"| est_tokens={estimate_tokens(sample.text)}"
            )
            lines.append(sample.text)
            lines.append("ENDSAMPLE")
        lines.append("ENDSRC")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse_probe_response(
    text: str,
    *,
    known_profiles: set[str],
    expected_aliases: set[str],
) -> list[SourceProfileDecision]:
    """Parse source-profile probe DSL into decisions."""
    lines = _strip_fences(text.splitlines())
    index = 0
    decisions: list[SourceProfileDecision] = []
    seen_aliases: set[str] = set()
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if not line.startswith("SRC "):
            raise IntakeParseError(f"expected SRC line, got {line!r}")
        header = _parse_header(
            line,
            known_profiles=known_profiles,
            expected_aliases=expected_aliases,
        )
        index += 1
        fields: dict[str, str] = {"TITLE": "", "NOTE": "", "REASON": ""}
        closed = False
        while index < len(lines):
            body = lines[index].strip()
            index += 1
            if not body:
                continue
            if body == "ENDSRC":
                closed = True
                break
            if "|" in body:
                raise IntakeParseError(f"free-text field may not contain '|': {body!r}")
            key, value = _parse_body_line(body)
            if key not in fields:
                raise IntakeParseError(f"unknown source-profile probe field {key!r}")
            fields[key] = value
        if not closed:
            raise IntakeParseError(f"SRC {header['alias']} is missing ENDSRC")
        alias = header["alias"]
        if alias in seen_aliases:
            raise IntakeParseError(f"duplicate source alias {alias!r}")
        seen_aliases.add(alias)
        decisions.append(
            SourceProfileDecision(
                source_alias=alias,
                profile=header["profile"],
                language=header["language"],  # type: ignore[arg-type]
                status=header["status"],  # type: ignore[arg-type]
                title=fields["TITLE"],
                note=fields["NOTE"],
                reason=fields["REASON"],
            )
        )
    missing = sorted(expected_aliases - seen_aliases)
    if missing:
        raise IntakeParseError("missing source-profile decision(s): " + ", ".join(missing))
    return decisions


def _parse_header(
    line: str,
    *,
    known_profiles: set[str],
    expected_aliases: set[str],
) -> dict[str, str]:
    parts = [part.strip() for part in line.split("|")]
    head = parts[0].split()
    if len(head) != 2 or head[0] != "SRC":
        raise IntakeParseError(f"malformed SRC header {line!r}")
    alias = head[1]
    if alias not in expected_aliases:
        raise IntakeParseError(f"unknown source alias {alias!r}")
    values: dict[str, str] = {"alias": alias}
    for part in parts[1:]:
        if "=" not in part:
            raise IntakeParseError(f"malformed SRC header field {part!r}")
        key, value = [item.strip() for item in part.split("=", 1)]
        values[key] = value
    profile = values.get("profile")
    language = values.get("language")
    status = values.get("status")
    if profile is None or (profile != "unknown" and profile not in known_profiles):
        raise IntakeParseError(f"profile {profile!r} is not allowed")
    if language not in {"en", "zh", "unknown"}:
        raise IntakeParseError(f"language {language!r} is not allowed")
    if status not in {"ready", "needs_review"}:
        raise IntakeParseError(f"status {status!r} is not allowed")
    if status == "ready" and (profile == "unknown" or language == "unknown"):
        raise IntakeParseError("status=ready requires non-unknown profile and language")
    return values


def _parse_body_line(line: str) -> tuple[str, str]:
    for key in ("TITLE", "NOTE", "REASON"):
        prefix = key + " "
        if line.startswith(prefix):
            return key, line[len(prefix) :].strip()
    raise IntakeParseError(f"malformed body line {line!r}")


def _strip_fences(lines: list[str]) -> list[str]:
    stripped: list[str] = []
    for line in lines:
        if line.strip().startswith("```"):
            continue
        stripped.append(line)
    return stripped
