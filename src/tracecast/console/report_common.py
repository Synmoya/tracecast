"""Shared lineage and presentation helpers for Console stage reports."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from tracecast.storage import AUDIT_REPORT

from . import inventory


def sources(root: Path, run_id: str, *specs: Any) -> dict[str, Any]:
    return inventory.resolve_artifact_sources(root, run_id, tuple(specs), required=False)


def origins(resolved: dict[str, Any]) -> dict[str, str]:
    return {relpath: store.run_id for relpath, store in resolved.items()}


def stage_diagnostics(resolved: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    source = resolved.get(AUDIT_REPORT.relpath)
    if source is None:
        return []
    return [
        item.model_dump(mode="json")
        for item in source.read_records(AUDIT_REPORT)
        if item.stage.replace("-", "_") == stage
    ]


def unavailable(run_id: str, stage: str, required: list[str]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "stage": stage,
        "available": False,
        "required_artifacts": required,
        "summary": {},
        "samples": [],
        "diagnostics": [],
    }


def distribution(values: list[int]) -> dict[str, int | float | None]:
    if not values:
        return {"min": None, "median": None, "p90": None, "max": None, "avg": None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median: int | float = (
        ordered[middle]
        if len(ordered) % 2
        else round((ordered[middle - 1] + ordered[middle]) / 2, 1)
    )
    return {
        "min": ordered[0],
        "median": median,
        "p90": ordered[max(0, math.ceil(len(ordered) * 0.9) - 1)],
        "max": ordered[-1],
        "avg": round(sum(ordered) / len(ordered), 1),
    }


def ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def sample_even(items: list[Any], limit: int) -> list[Any]:
    if len(items) <= limit:
        return list(items)
    indices = sorted(
        {round(index * (len(items) - 1) / (limit - 1)) for index in range(limit)}
    )
    return [items[index] for index in indices]


def excerpt(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
