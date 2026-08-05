"""Deterministic ``SourcePiece`` -> ``SourceWindow`` packing (no LLM).

A window is a call-context boundary over a run of *consecutive* pieces. Windows
partition the piece sequence with no overlap and no gap -- every piece belongs to
exactly one window. Tuning the window size only regroups pieces into calls; it never
moves a piece's identity.

Packing is greedy under two budgets: a per-call input-token budget
(``atom_window_max_input_tokens``) and a soft cap on piece count
(``pieces_per_call_soft_limit``). A single piece larger than the token budget is
still emitted as its own window -- a piece is never split (its identity is fixed).
"""

from __future__ import annotations

from tracecast.domain import SourcePiece, SourceWindow, estimate_tokens
from tracecast.domain import window_id as make_window_id

# Rough per-piece projection overhead (the ``pNNNN | `` alias prefix + newline).
_PIECE_ALIAS_OVERHEAD_TOKENS = 4


def _piece_tokens(piece: SourcePiece) -> int:
    return estimate_tokens(piece.text) + _PIECE_ALIAS_OVERHEAD_TOKENS


def pack_windows(
    source_id: str,
    pieces: list[SourcePiece],
    *,
    max_input_tokens: int,
    pieces_per_call_soft_limit: int,
) -> list[SourceWindow]:
    """Group ``pieces`` (in order) into consecutive, gapless ``SourceWindow``s."""
    if max_input_tokens <= 0:
        raise ValueError("max_input_tokens must be positive")
    if pieces_per_call_soft_limit <= 0:
        raise ValueError("pieces_per_call_soft_limit must be positive")

    windows: list[SourceWindow] = []
    batch: list[SourcePiece] = []
    batch_tokens = 0
    order_index = 0

    def flush() -> None:
        nonlocal batch, batch_tokens, order_index
        if not batch:
            return
        piece_ids = [piece.piece_id for piece in batch]
        windows.append(
            SourceWindow(
                window_id=make_window_id(source_id, piece_ids),
                source_id=source_id,
                piece_ids=piece_ids,
                order_index=order_index,
                estimated_input_tokens=batch_tokens,
                metadata={
                    "max_input_tokens": max_input_tokens,
                    "pieces_per_call_soft_limit": pieces_per_call_soft_limit,
                },
            )
        )
        order_index += 1
        batch = []
        batch_tokens = 0

    for piece in pieces:
        tokens = _piece_tokens(piece)
        over_tokens = batch and (batch_tokens + tokens) > max_input_tokens
        over_count = len(batch) >= pieces_per_call_soft_limit
        if over_tokens or over_count:
            flush()
        batch.append(piece)
        batch_tokens += tokens
    flush()
    return windows
