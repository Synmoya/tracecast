"""Summarize ingestion atom quality for one run.

Usage:
    python tools/atom_report.py runs/<run_id> --out runs/<run_id>/ingestion/atom_report.md
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from tracecast.domain import (
    MaterialAtom,
    SourcePiece,
    SourceWindow,
    estimate_tokens,
)
from tracecast.storage import (
    LLM_CALL_RECORDS,
    MATERIAL_ATOMS,
    SOURCE_PIECES,
    SOURCE_WINDOWS,
    RunStore,
)


def build_report(run_dir: Path) -> str:
    store = RunStore(run_dir)
    pieces = store.read_records(SOURCE_PIECES)
    windows = store.read_records(SOURCE_WINDOWS)
    atoms = store.read_records(MATERIAL_ATOMS)
    calls = store.read_records(LLM_CALL_RECORDS) if store.exists(LLM_CALL_RECORDS) else []

    lines = [
        f"# Atom Report: {run_dir.name}",
        "",
        "## Coverage",
        *_coverage_lines(pieces, atoms),
        "",
        "## Atom Granularity",
        *_granularity_lines(atoms),
        "",
        "## Content Kind",
        *_counter_lines(Counter(atom.content_kind.value for atom in atoms), len(atoms)),
        "",
        "## Tags",
        *_counter_lines(Counter(tag for atom in atoms for tag in atom.tags), len(atoms)),
        "",
        "## Calls",
        *_call_lines(windows, calls),
        "",
    ]
    return "\n".join(lines)


def _coverage_lines(pieces: list[SourcePiece], atoms: list[MaterialAtom]) -> list[str]:
    source_piece_ids = {piece.piece_id for piece in pieces}
    covered = [piece_ref for atom in atoms for piece_ref in atom.provenance.piece_refs]
    covered_set = set(covered)
    missing = sorted(source_piece_ids - covered_set)
    duplicate_count = sum(count - 1 for count in Counter(covered).values() if count > 1)
    coverage = (
        100.0 * len(covered_set & source_piece_ids) / len(source_piece_ids) if pieces else 100.0
    )
    return [
        f"- pieces: {len(pieces)}",
        f"- atoms: {len(atoms)}",
        f"- covered pieces: {len(covered_set & source_piece_ids)} ({coverage:.2f}%)",
        f"- missing pieces: {len(missing)}",
        f"- duplicate piece refs: {duplicate_count}",
    ]


def _granularity_lines(atoms: list[MaterialAtom]) -> list[str]:
    if not atoms:
        return ["- no atoms"]
    piece_counts = [len(atom.provenance.piece_refs) for atom in atoms]
    chars = [len(atom.content) for atom in atoms]
    tokens = [estimate_tokens(atom.content) for atom in atoms]
    return [
        f"- avg pieces / atom: {_avg(piece_counts):.2f}",
        f"- avg chars / atom: {_avg(chars):.1f}",
        f"- median chars / atom: {_median(chars):.1f}",
        f"- avg estimated tokens / atom: {_avg(tokens):.1f}",
        f"- median estimated tokens / atom: {_median(tokens):.1f}",
    ]


def _counter_lines(counter: Counter[str], denominator: int, *, limit: int = 24) -> list[str]:
    if not counter:
        return ["- none"]
    total = sum(counter.values())
    denom = denominator or total
    lines = []
    for key, count in counter.most_common(limit):
        lines.append(f"- {key}: {count} ({100.0 * count / denom:.1f}% of atoms)")
    if len(counter) > limit:
        lines.append(f"- ... {len(counter) - limit} more")
    return lines


def _call_lines(windows: list[SourceWindow], calls: list[object]) -> list[str]:
    atom_calls = [
        call for call in calls if getattr(call, "operation_route", None) == "atom_boundary_dsl"
    ]
    input_tokens = sum((getattr(call, "input_tokens", None) or 0) for call in atom_calls)
    output_tokens = sum((getattr(call, "output_tokens", None) or 0) for call in atom_calls)
    return [
        f"- windows: {len(windows)}",
        f"- atom LLM attempts: {len(atom_calls)}",
        f"- extra attempts beyond windows: {max(0, len(atom_calls) - len(windows))}",
        f"- input tokens: {input_tokens}",
        f"- output tokens: {output_tokens}",
    ]


def _avg(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = build_report(args.run_dir)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
