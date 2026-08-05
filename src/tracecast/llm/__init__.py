"""LLM provider boundary and call ledger.

The single place a real model call happens. ``call_llm`` is the stages' one call
point; everything else here supports request canonicalization and configured cost
estimation. No business prompt construction or DSL parsing lives in this package.
"""

from __future__ import annotations

from .client import (
    CHAT_COMPLETIONS_PATH,
    LlmTransportError,
    build_request_envelope,
    call_llm,
)
from .cost import (
    DEFAULT_COST_PROFILES,
    CostProfile,
    Usage,
    estimate_cost,
    extract_usage,
    resolve_cost_profile,
)

__all__ = [
    "call_llm",
    "LlmTransportError",
    "build_request_envelope",
    "CHAT_COMPLETIONS_PATH",
    "CostProfile",
    "DEFAULT_COST_PROFILES",
    "Usage",
    "extract_usage",
    "resolve_cost_profile",
    "estimate_cost",
]
