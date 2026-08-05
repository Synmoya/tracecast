"""Stable dispatch facade for on-demand Console stage reports."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import inventory
from .errors import BadRequest
from .report_early import ingestion, node_edit, planning, selection
from .report_late import assembly, name_resolution, packing, rendering, validation

STAGES = (
    "ingestion",
    "planning",
    "selection",
    "node_edit",
    "packing",
    "name_resolution",
    "rendering",
    "validation",
    "assembly",
)

_BUILDERS: dict[str, Callable[[Path, str], dict[str, Any]]] = {
    "ingestion": ingestion,
    "planning": planning,
    "selection": selection,
    "node_edit": node_edit,
    "packing": packing,
    "name_resolution": name_resolution,
    "rendering": rendering,
    "validation": validation,
    "assembly": assembly,
}


def stage_report(runs_root: str | Path, run_id: str, stage: str) -> dict[str, Any]:
    """Return one structured immediate report, or ``available=false``."""
    normalized = stage.replace("-", "_").lower()
    builder = _BUILDERS.get(normalized)
    if builder is None:
        raise BadRequest(f"unknown report stage {stage!r}; expected one of {STAGES}")
    root = Path(runs_root)
    inventory.open_run(root, run_id)
    result = builder(root, run_id)
    result.setdefault("run_id", run_id)
    result.setdefault("stage", normalized)
    result.setdefault("available", True)
    return result
