"""CJK-aware token estimation.

A single, swappable heuristic shared by every stage's budget seam so token
accounting is consistent across the pipeline. It is intentionally a rough
estimate -- not a provider tokenizer -- so it never needs network or model
access. If a real tokenizer is later adopted, replace this one function and all
callers inherit it.

Heuristic: CJK characters count ~1 token each; remaining (latin/other)
non-whitespace characters are folded at ~4 characters per token.
"""

from __future__ import annotations

# Unicode ranges treated as "~1 token per character". Focused on the languages
# Tracecast actually handles rather than exhaustive CJK coverage.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xFF00, 0xFFEF),  # Halfwidth and Fullwidth Forms
)

_LATIN_CHARS_PER_TOKEN = 4


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` with a CJK-aware heuristic."""
    if not text:
        return 0
    cjk = 0
    latin = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        elif not char.isspace():
            latin += 1
    latin_tokens = (latin + _LATIN_CHARS_PER_TOKEN - 1) // _LATIN_CHARS_PER_TOKEN
    return cjk + latin_tokens
