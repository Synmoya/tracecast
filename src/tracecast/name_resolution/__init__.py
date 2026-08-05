"""Run-level pre-render proper-name table compilation."""

from .matching import (
    is_cjk_output_language,
    stable_name_script_mismatch,
)
from .planner import (
    DEFAULT_TABLE_MAX_INPUT_TOKENS,
    NAME_TABLE_ROUTE,
    NameResolutionError,
    NameResolutionInputEstimate,
    NameResolutionResult,
    build_name_resolution,
    estimate_name_resolution_inputs,
)
from .policy import (
    DEFAULT_NAME_RESOLUTION_MODE,
    NameResolutionMode,
    coerce_name_resolution_mode,
)
from .projection import (
    resolve_contract_names,
    resolve_fact_context_names,
    resolve_text_names,
)

__all__ = [
    "DEFAULT_TABLE_MAX_INPUT_TOKENS",
    "DEFAULT_NAME_RESOLUTION_MODE",
    "NAME_TABLE_ROUTE",
    "NameResolutionError",
    "NameResolutionInputEstimate",
    "NameResolutionMode",
    "NameResolutionResult",
    "build_name_resolution",
    "coerce_name_resolution_mode",
    "estimate_name_resolution_inputs",
    "resolve_contract_names",
    "resolve_fact_context_names",
    "resolve_text_names",
    "is_cjk_output_language",
    "stable_name_script_mismatch",
]
