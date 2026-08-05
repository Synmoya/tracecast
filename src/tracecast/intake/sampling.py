"""Raw-source sampling for the sampled DSL intake probe.

This module deliberately reuses the core ingestion slicer. Intake samples
``SourcePiece`` rows; it does not define a second raw-text splitting policy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tracecast.domain import SourcePiece, estimate_tokens
from tracecast.ingestion.slicing import split_source
from tracecast.storage import content_hash

DEFAULT_SAMPLE_TOKEN_BUDGET = 1200
DEFAULT_SOURCE_PIECE_MAX_TOKENS = 120

_QA_RE = re.compile(
    r"^\s*(?:Q|A|Question|Answer|Interviewer|Interviewee|Developer)\s*[:：]",
    re.IGNORECASE,
)
_SPEAKER_RE = re.compile(r"^\s*[A-Z][A-Za-z0-9 .'\-]{1,40}\s*[:：]\s+\S")
_STAGE_RE = re.compile(r"^\s*(?:\[.*\]|\(.*\)|<.*>)\s*$")
_REVIEW_RE = re.compile(
    r"\b(?:review|retrospective|recommend|rating|score|verdict|opinion|"
    r"i think|i feel|in my view|gameplay|graphics|soundtrack)\b",
    re.IGNORECASE,
)
_NAV_RE = re.compile(
    r"\b(?:walkthrough|strategy|guide|faq|contents|table of contents|menu|"
    r"boss|treasure|controls|equipment|weapon|armor)\b",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SourceSample:
    """One sampled piece projected to the intake LLM probe."""

    label: str
    piece_id: str
    order_index: int
    text: str


@dataclass(frozen=True)
class SourceProbe:
    """A compact, sampled representation of one raw source file."""

    source_alias: str
    path: str
    source_id_guess: str
    raw_text: str
    content_hash: str
    char_count: int
    line_count: int
    estimated_tokens: int
    stats: Mapping[str, int | str]
    samples: tuple[SourceSample, ...]


def source_id_slug(path: str | Path) -> str:
    """Create a deterministic source-id guess from a raw filename."""
    stem = Path(path).stem.casefold()
    slug = _SLUG_RE.sub("_", stem).strip("_")
    return slug or "source"


def unique_source_ids(paths: Iterable[str | Path]) -> dict[str, str]:
    """Return ``path -> source_id`` guesses, adding a hash suffix on collisions."""
    result: dict[str, str] = {}
    seen: dict[str, str] = {}
    for path in paths:
        path_text = str(path).replace("\\", "/")
        base = source_id_slug(path_text)
        if base not in seen:
            result[path_text] = base
            seen[base] = path_text
            continue
        suffix = content_hash(path_text)[:8]
        result[path_text] = f"{base}_{suffix}"
    return result


def build_source_probe(
    *,
    source_alias: str,
    path: str,
    raw_text: str,
    source_id_guess: str | None = None,
    source_piece_max_tokens: int = DEFAULT_SOURCE_PIECE_MAX_TOKENS,
    sample_token_budget: int = DEFAULT_SAMPLE_TOKEN_BUDGET,
) -> SourceProbe:
    """Build the sampled probe object for one raw source."""
    source_id = source_id_guess or source_id_slug(path)
    pieces = split_source(source_id, raw_text, source_piece_max_tokens)
    stats = _structural_stats(raw_text)
    samples = _sample_pieces(pieces, sample_token_budget=sample_token_budget)
    return SourceProbe(
        source_alias=source_alias,
        path=path.replace("\\", "/"),
        source_id_guess=source_id,
        raw_text=raw_text,
        content_hash=content_hash(raw_text),
        char_count=len(raw_text),
        line_count=raw_text.count("\n") + (1 if raw_text else 0),
        estimated_tokens=estimate_tokens(raw_text),
        stats=stats,
        samples=tuple(samples),
    )


def _structural_stats(text: str) -> dict[str, int | str]:
    lines = text.splitlines()
    qa_lines = sum(1 for line in lines if _QA_RE.search(line))
    q_lines = sum(1 for line in lines if re.match(r"^\s*(?:Q|Question)\s*[:：]", line, re.I))
    a_lines = sum(1 for line in lines if re.match(r"^\s*(?:A|Answer)\s*[:：]", line, re.I))
    speaker_lines = sum(1 for line in lines if _SPEAKER_RE.search(line))
    stage_lines = sum(1 for line in lines if _STAGE_RE.search(line))
    review_terms = len(_REVIEW_RE.findall(text))
    nav_terms = len(_NAV_RE.findall(text))
    cjk_chars = len(_CJK_RE.findall(text))
    latin_chars = len(_LATIN_RE.findall(text))
    language_hint = "unknown"
    if cjk_chars > latin_chars:
        language_hint = "zh"
    elif latin_chars > 0:
        language_hint = "en"
    return {
        "qa_lines": qa_lines,
        "qa_pairs": min(q_lines, a_lines),
        "speaker_colon": speaker_lines,
        "stage_direction": stage_lines,
        "review_terms": review_terms,
        "nav_terms": nav_terms,
        "cjk_chars": cjk_chars,
        "latin_chars": latin_chars,
        "language_hint": language_hint,
    }


def _sample_pieces(
    pieces: list[SourcePiece],
    *,
    sample_token_budget: int,
) -> list[SourceSample]:
    if not pieces or sample_token_budget <= 0:
        return []

    candidates: list[tuple[str, SourcePiece]] = []
    candidates.extend((f"head:{index + 1}", piece) for index, piece in enumerate(pieces[:2]))

    middle_start = max(0, len(pieces) // 2 - 1)
    candidates.extend(
        (f"middle:{index + 1}", piece)
        for index, piece in enumerate(pieces[middle_start : middle_start + 2])
    )
    tail = pieces[-2:] if len(pieces) > 2 else []
    candidates.extend((f"tail:{index + 1}", piece) for index, piece in enumerate(tail))

    signal_scorers = (
        ("signal:qa", lambda text: len(_QA_RE.findall(text))),
        ("signal:speaker_colon", lambda text: len(_SPEAKER_RE.findall(text))),
        ("signal:review_terms", lambda text: len(_REVIEW_RE.findall(text))),
        ("signal:nav_terms", lambda text: len(_NAV_RE.findall(text))),
    )
    for label, scorer in signal_scorers:
        scored = [(scorer(piece.text), piece.order_index, piece) for piece in pieces]
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored and scored[0][0] > 0:
            candidates.append((label, scored[0][2]))

    selected: list[SourceSample] = []
    seen_piece_ids: set[str] = set()
    used_tokens = 0
    for label, piece in candidates:
        if piece.piece_id in seen_piece_ids:
            continue
        text = piece.text.strip()
        if not text:
            continue
        remaining = sample_token_budget - used_tokens
        if remaining <= 0:
            break
        text = _trim_to_token_budget(text, remaining)
        token_count = estimate_tokens(text)
        if token_count <= 0:
            continue
        selected.append(
            SourceSample(
                label=label,
                piece_id=piece.piece_id,
                order_index=piece.order_index,
                text=text,
            )
        )
        seen_piece_ids.add(piece.piece_id)
        used_tokens += token_count
    return selected


def _trim_to_token_budget(text: str, budget: int) -> str:
    if estimate_tokens(text) <= budget:
        return text
    lo = 0
    hi = len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].rstrip()
        if estimate_tokens(candidate) <= budget:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best.rstrip()
