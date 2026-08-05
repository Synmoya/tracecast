"""Source slicing and material-pack construction (Ingestion stage).

Turns declared raw sources into an auditable evidence pack:

    raw source
      -> SourcePiece sequence    (deterministic, source-global, no LLM)
      -> SourceWindow packing     (consecutive pieces under a token budget)
      -> atom-boundary DSL        (one LLM call per window via call_llm)
      -> code rebuilds MaterialAtom

Code owns structure (stable ids, content rebuild, provenance, partition checks);
the model only marks atom boundaries and assigns discrete labels. The driver
calls :func:`ingest_sources` (or :func:`ingest_source`) with declared snapshots,
per-source profiles and an injected ``RuntimeConfig`` / ``RunStore``, then
persists the returned artifacts (optionally via :func:`persist_ingest_result`).
Inject a fake ``call`` to exercise the DSL path with no network.
"""

from __future__ import annotations

from .atoms import build_atom, rebuild_content
from .dsl import (
    AtomSpec,
    ParsedWindow,
    PartialParsedWindow,
    WindowParseError,
    build_manifest,
    parse_window_response,
    parse_window_response_partial,
    project_window,
)
from .inputs import (
    EXTRACTOR_VERSION,
    OPERATION_ROUTE,
    IngestionBudgets,
    LanguageDetector,
    SourceInput,
    SourceProfile,
    build_source_inputs,
    source_language_detector,
)
from .pipeline import (
    CallLlm,
    ingest_source,
    ingest_sources,
    persist_ingest_result,
)
from .results import (
    IngestionError,
    IngestionItem,
    IngestResult,
    SourceIngestResult,
)
from .slicing import split_source
from .windowing import pack_windows

__all__ = [
    # entry points
    "ingest_sources",
    "ingest_source",
    "persist_ingest_result",
    # injected inputs
    "SourceInput",
    "SourceProfile",
    "build_source_inputs",
    "IngestionBudgets",
    "LanguageDetector",
    "source_language_detector",
    "CallLlm",
    # results / report
    "IngestResult",
    "SourceIngestResult",
    "IngestionItem",
    "IngestionError",
    # deterministic stages (no LLM)
    "split_source",
    "pack_windows",
    # atom-boundary DSL
    "build_manifest",
    "project_window",
    "parse_window_response",
    "parse_window_response_partial",
    "AtomSpec",
    "ParsedWindow",
    "PartialParsedWindow",
    "WindowParseError",
    # atom compile
    "build_atom",
    "rebuild_content",
    # constants
    "OPERATION_ROUTE",
    "EXTRACTOR_VERSION",
]
