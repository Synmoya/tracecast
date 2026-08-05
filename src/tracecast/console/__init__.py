"""Local console: fresh Ingestion runs, stage derives, chunk regen, and diffs.

A developer-only FastAPI app over project inputs and run artifacts. It can create
an Ingestion-only root run; derives never mutate their base and always write a new
run dir. Every console-created run carries ``console/manifest.json`` provenance.
Spend is two-phase: a dry-run estimate first, then an explicit ``confirm``.

``fastapi``/``uvicorn`` are an optional extra
(``python -m pip install '.[console]'``);
importing this package stays dependency-free, ``create_app`` is loaded lazily.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
