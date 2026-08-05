"""Stable facade for Console read-only project and run inspection.

Implementation is split by concern so project pickers, lineage/artifact reads,
ingestion diagnostics, and LLM payload inspection can evolve independently.
Existing imports continue to use :mod:`tracecast.console.inventory`.
"""

from .ingestion_audit import ingestion_audit
from .llm_records import llm_calls, raw_payload
from .projects import list_project_sources, list_projects, open_project_dir
from .run_artifacts import (
    MANIFEST_RELPATH,
    list_chunks,
    list_runs,
    open_run,
    read_artifact,
    read_console_manifest,
    resolve_artifact_sources,
    run_inventory,
)

__all__ = [
    "MANIFEST_RELPATH",
    "ingestion_audit",
    "list_chunks",
    "list_project_sources",
    "list_projects",
    "list_runs",
    "llm_calls",
    "open_project_dir",
    "open_run",
    "raw_payload",
    "read_artifact",
    "read_console_manifest",
    "resolve_artifact_sources",
    "run_inventory",
]
