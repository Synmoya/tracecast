"""Optional preference-corpus to Host compiler."""

from __future__ import annotations

from .dsl import (
    HostBuildParseError,
    parse_examples_response,
    parse_profile_response,
    render_examples_user_prompt,
    render_profile_user_prompt,
    selected_example_rows,
)
from .models import (
    DistilledHostProfile,
    HostBuildCandidate,
    HostBuildInputRecord,
    HostBuildManifest,
    HostBuildResult,
    HostBuildSelectedExample,
)
from .pipeline import (
    load_host_build_prompts,
    profile_sample_rows,
    resolve_host_build_control_prompt,
    run_host_build,
)
from .sampling import (
    DEFAULT_PIECE_MAX_TOKENS,
    DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET,
    PreferenceDocument,
    PreferencePiece,
    PreparedCorpus,
    build_position_pools,
    build_profile_sample,
    expand_input_paths,
    is_bcp47_language_tag,
    language_script_family,
    prepare_corpus,
    script_counts,
    select_style_examples,
    split_preference_spans,
    trim_platform_boilerplate,
)

__all__ = [
    "DEFAULT_PIECE_MAX_TOKENS",
    "DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET",
    "DistilledHostProfile",
    "HostBuildCandidate",
    "HostBuildInputRecord",
    "HostBuildManifest",
    "HostBuildParseError",
    "HostBuildResult",
    "HostBuildSelectedExample",
    "PreferenceDocument",
    "PreferencePiece",
    "PreparedCorpus",
    "build_position_pools",
    "build_profile_sample",
    "expand_input_paths",
    "is_bcp47_language_tag",
    "language_script_family",
    "load_host_build_prompts",
    "parse_examples_response",
    "parse_profile_response",
    "prepare_corpus",
    "profile_sample_rows",
    "render_examples_user_prompt",
    "render_profile_user_prompt",
    "resolve_host_build_control_prompt",
    "run_host_build",
    "script_counts",
    "select_style_examples",
    "selected_example_rows",
    "split_preference_spans",
    "trim_platform_boilerplate",
]
