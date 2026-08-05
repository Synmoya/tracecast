"""Read-only project picker data for the local Console."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BadRequest, NotFound


def list_projects(projects_dir: str | Path) -> list[dict[str, Any]]:
    """List project dirs that carry a ``sources.json`` (the loadable ones)."""
    root = Path(projects_dir)
    if not root.is_dir():
        return []
    projects: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        spec_path = child / "sources.json"
        if not child.is_dir() or not spec_path.exists():
            continue
        project_id = child.name
        output_language: str | None = None
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            project_id = spec.get("project_id", child.name)
            output_language = spec.get("output_language")
        except (OSError, json.JSONDecodeError):
            pass
        projects.append(
            {
                "project_id": project_id,
                "dir": child.name,
                "output_language": output_language,
            }
        )
    return projects


def list_project_sources(projects_dir: str | Path, project_id: str) -> list[dict[str, Any]]:
    """List declared sources with basic stats, tolerating missing raw files."""
    project_dir = open_project_dir(projects_dir, project_id)
    spec_path = project_dir / "sources.json"
    if not spec_path.exists():
        raise NotFound(f"project {project_id!r} has no sources.json")
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BadRequest(f"project {project_id!r} sources.json is not valid JSON: {exc}") from exc
    sources: list[dict[str, Any]] = []
    for entry in spec.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        chars: int | None = None
        exists = False
        rel = entry.get("path")
        if rel:
            path = project_dir / str(rel)
            exists = path.is_file()
            if exists:
                try:
                    chars = len(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    chars = None
        sources.append(
            {
                "source_id": source_id,
                "title": entry.get("title") or source_id,
                "profile": entry.get("profile"),
                "language": entry.get("language"),
                "source_type": entry.get("source_type"),
                "chars": chars,
                "exists": exists,
            }
        )
    return sources


def open_project_dir(projects_dir: str | Path, project_id: str) -> Path:
    """Resolve a safe existing project directory or raise a console-layer error."""
    if not project_id or "/" in project_id or "\\" in project_id or project_id in {".", ".."}:
        raise BadRequest(f"unsafe project id: {project_id!r}")
    path = Path(projects_dir) / project_id
    if not path.is_dir():
        raise NotFound(f"unknown project {project_id!r}")
    return path
