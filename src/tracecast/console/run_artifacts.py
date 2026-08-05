"""Lineage-aware run inventory and registered artifact reads."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracecast.storage import (
    ARTIFACT_SPECS,
    CHUNK_ARTIFACTS,
    SCRIPT,
    RunStore,
    json_io,
    jsonl_io,
)
from tracecast.storage.run_store import ArtifactFormat

from .errors import BadRequest, NotFound

MANIFEST_RELPATH = "console/manifest.json"
_SPECS_BY_RELPATH = {spec.relpath: spec for spec in ARTIFACT_SPECS}


def list_runs(runs_root: str | Path) -> list[dict[str, Any]]:
    """List run dirs (newest first) with artifact presence + manifest summary."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    runs: list[dict[str, Any]] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        store = RunStore(child)
        present = [spec.relpath for spec in ARTIFACT_SPECS if store.exists(spec)]
        if store.exists(SCRIPT):
            present.append(SCRIPT.relpath)
        manifest = read_console_manifest(store)
        if (
            (child / "host_builder" / "manifest.json").is_file()
            and manifest is None
        ):
            # Host Builder has its own run artifacts, but is not a pipeline run.
            # A real Builder call writes the shared LLM ledger, so ``present`` is
            # deliberately irrelevant to this application-boundary check.
            continue
        runs.append(
            {
                "run_id": child.name,
                "modified_at": datetime.fromtimestamp(child.stat().st_mtime, tz=UTC).isoformat(),
                "present": present,
                "console_manifest": _manifest_summary(manifest),
            }
        )
    runs.sort(key=lambda row: row["modified_at"], reverse=True)
    return runs


