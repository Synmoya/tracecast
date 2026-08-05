"""Compile parsed DSL rows into ``MaterialAtom`` records (code owns ids).

Code -- not the model -- assigns stable ids, rebuilds content, and writes
provenance. ``MaterialAtom.content`` is the verbatim concatenation of the source
pieces the atom covers (pieces tile the source losslessly), so the content is a
source substring the model never authored. ``content_kind``/``tags`` are excluded
from id derivation, so relabelling never changes an atom's identity.
"""

from __future__ import annotations

from tracecast.domain import (
    MaterialAtom,
    Provenance,
    SourcePiece,
    SourceSnapshot,
    atom_id,
)

from .dsl import AtomSpec
from .inputs import EXTRACTOR_VERSION, LanguageDetector


def rebuild_content(piece_refs: tuple[str, ...], piece_by_id: dict[str, SourcePiece]) -> str:
    """Verbatim atom content: concatenation of the covered source pieces' text."""
    return "".join(piece_by_id[ref].text for ref in piece_refs)


def build_atom(
    spec: AtomSpec,
    *,
    snapshot: SourceSnapshot,
    piece_by_id: dict[str, SourcePiece],
    source_order: int,
    detect_language: LanguageDetector,
    source_language: str,
) -> MaterialAtom:
    """Build one stable ``MaterialAtom`` from a parsed atom row."""
    piece_refs = list(spec.piece_refs)
    first = piece_by_id[piece_refs[0]]
    last = piece_by_id[piece_refs[-1]]
    content = rebuild_content(spec.piece_refs, piece_by_id)
    provenance = Provenance(
        source_ref=snapshot.source_id,
        source_type=snapshot.source_type,
        source_class=snapshot.source_class,
        source_content_hash=snapshot.content_hash,
        fetched_at=snapshot.fetched_at,
        span_start=first.span_start,
        span_end=last.span_end,
        piece_refs=piece_refs,
        extractor_version=EXTRACTOR_VERSION,
    )
    return MaterialAtom(
        id=atom_id(snapshot.source_id, piece_refs),
        content=content,
        language=detect_language(content, source_language),
        content_kind=spec.content_kind,
        source_order=source_order,
        trust_tier=snapshot.trust_tier,
        provenance=provenance,
        tags=list(spec.tags),
        source_material_ids=[],
    )
