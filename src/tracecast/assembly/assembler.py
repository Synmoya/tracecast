"""Deterministic final-script assembly.

Assembly is the *mechanical* concatenation step. It takes the available rendered
:class:`~tracecast.domain.ChunkArtifact` bodies,
orders them by the authoritative :class:`~tracecast.domain.ChunkPlan` order,
concatenates them verbatim into one Markdown script, and emits an
:class:`~tracecast.domain.AssemblyReport` recording assembly integrity.

It is a *pure transform*: no config, no IO, no LLM, no network, no randomness.
It never rewrites body text, never invents
bridging sentences, never merges or reorders chunks, and never repairs renderer
content. When it cannot produce a clean assembly -- missing / duplicate /
unexpected chunks, an order conflict, or upstream report issues carried in -- it
still returns the script but flags ``assembled_with_issues``. It never silently
skips a missing chunk, reorders input, or repairs content.

Pre-render Name Resolution only changes the temporary renderer projection.
Assembly therefore always consumes ``ChunkArtifact.text`` verbatim and has no
name-replacement override path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from tracecast.domain import AssemblyReport, ChunkArtifact, ChunkPlan
from tracecast.storage.hashing import sha256_text

CHUNK_SEPARATOR = "\n\n"
"""Fixed blank-line separator inserted *between* chunks/sections.

