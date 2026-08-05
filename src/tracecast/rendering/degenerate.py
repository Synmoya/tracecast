"""Detect degenerate repeated-character output from the render provider call.

A known LLM failure mode is repetition collapse: the model gets stuck emitting
the same short unit (often a single character) until it hits the route's
``max_output_tokens`` cap. This is a transport-level success (2xx, non-empty
body) that ``llm/client.py`` has no reason to retry, so the stage itself has to
catch it -- see ``renderer.py``'s per-chunk retry loop.
"""

from __future__ import annotations

_MAX_PERIOD = 8


def longest_repetition_run(text: str, max_period: int = _MAX_PERIOD) -> int:
    """Length of the longest contiguous run built from a repeating short unit.

    For each candidate unit length (period) ``p`` in ``1..max_period``, finds
    the longest stretch where ``text[i] == text[i + p]`` holds for consecutive
    ``i``, which is exactly a run of the ``p``-character unit repeating in
    place. Runs across all periods are compared and the longest wins. Linear
    per period (no regex backtracking), so it stays cheap even on the
    pathological input it exists to catch.
    """
    n = len(text)
    best = 0
    for period in range(1, max_period + 1):
        if n <= period:
            break
        current = 0
        best_for_period = 0
        for i in range(n - period):
            if text[i] == text[i + period]:
                current += 1
                if current > best_for_period:
                    best_for_period = current
            else:
                current = 0
        if best_for_period:
            best = max(best, best_for_period + period)
    return best


def is_degenerate_repetition(text: str, *, min_run: int = 30) -> bool:
    """True if ``text`` contains a short repeated unit run at least ``min_run`` long.

    ``min_run=30`` sits well above any plausible stylistic repeat in narrated
    prose (onomatopoeia rarely exceeds ~10 characters in a row) and far below
    observed repetition-collapse failures (tens of thousands of characters).
    """
    return longest_repetition_run(text) >= min_run
