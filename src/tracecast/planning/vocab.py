"""Protocol vocabularies, protocol versions, and the stage diagnostic helper.

These are *protocol-owned* controlled vocabularies, not source/project content --
adding a value is a deliberate change to the planning protocol, never a
project-specific tweak. ``narrative_function`` is an open string
on the domain record (volatile labels excluded from stable ids); the set here is
the v1 *starting* vocabulary used to route unknown values to review diagnostics,
not to hard-reject them.
"""

from __future__ import annotations

from typing import Any

from tracecast.prompt_components import load_planning_protocol_catalog

# Protocol version strings are no longer code constants: each route's version is
# read from the ``<!-- version: -->`` header of its
# ``prompt/<stage>/<route>.system.{locale}.md`` file
# (see ``tracecast.prompts.load_system_prompt``) and used for both the call's
# ``prompt_version`` and the artifact's ``*_protocol`` metadata, so the version
# stamp and the prompt bytes stay same-source.

# operation_route keys looked up in RuntimeConfig.routes (see runtime config).
PLANNER_ROUTE = "planner_graph_dsl"
COMPRESS_ROUTE = "planner_node_compress_dsl"
SELECTOR_ROUTE = "selector_path_dsl"
SELECTOR_DIRECTIVE_ROUTE = "selector_directive_dsl"

# Diagnostic stage labels (kept distinct so Validation can split the two stages).
STAGE_PLANNING = "planning"
STAGE_SELECTION = "selection"

_PLANNING_PROTOCOL_CATALOG = load_planning_protocol_catalog()
PLANNING_PROTOCOL_VOCAB_VERSION = _PLANNING_PROTOCOL_CATALOG.version
NARRATIVE_FUNCTION_DESCRIPTIONS = (
    _PLANNING_PROTOCOL_CATALOG.narrative_function_descriptions
)

# Recognized ``narrative_function`` values. Unknown values are stored verbatim
# but surfaced to review.
ALLOWED_NARRATIVE_FUNCTIONS: frozenset[str] = frozenset(
    name for name, _description in NARRATIVE_FUNCTION_DESCRIPTIONS
)

# edge_type -> required endpoint role keys.
# Directional edges keep their oriented role pair; symmetric edges use a/b and
# are normalized/deduped by code.
DIRECTIONAL_EDGE_ROLES: dict[str, tuple[str, str]] = {
    "setup_payoff": ("setup", "payoff"),
    "causal_link": ("cause", "effect"),
}
SYMMETRIC_EDGE_ROLES: tuple[str, str] = ("a", "b")
SYMMETRIC_EDGE_TYPES: frozenset[str] = frozenset(
    {"entity_thread", "duplicates", "complements", "contrasts"}
)

EDGE_TYPE_DESCRIPTIONS = _PLANNING_PROTOCOL_CATALOG.edge_type_descriptions

_EDGE_DESCRIPTION_KEYS = frozenset(name for name, _items in EDGE_TYPE_DESCRIPTIONS)
_STRUCTURAL_EDGE_KEYS = frozenset(DIRECTIONAL_EDGE_ROLES) | SYMMETRIC_EDGE_TYPES
if _EDGE_DESCRIPTION_KEYS != _STRUCTURAL_EDGE_KEYS:
    raise ValueError(
        "planning edge description keys must match structural edge types: "
        f"{sorted(_EDGE_DESCRIPTION_KEYS)} != {sorted(_STRUCTURAL_EDGE_KEYS)}"
    )


def required_roles(edge_type: str) -> frozenset[str] | None:
    """Allowed endpoint role keys for ``edge_type``, or ``None`` if unknown."""
    if edge_type in DIRECTIONAL_EDGE_ROLES:
        return frozenset(DIRECTIONAL_EDGE_ROLES[edge_type])
    if edge_type in SYMMETRIC_EDGE_TYPES:
        return frozenset(SYMMETRIC_EDGE_ROLES)
    return None


def diagnostic(stage: str, kind: str, message: str, **context: Any) -> dict[str, Any]:
    """Build one stage-local diagnostic (review-level, never a main-chain gate).

    Builders compile these dictionaries into explicit ``AuditReportItem`` output;
    they are never mirrored into graph/plan metadata. ``context`` carries trace
    fields (line, alias, candidate_id, window_id, call_id); ``None`` values are
    dropped.
    """
    item: dict[str, Any] = {
        "stage": stage,
        "kind": kind,
        "level": "review",
        "message": message,
    }
    for key, value in context.items():
        if value is not None:
            item[key] = value
    return item