def open_run(runs_root: str | Path, run_id: str) -> RunStore:
    """Open an existing run dir as a :class:`RunStore` (404 when absent)."""
    try:
        store = RunStore.for_run(runs_root, run_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    if not store.run_dir.is_dir():
        raise NotFound(f"unknown run {run_id!r}")
    return store


def resolve_artifact_sources(
    runs_root: str | Path,
    start_run_id: str,
    specs: tuple[Any, ...],
    *,
    required: bool = True,
) -> dict[str, RunStore]:
    """Resolve artifact slots through a console run's ``base_run_id`` lineage."""
    pending = {spec.relpath: spec for spec in specs}
    resolved: dict[str, RunStore] = {}
    lineage: list[str] = []
    seen: set[str] = set()
    run_id: str | None = start_run_id

    while run_id is not None and pending:
        if run_id in seen:
            chain = " -> ".join([*lineage, run_id])
            raise BadRequest(f"cycle in console run lineage: {chain}")
        seen.add(run_id)
        lineage.append(run_id)
        try:
            store = open_run(runs_root, run_id)
        except NotFound as exc:
            if len(lineage) == 1:
                raise
            raise BadRequest(
                f"broken console run lineage: ancestor {run_id!r} does not exist"
            ) from exc

        for relpath, spec in list(pending.items()):
            if store.exists(spec):
                resolved[relpath] = store
                del pending[relpath]

        manifest = read_console_manifest(store)
        parent = (manifest or {}).get("base_run_id")
        if parent is None:
            run_id = None
        elif not isinstance(parent, str) or not parent.strip():
            raise BadRequest(
                f"broken console run lineage: {run_id!r} has invalid base_run_id {parent!r}"
            )
        else:
            run_id = parent

    if pending and required:
        missing = ", ".join(sorted(pending))
        searched = " -> ".join(lineage)
        raise BadRequest(
            f"run lineage starting at {start_run_id!r} has no {missing} "
            f"(searched: {searched})"
        )
    return resolved


def run_inventory(store: RunStore) -> dict[str, Any]:
    """Per-slot inventory including the nearest effective lineage origin."""
    slots: list[dict[str, Any]] = []
    for spec in ARTIFACT_SPECS:
        local_path = store.path_for(spec.relpath)
        sources = resolve_artifact_sources(
            store.run_dir.parent, store.run_id, (spec,), required=False
        )
        origin = sources.get(spec.relpath)
        path = origin.path_for(spec.relpath) if origin is not None else local_path
        entry: dict[str, Any] = {
            "relpath": spec.relpath,
            "exists": local_path.exists(),
            "exists_here": local_path.exists(),
            "available": origin is not None,
            "origin_run_id": origin.run_id if origin is not None else None,
        }
        if origin is not None:
            entry["bytes"] = path.stat().st_size
            if spec.format is ArtifactFormat.jsonl:
                entry["rows"] = _count_jsonl_rows(path)
        slots.append(entry)
    script_sources = resolve_artifact_sources(
        store.run_dir.parent, store.run_id, (SCRIPT,), required=False
    )
    script_origin = script_sources.get(SCRIPT.relpath)
    script_path = (
        script_origin.path_for(SCRIPT.relpath)
        if script_origin is not None
        else store.path_for(SCRIPT.relpath)
    )
    slots.append(
        {
            "relpath": SCRIPT.relpath,
            "exists": store.path_for(SCRIPT.relpath).exists(),
            "exists_here": store.path_for(SCRIPT.relpath).exists(),
            "available": script_origin is not None,
            "origin_run_id": script_origin.run_id if script_origin is not None else None,
            **({"bytes": script_path.stat().st_size} if script_origin is not None else {}),
        }
    )
    return {
        "run_id": store.run_id,
        "slots": slots,
        "console_manifest": read_console_manifest(store),
    }


def read_artifact(store: RunStore, relpath: str, *, limit: int | None = None) -> dict[str, Any]:
    """Read one registered artifact slot as plain JSON data."""
    if relpath == SCRIPT.relpath:
        sources = resolve_artifact_sources(
            store.run_dir.parent, store.run_id, (SCRIPT,), required=False
        )
        origin = sources.get(relpath)
        if origin is None:
            raise NotFound(f"run lineage {store.run_id!r} has no {relpath}")
        return {
            "relpath": relpath,
            "format": "text",
            "text": origin.read_text(SCRIPT),
            "origin_run_id": origin.run_id,
        }
    if relpath == MANIFEST_RELPATH:
        manifest = read_console_manifest(store)
        if manifest is None:
            raise NotFound(f"run {store.run_id!r} has no console manifest")
        return {"relpath": relpath, "format": "json", "data": manifest}
    spec = _SPECS_BY_RELPATH.get(relpath)
    if spec is None:
        raise NotFound(f"unknown artifact relpath {relpath!r}")
    sources = resolve_artifact_sources(
        store.run_dir.parent, store.run_id, (spec,), required=False
    )
    origin = sources.get(relpath)
    if origin is None:
        raise NotFound(f"artifact {relpath!r} unavailable in run lineage {store.run_id!r}")
    path = origin.path_for(spec.relpath)
    if spec.format is ArtifactFormat.json_model:
        return {
            "relpath": relpath,
            "format": "json",
            "data": json_io.read_json(path),
            "origin_run_id": origin.run_id,
        }
    rows = jsonl_io.read_jsonl(path)
    total = len(rows)
    if limit is not None:
        rows = rows[: max(limit, 0)]
    return {
        "relpath": relpath,
        "format": "jsonl",
        "total_rows": total,
        "rows": rows,
        "origin_run_id": origin.run_id,
    }


def list_chunks(store: RunStore) -> list[dict[str, Any]]:
    """Chunk summaries for a run, in ``order_index`` order."""
    sources = resolve_artifact_sources(
        store.run_dir.parent, store.run_id, (CHUNK_ARTIFACTS,), required=False
    )
    origin = sources.get(CHUNK_ARTIFACTS.relpath)
    if origin is None:
        return []
    chunks = origin.read_records(CHUNK_ARTIFACTS)
    chunks.sort(key=lambda chunk: chunk.order_index)
    return [
        {
            "chunk_id": chunk.chunk_id,
            "order_index": chunk.order_index,
            "contract_id": chunk.contract_id,
            "chars": len(chunk.text),
            "call_id": chunk.metadata.get("call_id"),
            "output_hash": chunk.output_hash,
            "origin_run_id": origin.run_id,
        }
        for chunk in chunks
    ]


def read_console_manifest(store: RunStore) -> dict[str, Any] | None:
    """The derived-run provenance manifest, or ``None`` for a plain run."""
    path = store.path_for(MANIFEST_RELPATH)
    if not path.exists():
        return None
    return json_io.read_json(path)


def _manifest_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if manifest is None:
        return None
    request = manifest.get("request") or {}
    effective = manifest.get("effective") or {}
    return {
        "kind": manifest.get("kind"),
        "base_run_id": manifest.get("base_run_id"),
        "status": manifest.get("status"),
        "created_at": manifest.get("created_at"),
        "run_label": request.get("run_label"),
        "project_id": request.get("project_id") or effective.get("project_id"),
    }


def _count_jsonl_rows(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count
