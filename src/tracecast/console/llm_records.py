"""Lineage-aware LLM ledger and raw provider payload reads."""

from __future__ import annotations

from typing import Any

from tracecast.storage import LLM_CALL_RECORDS, RunStore, jsonl_io

from .errors import BadRequest, NotFound
from .run_artifacts import open_run, read_console_manifest


def llm_calls(store: RunStore) -> list[dict[str, Any]]:
    """The run's ``LlmCallRecord`` ledger as raw rows (may be empty)."""
    path = store.path_for(LLM_CALL_RECORDS.relpath)
    if not path.exists():
        return []
    return jsonl_io.read_jsonl(path)


def raw_payload(store: RunStore, call_id: str, role: str) -> dict[str, Any]:
    """Read one persisted raw provider request/response payload."""
    if role not in ("request", "response"):
        raise BadRequest("role must be 'request' or 'response'")
    origin = _find_raw_payload_origin(store, call_id, role)
    try:
        payload = origin.read_raw_payload(call_id, role)  # type: ignore[arg-type]
    except FileNotFoundError as exc:
        raise NotFound(str(exc)) from exc
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    if isinstance(payload, str):
        return {
            "call_id": call_id,
            "role": role,
            "format": "text",
            "text": payload,
            "origin_run_id": origin.run_id,
        }
    return {
        "call_id": call_id,
        "role": role,
        "format": "json",
        "data": payload,
        "origin_run_id": origin.run_id,
    }


def _find_raw_payload_origin(store: RunStore, call_id: str, role: str) -> RunStore:
    current = store
    seen: set[str] = set()
    while current.run_id not in seen:
        seen.add(current.run_id)
        raw_dir = current.run_dir / "llm" / "raw"
        if (raw_dir / f"{call_id}.{role}.json").exists() or (
            raw_dir / f"{call_id}.{role}.txt"
        ).exists():
            return current
        manifest = read_console_manifest(current) or {}
        parent = manifest.get("base_run_id")
        if not isinstance(parent, str) or not parent:
            break
        current = open_run(store.run_dir.parent, parent)
    raise NotFound(f"raw {role} payload for call {call_id!r} not found in run lineage")
