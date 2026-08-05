"""Instant, non-persisted ingestion diagnostics for the Console."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from tracecast.domain import estimate_tokens
from tracecast.storage import MATERIAL_ATOMS, SOURCE_PIECES, SOURCE_WINDOWS, RunStore

from .errors import NotFound
from .run_artifacts import resolve_artifact_sources


def ingestion_audit(store: RunStore, *, sample_size: int = 16) -> dict[str, Any]:
    """Build a readable, non-persisted audit view from existing ingestion artifacts."""
    origins = resolve_artifact_sources(
        store.run_dir.parent,
        store.run_id,
        (SOURCE_PIECES, SOURCE_WINDOWS, MATERIAL_ATOMS),
        required=False,
    )
    if SOURCE_PIECES.relpath not in origins or MATERIAL_ATOMS.relpath not in origins:
        raise NotFound(f"run lineage {store.run_id!r} has no complete ingestion artifacts")

    sample_size = max(1, min(sample_size, 40))
    pieces = sorted(
        origins[SOURCE_PIECES.relpath].read_records(SOURCE_PIECES),
        key=lambda row: (row.source_id, row.order_index),
    )
    atoms = sorted(
        origins[MATERIAL_ATOMS.relpath].read_records(MATERIAL_ATOMS),
        key=lambda row: (row.provenance.source_ref, row.source_order),
    )
    windows = (
        sorted(
            origins[SOURCE_WINDOWS.relpath].read_records(SOURCE_WINDOWS),
            key=lambda row: (row.source_id, row.order_index),
        )
        if SOURCE_WINDOWS.relpath in origins
        else []
    )

    pieces_by_source: dict[str, list[Any]] = defaultdict(list)
    atoms_by_source: dict[str, list[Any]] = defaultdict(list)
    windows_by_source: dict[str, list[Any]] = defaultdict(list)
    for piece in pieces:
        pieces_by_source[piece.source_id].append(piece)
    for atom in atoms:
        atoms_by_source[atom.provenance.source_ref].append(atom)
    for window in windows:
        windows_by_source[window.source_id].append(window)

    source_ids = sorted(set(pieces_by_source) | set(atoms_by_source))
    sources = [
        _source_summary(
            source_id,
            pieces_by_source[source_id],
            windows_by_source[source_id],
            atoms_by_source[source_id],
        )
        for source_id in source_ids
    ]
    window_rows = _window_audit_rows(windows, atoms)
    return {
        "run_id": store.run_id,
        "origins": {relpath: origin.run_id for relpath, origin in origins.items()},
        "summary": _summary(pieces, windows, atoms, window_rows),
        "sources": sources,
        "underaggregated_windows": [
            row for row in window_rows if row["underaggregated"]
        ][:12],
        "samples": _sample_atoms(atoms_by_source, sample_size),
    }


def _source_summary(
    source_id: str, pieces: list[Any], windows: list[Any], atoms: list[Any]
) -> dict[str, Any]:
    single = sum(len(atom.provenance.piece_refs) == 1 for atom in atoms)
    return {
        "source_id": source_id,
        "pieces": len(pieces),
        "windows": len(windows),
        "atoms": len(atoms),
        "aggregation_factor": _ratio(len(pieces), len(atoms)),
        "single_piece_atoms": single,
        "single_piece_ratio": _ratio(single, len(atoms)),
        "piece_tokens": _distribution([estimate_tokens(piece.text) for piece in pieces]),
        "atom_tokens": _distribution([estimate_tokens(atom.content) for atom in atoms]),
        "window_tokens": _distribution([window.estimated_input_tokens for window in windows]),
    }


def _summary(
    pieces: list[Any], windows: list[Any], atoms: list[Any], window_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    single = sum(len(atom.provenance.piece_refs) == 1 for atom in atoms)
    configured_budgets = {
        int(window.metadata["max_input_tokens"])
        for window in windows
        if isinstance(window.metadata.get("max_input_tokens"), int)
    }
    return {
        "sources": len({piece.source_id for piece in pieces}),
        "pieces": len(pieces),
        "windows": len(windows),
        "atoms": len(atoms),
        "aggregation_factor": _ratio(len(pieces), len(atoms)),
        "single_piece_atoms": single,
        "single_piece_ratio": _ratio(single, len(atoms)),
        "piece_tokens": _distribution([estimate_tokens(piece.text) for piece in pieces]),
        "atom_tokens": _distribution([estimate_tokens(atom.content) for atom in atoms]),
        "window_tokens": _distribution([window.estimated_input_tokens for window in windows]),
        "configured_window_budgets": sorted(configured_budgets),
        "underaggregated_windows": sum(row["underaggregated"] for row in window_rows),
    }


def _window_audit_rows(windows: list[Any], atoms: list[Any]) -> list[dict[str, Any]]:
    atom_by_piece = {
        piece_ref: atom for atom in atoms for piece_ref in atom.provenance.piece_refs
    }
    rows: list[dict[str, Any]] = []
    for window in windows:
        window_atoms: list[Any] = []
        seen: set[str] = set()
        for piece_id in window.piece_ids:
            atom = atom_by_piece.get(piece_id)
            if atom is not None and atom.id not in seen:
                seen.add(atom.id)
                window_atoms.append(atom)
        atom_count = len(window_atoms)
        piece_count = len(window.piece_ids)
        one_to_one_ratio = _ratio(atom_count, piece_count)
        rows.append(
            {
                "source_id": window.source_id,
                "window_id": window.window_id,
                "order_index": window.order_index,
                "estimated_input_tokens": window.estimated_input_tokens,
                "pieces": piece_count,
                "atoms": atom_count,
                "aggregation_factor": _ratio(piece_count, atom_count),
                "one_to_one_ratio": one_to_one_ratio,
                "underaggregated": piece_count >= 8 and one_to_one_ratio >= 0.85,
                "preview": _excerpt(window_atoms[0].content) if window_atoms else "",
            }
        )
    rows.sort(key=lambda row: (row["one_to_one_ratio"], row["pieces"]), reverse=True)
    return rows


def _sample_atoms(atoms_by_source: dict[str, list[Any]], limit: int) -> list[dict[str, Any]]:
    if not atoms_by_source:
        return []
    per_source = max(1, math.ceil(limit / len(atoms_by_source)))
    source_samples: dict[str, list[Any]] = {}
    for source_id, atoms in sorted(atoms_by_source.items()):
        indices = _even_indices(len(atoms), min(per_source, len(atoms)))
        source_samples[source_id] = [atoms[index] for index in indices]

    samples: list[dict[str, Any]] = []
    for offset in range(per_source):
        for source_id in sorted(source_samples):
            rows = source_samples[source_id]
            if offset >= len(rows):
                continue
            atom = rows[offset]
            samples.append(
                {
                    "source_id": source_id,
                    "source_order": atom.source_order,
                    "atom_id": atom.id,
                    "piece_count": len(atom.provenance.piece_refs),
                    "estimated_tokens": estimate_tokens(atom.content),
                    "content_kind": atom.content_kind.value,
                    "tags": atom.tags,
                    "content": atom.content,
                }
            )
            if len(samples) >= limit:
                return samples
    return samples


def _even_indices(total: int, count: int) -> list[int]:
    if count <= 1:
        return [0]
    return sorted({round(index * (total - 1) / (count - 1)) for index in range(count)})


def _distribution(values: list[int]) -> dict[str, int | float | None]:
    if not values:
        return {"min": None, "median": None, "p90": None, "max": None, "avg": None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else round((ordered[middle - 1] + ordered[middle]) / 2, 1)
    )
    p90 = ordered[max(0, math.ceil(len(ordered) * 0.9) - 1)]
    return {
        "min": ordered[0],
        "median": median,
        "p90": p90,
        "max": ordered[-1],
        "avg": round(sum(ordered) / len(ordered), 1),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _excerpt(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "..."
