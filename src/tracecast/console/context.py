"""Shared console context: paths, job registry, the injectable provider seam.

``call`` mirrors the pipeline's seam (default ``llm.call_llm``); tests inject a
fake to exercise ingestion/derive/chunklab/Host Builder without any spend. Config is
re-loaded from ``config_path`` per use so edits to ``configs/runtime.json`` apply live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tracecast.domain import NameOverride
from tracecast.name_resolution import NameResolutionMode, coerce_name_resolution_mode
from tracecast.project import load_project
from tracecast.storage import RuntimeConfig

from .errors import BadRequest, SetupRequired
from .jobs import JobRegistry


@dataclass
class ConsoleContext:
    """Everything the console services need, wired once by ``create_app``."""

    runs_root: Path
    projects_dir: Path
    hosts_dir: Path
    config_path: Path
    profiles_path: Path
    call: Any  # LlmCall seam, default tracecast.llm.call_llm
    env_path: Path | None = None
    registry: JobRegistry = field(default_factory=JobRegistry)
    project_drafts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.env_path is None:
            self.env_path = (
                self.config_path.parent.parent / ".env"
                if self.config_path.parent.name == "configs"
                else self.config_path.parent / ".env"
            )

    def load_config(self) -> RuntimeConfig:
        if not self.config_path.is_file():
            raise SetupRequired("runtime configuration is missing; complete Console setup first")
        try:
            return RuntimeConfig.load(self.config_path, env_file=self.env_path)
        except (OSError, TypeError, ValueError) as exc:
            raise SetupRequired(
                "runtime configuration is invalid; repair it in Console setup"
            ) from exc


@dataclass(frozen=True)
class ProjectContext:
    """Per-project inputs a rerun resolves from ``projects/<id>/``."""

    project_id: str
    output_language: str
    name_resolution_mode: NameResolutionMode
    name_overrides: list[NameOverride]


def load_project_context(ctx: ConsoleContext, project_id: str) -> ProjectContext:
    """Resolve project-owned language and name-planning inputs."""

    try:
        project = load_project(Path(ctx.projects_dir) / project_id, ctx.profiles_path)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot load project {project_id!r}: {exc}") from exc
    return ProjectContext(
        project_id=project.project_id,
        output_language=project.output_language,
        name_resolution_mode=project.name_resolution_mode,
        name_overrides=list(project.name_overrides),
    )


def resolve_lineage_effective(
    ctx: ConsoleContext,
    start_run_id: str,
    keys: set[str] | None = None,
) -> dict[str, Any]:
    """Resolve nearest recorded effective fields through Console lineage."""

    from .inventory import open_run, read_console_manifest

    wanted = keys or {
        "output_language",
        "name_resolution_mode",
        "control_prompt_decision",
    }
    resolved: dict[str, Any] = {}
    current: str | None = start_run_id
    seen: set[str] = set()
    while current is not None and wanted - set(resolved):
        if current in seen:
            raise BadRequest(f"cycle in console run lineage at {current!r}")
        seen.add(current)
        manifest = read_console_manifest(open_run(ctx.runs_root, current)) or {}
        effective = manifest.get("effective") or {}
        for key in wanted - set(resolved):
            if effective.get(key) is not None:
                value = effective[key]
                if key == "name_resolution_mode":
                    try:
                        value = coerce_name_resolution_mode(value).value
                    except (TypeError, ValueError) as exc:
                        raise BadRequest(
                            f"run {current!r} has invalid name_resolution_mode"
                        ) from exc
                resolved[key] = value
        parent = manifest.get("base_run_id")
        if parent is not None and (not isinstance(parent, str) or not parent.strip()):
            raise BadRequest(f"run {current!r} has invalid base_run_id")
        current = parent
    return resolved
