"""Configured cost estimation for one provider attempt.

v1 cost is always a ``configured_estimate`` (never a provider invoice): the
estimate is computed from the response usage and a per-profile rate table.

The cost inputs are provider, model, cost-profile key, uncached/cached input
tokens, and reasoning/visible output tokens; the operator owns the actual rates.
No provider prices or zero-valued placeholder profiles are bundled. An optional
``cost_profiles`` section in runtime config supplies named rates. The resolved
profile name and rate source are recorded so Console can say “rate not configured”
instead of presenting a computed zero as a real cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Where the effective rates came from; recorded on the call-record metadata.
#: Kept under the historical exported name for API compatibility.
RATES_SOURCE_DEFAULT = "unconfigured"
RATES_SOURCE_CONFIG = "runtime_config"

#: Rates are quoted per this many tokens (operator fills real prices in this unit).
_TOKENS_PER_RATE_UNIT = 1_000_000


@dataclass(frozen=True)
class CostProfile:
    """Per-token rates for one ``cost_profile`` key.

    Rates are in operator-defined currency units per 1,000,000 tokens. Real prices
    are operator-owned and must be supplied via a ``cost_profiles`` section in the
    runtime config, never hardcoded here.
    Input is priced uncached/cached separately so a cache hit is never charged at
    the uncached rate; output is priced as a single rate over the full output
    token count (which already includes reasoning tokens).
    """

    uncached_input_per_million: float = 0.0
    cached_input_per_million: float = 0.0
    output_per_million: float = 0.0


#: No provider prices are bundled. Operators may add named profiles to runtime config.
DEFAULT_COST_PROFILES: dict[str, CostProfile] = {}


@dataclass(frozen=True)
class Usage:
    """Token counts extracted from a provider response; every field optional."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    visible_output_tokens: int | None = None


def _opt_int(value: Any) -> int | None:
    # bool is an int subclass; reject it so a stray True/False is not a token count.
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def extract_usage(response_json: Mapping[str, Any]) -> Usage:
    """Pull OpenAI-compatible usage out of a chat-completions response.

    Defensive: any missing field stays ``None`` rather than being fabricated.
    The cache split accepts both top-level ``prompt_cache_hit_tokens`` /
    ``prompt_cache_miss_tokens`` and the OpenAI-compatible
    ``prompt_tokens_details.cached_tokens`` shape. When the gateway reports no
    cache hit the full input is treated as
    uncached (so a cache hit is never silently priced as uncached -- there is
    none). Derived splits are clamped to ``>= 0`` to satisfy the record schema.
    """
    usage = response_json.get("usage")
    if not isinstance(usage, Mapping):
        return Usage()

    input_tokens = _opt_int(usage.get("prompt_tokens"))
    output_tokens = _opt_int(usage.get("completion_tokens"))

    cached = _opt_int(usage.get("prompt_cache_hit_tokens"))
    if cached is None:
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, Mapping):
            cached = _opt_int(prompt_details.get("cached_tokens"))

    uncached = _opt_int(usage.get("prompt_cache_miss_tokens"))
    if uncached is None and input_tokens is not None:
        uncached = max(input_tokens - cached, 0) if cached is not None else input_tokens

    reasoning = None
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        reasoning = _opt_int(completion_details.get("reasoning_tokens"))

    visible = None
    if output_tokens is not None:
        visible = max(output_tokens - reasoning, 0) if reasoning is not None else output_tokens

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        uncached_input_tokens=uncached,
        reasoning_output_tokens=reasoning,
        visible_output_tokens=visible,
    )


def resolve_cost_profile(
    profile_key: str | None, config: Any = None
) -> tuple[CostProfile, str]:
    """Resolve the effective ``CostProfile`` and its rate source.

    An operator-supplied ``cost_profiles`` mapping on the runtime config wins over
    the (normally empty) built-in table. Returns ``(profile, rates_source)`` so the
    source can be recorded on the call record.
    """
    if profile_key is not None and config is not None:
        overrides = getattr(config, "cost_profiles", None)
        if isinstance(overrides, Mapping):
            raw = overrides.get(profile_key)
            if isinstance(raw, Mapping):
                return _profile_from_mapping(raw), RATES_SOURCE_CONFIG
    if profile_key is not None and profile_key in DEFAULT_COST_PROFILES:
        return DEFAULT_COST_PROFILES[profile_key], RATES_SOURCE_DEFAULT
    return CostProfile(), RATES_SOURCE_DEFAULT


def _profile_from_mapping(raw: Mapping[str, Any]) -> CostProfile:
    return CostProfile(
        uncached_input_per_million=float(raw.get("uncached_input_per_million", 0.0)),
        cached_input_per_million=float(raw.get("cached_input_per_million", 0.0)),
        output_per_million=float(raw.get("output_per_million", 0.0)),
    )


def estimate_cost(usage: Usage, profile: CostProfile) -> float:
    """Configured cost estimate from usage and a rate profile.

    Prices uncached and cached input separately (never a cache hit at the
    uncached rate) and prices the full ``output_tokens`` -- which includes
    reasoning tokens -- so reasoning output is never dropped. With the default
    unconfigured rates this returns exactly ``0.0``; callers must inspect the
    recorded rate source before displaying that value as a cost.
    """
    uncached = usage.uncached_input_tokens or 0
    cached = usage.cached_input_tokens or 0
    output = usage.output_tokens or 0
    total = (
        uncached * profile.uncached_input_per_million
        + cached * profile.cached_input_per_million
        + output * profile.output_per_million
    )
    return total / _TOKENS_PER_RATE_UNIT
