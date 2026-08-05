"""Shared language-tag validation for project, Host, and Console boundaries."""

from __future__ import annotations

import re

_BCP47_LANGUAGE_TAG = re.compile(
    r"^(?:[A-Za-z]{2,8}"
    r"(?:-[A-Za-z]{4})?"
    r"(?:-(?:[A-Za-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[A-Za-z0-9]{5,8}|[0-9][A-Za-z0-9]{3}))*"
    r"(?:-[0-9A-WY-Za-wy-z](?:-[A-Za-z0-9]{2,8})+)*"
    r"(?:-x(?:-[A-Za-z0-9]{1,8})+)?"
    r"|x(?:-[A-Za-z0-9]{1,8})+)$"
)


def is_bcp47_language_tag(value: str) -> bool:
    """Return whether ``value`` is a trimmed, structurally valid BCP-47 tag."""

    return bool(value and value == value.strip() and _BCP47_LANGUAGE_TAG.fullmatch(value))


def require_bcp47_language_tag(value: str, *, field: str = "output_language") -> str:
    """Return ``value`` or raise a stable validation error for public boundaries."""

    if not is_bcp47_language_tag(value):
        raise ValueError(f"{field} must be a valid BCP-47 language tag")
    return value


__all__ = ["is_bcp47_language_tag", "require_bcp47_language_tag"]
