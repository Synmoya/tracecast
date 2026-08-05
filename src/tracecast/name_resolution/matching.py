"""Exact, deterministic surface matching for pre-render name replacement."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator


def iter_surface_spans(text: str, surface: str) -> Iterator[tuple[int, int]]:
    """Yield exact non-overlapping occurrences accepted by Latin word boundaries."""

    if not surface:
        return
    start = 0
    while True:
        index = text.find(surface, start)
        if index < 0:
            return
        end = index + len(surface)
        if _has_valid_boundaries(text, index, end, surface):
            yield index, end
            start = end
        else:
            start = index + 1


def contains_surface(text: str, surface: str) -> bool:
    return next(iter_surface_spans(text, surface), None) is not None


def is_cjk_output_language(output_language: str) -> bool:
    primary = output_language.strip().replace("_", "-").split("-", 1)[0].lower()
    return primary in {"zh", "ja", "ko"}


def stable_name_script_mismatch(stable_name: str, output_language: str) -> bool:
    if not is_cjk_output_language(output_language):
        return False
    return not any(_is_cjk(character) for character in stable_name)


def _has_valid_boundaries(text: str, start: int, end: int, surface: str) -> bool:
    if not any(_is_latin(character) or character.isdigit() for character in surface):
        return True
    if start > 0 and _is_latin_numeric_word_character(text[start - 1]):
        return False
    return end >= len(text) or not _is_latin_numeric_word_character(text[end])


def _is_latin(character: str) -> bool:
    return unicodedata.name(character, "").startswith("LATIN ")


def _is_latin_numeric_word_character(character: str) -> bool:
    return character == "_" or character.isdigit() or _is_latin(character)


def _is_cjk(character: str) -> bool:
    name = unicodedata.name(character, "")
    return name.startswith(("CJK ", "HIRAGANA ", "KATAKANA ", "HANGUL "))