Only ever placed between distinct sections; each chunk's own ``text`` is left
byte-for-byte intact.
"""


def assemble_script(
    *,
    chunk_plan: ChunkPlan,
    chunks: Iterable[ChunkArtifact],
    title: str | None = None,
    front_matter: str | None = None,
    validation_report_ref: str | None = None,
    upstream_has_issues: bool = False,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, AssemblyReport]:
    """Concatenate available chunks in plan order; return ``(script_text, report)``.

    Deterministic and pure: the same inputs always yield the same script text and
    report. The caller (pipeline driver) persists ``script.md`` and the report via
    the storage layer; Assembly performs no IO.

    Args:
        chunk_plan: The authoritative chunk order and the *expected* chunk set.
            Assembly reads this order directly; it does not infer order from
            rendered artifacts.
        chunks: The available rendered ``ChunkArtifact`` objects (iterated once).
        title: Optional global title, rendered as a top-level ``# {title}`` heading.
        front_matter: Optional pre-rendered front-matter block, placed verbatim at
            the very top (Assembly does not construct front-matter semantics).
        validation_report_ref: Optional storage ref of the validation/audit report;
            recorded only.
        upstream_has_issues: Set by the driver when the validation/audit report
            carries issues that should mark the script ``assembled_with_issues``.
            Assembly does not re-derive validation severity itself.
        metadata: Optional extra report metadata; merged into the report's
            ``metadata`` (Assembly-detected diagnostics take precedence on
            conflict).

    Returns:
        ``(script_text, AssemblyReport)``. Report field semantics:
        ``chunk_order`` is the authoritative *full* plan order (including ids that
        turn out missing); ``assembled_chunk_ids`` is the realized subset actually
        concatenated, in order; ``missing_chunk_ids`` = expected but unavailable;
        ``duplicate_chunk_ids`` = chunk ids supplied more than once;
        ``per_chunk_text_hash`` hashes the *assembled* body of each placed chunk;
        ``final_script_hash`` hashes the full ``script_text``. Order conflicts,
        unexpected (off-plan) chunks, and any plan-internal duplicate ids are
        surfaced under ``metadata`` -- never silently dropped.
    """
    # --- authoritative expected order (from the ChunkPlan) -------------------
    plan_chunks = sorted(chunk_plan.chunks, key=lambda chunk: chunk.order_index)
    expected_order = _dedupe(chunk.chunk_id for chunk in plan_chunks)
    plan_order_by_id = {chunk.chunk_id: chunk.order_index for chunk in plan_chunks}
    plan_duplicate_chunk_ids = _duplicates(chunk.chunk_id for chunk in plan_chunks)
    expected_set = set(expected_order)

    # --- index available artifacts; first occurrence wins, dups are reported -
    by_id: dict[str, ChunkArtifact] = {}
    counts: dict[str, int] = {}
    for artifact in chunks:
        counts[artifact.chunk_id] = counts.get(artifact.chunk_id, 0) + 1
        by_id.setdefault(artifact.chunk_id, artifact)
    duplicate_chunk_ids = sorted(cid for cid, n in counts.items() if n > 1)

    missing_chunk_ids = [cid for cid in expected_order if cid not in by_id]
    unexpected_chunk_ids = sorted(cid for cid in by_id if cid not in expected_set)

    # --- assemble strictly in plan order; present chunks only, each placed once
    assembled_chunk_ids: list[str] = []
    bodies: list[str] = []
    per_chunk_text_hash: dict[str, str] = {}
    order_conflicts: list[dict[str, Any]] = []
    for cid in expected_order:
        artifact = by_id.get(cid)
        if artifact is None:
            continue
        body = artifact.text
        assembled_chunk_ids.append(cid)
        bodies.append(body)
        per_chunk_text_hash[cid] = _text_hash(body)
        if artifact.order_index != plan_order_by_id[cid]:
            order_conflicts.append(
                {
                    "chunk_id": cid,
                    "artifact_order_index": artifact.order_index,
                    "plan_order_index": plan_order_by_id[cid],
                }
            )

    script_text = _render_document(front_matter=front_matter, title=title, bodies=bodies)
    final_script_hash = _text_hash(script_text)

    assembled_with_issues = bool(
        missing_chunk_ids
        or duplicate_chunk_ids
        or unexpected_chunk_ids
        or order_conflicts
        or plan_duplicate_chunk_ids
        or upstream_has_issues
    )

    report_metadata: dict[str, Any] = dict(metadata or {})
    report_metadata["chunk_plan_id"] = chunk_plan.plan_id
    report_metadata["project_id"] = chunk_plan.project_id
    if unexpected_chunk_ids:
        report_metadata["unexpected_chunk_ids"] = unexpected_chunk_ids
    if order_conflicts:
        report_metadata["order_conflicts"] = order_conflicts
    if plan_duplicate_chunk_ids:
        report_metadata["plan_duplicate_chunk_ids"] = plan_duplicate_chunk_ids
    if upstream_has_issues:
        report_metadata["upstream_has_issues"] = True

    report = AssemblyReport(
        assembled_chunk_ids=assembled_chunk_ids,
        chunk_order=expected_order,
        missing_chunk_ids=missing_chunk_ids,
        duplicate_chunk_ids=duplicate_chunk_ids,
        per_chunk_text_hash=per_chunk_text_hash,
        final_script_hash=final_script_hash,
        assembled_with_issues=assembled_with_issues,
        validation_report_ref=validation_report_ref,
        metadata=report_metadata,
    )
    return script_text, report


def _render_document(*, front_matter: str | None, title: str | None, bodies: list[str]) -> str:
    """Join optional front matter, title heading, and chunk bodies into one script.

    Sections are separated by :data:`CHUNK_SEPARATOR`; chunk bodies are joined by
    the same separator. Each body is used verbatim -- only separators are inserted
    between sections, never inside a body.
    """
    sections: list[str] = []
    if front_matter:
        sections.append(front_matter)
    if title:
        sections.append(f"# {title}")
    if bodies:
        sections.append(CHUNK_SEPARATOR.join(bodies))
    return CHUNK_SEPARATOR.join(sections)


def _text_hash(text: str) -> str:
    """``"sha256:<hex>"`` -- the same convention as ``ChunkArtifact.output_hash``.

    So a per-chunk hash here is directly comparable to the artifact's
    ``output_hash`` whenever the assembled body equals the verbatim chunk text.
    """
    return f"sha256:{sha256_text(text)}"


def _dedupe(items: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _duplicates(items: Iterable[str]) -> list[str]:
    """Sorted list of values that appear more than once."""
    seen: set[str] = set()
    dups: set[str] = set()
    for item in items:
        if item in seen:
            dups.add(item)
        seen.add(item)
    return sorted(dups)
