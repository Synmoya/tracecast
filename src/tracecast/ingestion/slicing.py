"""Deterministic source -> ``SourcePiece`` slicing (no LLM).

Slice-first: the whole source is cut into a global, deterministic, 0-based
``SourcePiece`` sequence *before* any windowing, so changing the window size
never moves a piece's identity.

The slicer tiles the source losslessly by character span: every character
belongs to exactly one piece and consecutive pieces are contiguous
(``piece[i].span_end == piece[i+1].span_start``). This is what lets a
``MaterialAtom``'s content be rebuilt as the verbatim source substring its
pieces cover -- the strongest form of "content comes from the source, not the
model". Breaks are preferred at paragraph > line > sentence-punctuation
> clause > whitespace boundaries under an estimated-token ceiling; no
semantics, no rewriting.
"""

from __future__ import annotations

import bisect
import re

from tracecast.domain import SourcePiece, estimate_tokens
from tracecast.domain import piece_id as make_piece_id

# Run of one-or-more blank lines: the end of such a run is a paragraph boundary.
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
# Sentence-ending punctuation (CJK + latin), used as a secondary break point.
_SENTENCE_PUNCT = "。．！？；….!?;"
_CLAUSE_PUNCT = "，、：,:"


def split_source(source_id: str, raw_text: str, max_tokens: int) -> list[SourcePiece]:
    """Slice ``raw_text`` into the source-global ``SourcePiece`` sequence."""
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    spans = _tile_spans(raw_text, max_tokens)
    line_starts = _line_start_offsets(raw_text)
    pieces: list[SourcePiece] = []
    for order_index, (start, end) in enumerate(spans):
        text = raw_text[start:end]
        pieces.append(
            SourcePiece(
                piece_id=make_piece_id(source_id, order_index),
                source_id=source_id,
                order_index=order_index,
                text=text,
                start_line=_line_of(line_starts, start),
                end_line=_line_of(line_starts, max(start, end - 1)),
                span_start=start,
                span_end=end,
                char_count=end - start,
            )
        )
    return pieces


def _tile_spans(text: str, max_tokens: int) -> list[tuple[int, int]]:
    """Contiguous ``(start, end)`` spans covering ``[0, len(text))`` losslessly.

    Empty / whitespace-only sources yield no pieces. A trailing whitespace-only
    remainder is merged into the previous piece so no piece is pure whitespace.
    """
    n = len(text)
    if not text.strip():
        return []
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < n:
        token_cap = _token_cap_end(text, pos, max_tokens)
        if token_cap == n:
            spans.append((pos, n))
            break
        cut = _best_break(text, pos, token_cap)
        spans.append((pos, cut))
        pos = cut
    # Fold any whitespace-only span into a neighbour (keeps tiling contiguous).
    return _merge_blank_spans(text, spans)


def _best_break(text: str, lo: int, hi: int) -> int:
    """Pick a natural break in ``(lo, hi]`` under the estimated-token cap."""
    window = text[lo:hi]
    para = None
    for match in _PARAGRAPH_BREAK.finditer(window):
        para = lo + match.end()
    if para is not None and para > lo:
        return para
    newline = window.rfind("\n")
    if newline != -1 and lo + newline + 1 > lo:
        return lo + newline + 1
    sentence = _rfind_any(window, _SENTENCE_PUNCT)
    if sentence != -1 and lo + sentence + 1 > lo:
        return lo + sentence + 1
    clause = _rfind_any(window, _CLAUSE_PUNCT)
    if clause != -1 and lo + clause + 1 > lo:
        return lo + clause + 1
    whitespace = _last_whitespace_end(window)
    if whitespace > 0:
        return lo + whitespace
    return hi


def _token_cap_end(text: str, start: int, max_tokens: int) -> int:
    """Return the furthest suffix end whose estimate stays within ``max_tokens``.

    ``estimate_tokens`` is monotonic but language-dependent, so a fixed
    characters-per-token multiplier cannot provide the cap. Exponential growth
    followed by binary search keeps this deterministic without a provider
    tokenizer or a full-source prefix table.
    """
    n = len(text)
    low = start
    high = min(n, start + max(64, max_tokens * 4))
    while high < n and estimate_tokens(text[start:high]) <= max_tokens:
        low = high
        high = min(n, start + (high - start) * 2)

    if high == n and estimate_tokens(text[start:high]) <= max_tokens:
        return n

    while low + 1 < high:
        middle = (low + high) // 2
        if estimate_tokens(text[start:middle]) <= max_tokens:
            low = middle
        else:
            high = middle
    return max(start + 1, low)


def _rfind_any(text: str, chars: str) -> int:
    best = -1
    for char in chars:
        best = max(best, text.rfind(char))
    return best


def _last_whitespace_end(text: str) -> int:
    for index in range(len(text) - 1, -1, -1):
        if text[index].isspace():
            return index + 1
    return -1


def _merge_blank_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if not text[start:end].strip() and merged:
            prev_start, _ = merged[-1]
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    # A leading whitespace-only span has no previous neighbour; fold it forward.
    if len(merged) >= 2 and not text[merged[0][0] : merged[0][1]].strip():
        first_start, _ = merged[0]
        _, second_end = merged[1]
        merged = [(first_start, second_end), *merged[2:]]
    return merged


def _line_start_offsets(text: str) -> list[int]:
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


def _line_of(line_starts: list[int], offset: int) -> int:
    """1-based line number containing ``offset``."""
    return bisect.bisect_right(line_starts, offset)
