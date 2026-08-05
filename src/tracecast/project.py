"""Operator-authored project configuration.

The target output language is an explicit run semantic. Optional name overrides
seed or override automatic resolution; they are never a required review gate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracecast.domain import AuditReportItem, NameOverride, issue_id
from tracecast.language import require_bcp47_language_tag
from tracecast.name_resolution import (
    DEFAULT_NAME_RESOLUTION_MODE,
    NameResolutionMode,
    coerce_name_resolution_mode,
    stable_name_script_mismatch,
)

_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def validate_project_id(value: str) -> str:
    """Validate the stable, path-safe id used by project creation boundaries."""

    if not isinstance(value, str) or value != value.strip():
        raise ValueError("project_id must be a trimmed string")
    if not _PROJECT_ID_RE.fullmatch(value):
        raise ValueError(
            "project_id must be 1-64 lowercase ASCII letters, digits, '_' or '-', "
            "starting with a letter or digit"
        )
    if value in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"project_id {value!r} is reserved by the local filesystem")
    return value


@dataclass(frozen=True)
class SourceSpec:
    """One entry of ``projects/<id>/sources.json``."""

    source_id: str
    path: str
    profile: str
    language: str
    source_type: str | None = None
    source_class: str | None = None
    trust_tier: str | None = None
    title: str | None = None
    note: str | None = None
    content_hash: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProjectConfig:
    """A resolved project and its output-language presentation settings."""

    project_id: str
    output_language: str
    name_resolution_mode: NameResolutionMode
    sources: list[SourceSpec]
    profile_templates: dict[str, Any]
    name_overrides: list[NameOverride]


def load_project(project_dir: str | Path, profiles_path: str | Path) -> ProjectConfig:
    """Load and validate the material project declaration."""

    project_dir = Path(project_dir)
    spec = json.loads((project_dir / "sources.json").read_text(encoding="utf-8"))
    output_language = spec.get("output_language")
    if not isinstance(output_language, str) or not output_language.strip():
        raise ValueError("project output_language must be an explicit non-empty string")
    require_bcp47_language_tag(output_language, field="project output_language")
    if output_language.casefold() in {"und", "unknown"}:
        raise ValueError("project output_language must identify a concrete target language")
    profile_templates = json.loads(Path(profiles_path).read_text(encoding="utf-8"))
    sources = [
        SourceSpec(
            source_id=source["source_id"],
            path=source["path"],
            profile=source["profile"],
            language=source["language"],
            source_type=source.get("source_type"),
            source_class=source.get("source_class"),
            trust_tier=source.get("trust_tier"),
            title=source.get("title"),
            note=source.get("note"),
            content_hash=source.get("content_hash"),
            metadata=dict(source.get("metadata") or {}),
        )
        for source in spec["sources"]
    ]
    return ProjectConfig(
        project_id=spec["project_id"],
        output_language=output_language,
        name_resolution_mode=_load_name_resolution_mode(spec),
        sources=sources,
        profile_templates=profile_templates,
        name_overrides=_load_name_overrides(spec, project_dir),
    )


def _load_name_resolution_mode(spec: dict[str, Any]) -> NameResolutionMode:
    raw_mode = spec.get("name_resolution_mode", DEFAULT_NAME_RESOLUTION_MODE)
    try:
        return coerce_name_resolution_mode(raw_mode)
    except (TypeError, ValueError) as exc:
        allowed = ", ".join(mode.value for mode in NameResolutionMode)
        raise ValueError(
            f"project name_resolution_mode must be one of: {allowed}"
        ) from exc


def _load_name_overrides(
    spec: dict[str, Any],
    project_dir: Path,
) -> list[NameOverride]:
    raw_spec = spec.get("name_overrides_path", spec.get("name_overrides", ()))
    if raw_spec in (None, ""):
        return []
    if isinstance(raw_spec, str):
        raw_data = json.loads((project_dir / raw_spec).read_text(encoding="utf-8"))
    else:
        raw_data = raw_spec
    if isinstance(raw_data, dict):
        raw_data = raw_data.get("entries", ())
    if not isinstance(raw_data, list | tuple):
        raise TypeError(
            "project name_overrides must be a list or an object with an 'entries' list"
        )
    overrides = [NameOverride.model_validate(item) for item in raw_data]
    _validate_override_conflicts(overrides)
    return overrides


def _validate_override_conflicts(overrides: list[NameOverride]) -> None:
    targets: dict[tuple[str, str], set[str]] = {}
    for override in overrides:
        for alias in override.aliases:
            targets.setdefault((override.output_language, alias), set()).add(
                override.target_surface
            )
    conflicts = [
        f"{language}:{alias}"
        for (language, alias), surfaces in targets.items()
        if len(surfaces) > 1
    ]
    if conflicts:
        raise ValueError(
            "project name overrides conflict for alias(es): " + ", ".join(conflicts)
        )


def override_script_mismatch_reports(
    overrides: Sequence[NameOverride],
) -> list[AuditReportItem]:
    """Report (never block) every override whose ``target_surface`` doesn't look
    like it's actually in its own declared ``output_language``'s script.

    This runs independently of whether any given run's materials happen to
    reference the override's alias at all -- unlike the equivalent per-run
    check on matched ``NameResolutionPlan`` rules, which
    only ever sees overrides that were actually matched into this run. An
    override can otherwise sit unflagged in project config indefinitely, until
    the first run that happens to use it bakes the bad value straight into
    that run's script.
    """

    reports: list[AuditReportItem] = []
    for override in overrides:
        if not stable_name_script_mismatch(
            override.target_surface, override.output_language
        ):
            continue
        message = (
            f"name override target_surface '{override.target_surface}' for "
            f"alias(es) {', '.join(override.aliases)} does not look like "
            f"{override.output_language} script"
        )
        reports.append(
            AuditReportItem(
                issue_id=issue_id(
                    "name_resolution", "name_override_script_mismatch", None, message
                ),
                stage="name_resolution",
                kind="name_override_script_mismatch",
                level="warning",
                message=message,
                usable=True,
                artifact_ref=f"override:{override.output_language}:{override.aliases[0]}",
                metadata={
                    "aliases": list(override.aliases),
                    "target_surface": override.target_surface,
                    "output_language": override.output_language,
                },
            )
        )
    return reports


__all__ = [
    "ProjectConfig",
    "SourceSpec",
    "load_project",
    "override_script_mismatch_reports",
]
