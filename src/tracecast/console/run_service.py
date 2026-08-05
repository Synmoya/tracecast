"""Shared run creation, manifest lifecycle, and provider progress for Console."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from tracecast.storage import AUDIT_REPORT, RunStore, json_io

from .context import ConsoleContext
from .errors import Conflict
from .inventory import MANIFEST_RELPATH
from .jobs import Job
from .overrides import ConfigOverrides, PersonaPatch

MANIFEST_SCHEMA = "tracecast-console-manifest/1"
_LABEL_RE = re.compile(r"[^a-z0-9-]+")


def derived_run_id(base_run_id: str, kind: str, run_label: str | None = None) -> str:
    """Name a derived run ``{base}-[{label}-]{kind}-{UTC timestamp}``."""

    stamp = f"{datetime.now(tz=UTC):%Y%m%dT%H%M%S%fZ}"
    label = _sanitize_label(run_label)
    middle = f"{label}-{kind}" if label else kind
    return f"{base_run_id}-{middle}-{stamp}"


def create_derived_store(
    ctx: ConsoleContext,
    base_run_id: str,
    kind: str,
    run_label: str | None,
) -> RunStore:
    """Create a derived run directory and refuse an existing id."""

    run_id = derived_run_id(base_run_id, kind, run_label)
    try:
        store = RunStore.create_for_run(ctx.runs_root, run_id)
    except FileExistsError:
        raise Conflict(f"run dir {run_id!r} already exists") from None
    return store


def base_manifest(
    *,
    kind: str,
    base_run_id: str | None,
    job_id: str | None,
    status: str,
    request: dict[str, Any],
    effective: dict[str, Any],
    copied_artifacts: list[str],
    artifact_sources: dict[str, str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the canonical Console manifest payload."""

    return {
        "schema": MANIFEST_SCHEMA,
        "kind": kind,
        "base_run_id": base_run_id,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "job_id": job_id,
        "status": status,
        "request": request,
        "effective": effective,
        "copied_artifacts": copied_artifacts,
        "artifact_sources": artifact_sources or {},
        "error": error,
    }


def run_manifested_job(
    ctx: ConsoleContext,
    *,
    store: RunStore,
    kind: str,
    base_run_id: str | None,
    manifest_seed: dict[str, Any],
    target: Any,
    progress_total: int | None,
) -> Job:
    """Submit a job whose lifecycle is mirrored into its run manifest."""

    def write_manifest(manifest: dict[str, Any]) -> None:
        json_io.write_json(store.path_for(MANIFEST_RELPATH), manifest)

    def runner(job: Job) -> dict[str, Any]:
        manifest = dict(manifest_seed, job_id=job.job_id, status="running")
        write_manifest(manifest)
        try:
            result = target(job)
        except Exception as exc:
            failed = "cancelled" if job.cancel_event.is_set() else "failed"
            write_manifest(dict(manifest, status=failed, error=str(exc)))
            raise
        write_manifest(dict(manifest, status="complete"))
        return result

    pending = dict(manifest_seed, job_id=None, status="pending")
    write_manifest(pending)
    try:
        return ctx.registry.submit(
            kind=kind,
            run_id=store.run_id,
            base_run_id=base_run_id,
            target=runner,
            progress_total=progress_total,
        )
    except Exception as exc:
        write_manifest(
            dict(
                pending,
                status="failed",
                copied_artifacts=[],
                error=str(exc),
            )
        )
        raise


def progress_call(ctx: ConsoleContext, job: Job, label: str) -> Any:
    """Wrap the provider seam with cooperative cancellation and progress."""

    def wrapped(**kwargs: Any) -> Any:
        job.check_cancelled()
        total = f"/{job.progress_total}" if job.progress_total else ""
        job.progress_message = f"{label} {job.progress_done + 1}{total}"
        result = ctx.call(**kwargs)
        job.advance()
        return result

    return wrapped


def request_payload(
    persona_patch: PersonaPatch | None,
    config_overrides: ConfigOverrides | None,
    run_label: str | None,
    **extra: Any,
) -> dict[str, Any]:
    """Serialize the operator request stored in a Console manifest."""

    return {
        "persona_patch": persona_patch.model_dump(exclude_none=True) if persona_patch else {},
        "config_overrides": (
            config_overrides.model_dump(exclude_none=True) if config_overrides else {}
        ),
        "run_label": run_label,
        **extra,
    }


def read_stage_reports(
    sources: dict[str, RunStore],
    stages: set[str],
) -> list[Any]:
    """Read the nearest aggregate report and retain only unchanged stages."""

    source = sources.get(AUDIT_REPORT.relpath)
    if source is None:
        return []
    return [
        item
        for item in source.read_records(AUDIT_REPORT)
        if item.stage.replace("-", "_") in stages
    ]


def _sanitize_label(run_label: str | None) -> str:
    if not run_label:
        return ""
    return _LABEL_RE.sub("-", run_label.lower()).strip("-")[:24]
