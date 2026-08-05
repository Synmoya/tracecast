"""Controlled vocabularies for Tracecast domain records.

Only *system-owned* closed vocabularies are modelled as enums here -- sets the
pipeline itself controls, where adding a value is a deliberate code change.

Deliberately **not** enums:

- ``source_type`` / ``source_class``: owned by the extensible
  ``source_profile_templates`` config, so they are open validated strings on the
  records; freezing them would reject valid future source profiles.
- ``edge_type`` and edge endpoint roles: owned by the planning edge protocol and
  kept as open strings.
- ``narrative_function`` and ``tags``: explicitly volatile/evolving labels that
  are excluded from stable ids; kept as open strings.
- audit ``level`` / ``kind`` / ``stage``: the audit schema is ``review`` status
  (not frozen in v1), so they are open strings.
"""

from __future__ import annotations

from enum import StrEnum


class ContentKind(StrEnum):
    """Semantic kind of a :class:`MaterialAtom`.

    Union of the per-profile ``allowed_content_kinds`` in the source-profile
    templates. Excluded from stable id derivation (volatile semantic label).
    """

    dialogue = "dialogue"
    narration = "narration"
    assertion = "assertion"
    opinion = "opinion"


class TrustTier(StrEnum):
    """Source trust ranking (system-wide)."""

    canonical = "canonical"
    high = "high"
    low = "low"


class Role(StrEnum):
    """Node-level presentation role assigned during Selection."""

    core = "core"
    bridge = "bridge"
    context = "context"
    color = "color"


class Treatment(StrEnum):
    """How an accepted node's material should be rendered."""

    quote = "quote"
    paraphrase = "paraphrase"
    summarize = "summarize"


class NodeEditOperation(StrEnum):
    """Per-material operation in Node Edit.

    ``RenderContract`` materials only ever carry ``keep``/``condense`` (``omit``
    materials are excluded from the contract); that narrowing is enforced on the
    render material record, not by a separate enum.
    """

    keep = "keep"
    condense = "condense"
    omit = "omit"


class SelectionStatus(StrEnum):
    """Whether a candidate made it onto the selected path."""

    accepted = "accepted"
    not_selected = "not_selected"


class CostKind(StrEnum):
    """Provenance of a cost figure on an LLM call record.

    v1 only ever produces ``configured_estimate``; ``provider_invoice`` exists so
    a real invoice is never silently conflated with an estimate.
    """

    configured_estimate = "configured_estimate"
    provider_invoice = "provider_invoice"


class ProviderStatus(StrEnum):
    """Transport/provider outcome of one provider attempt.

    Kept separate from business parse status. The LLM boundary may extend the
    value set as transport reporting evolves.
    """

    success = "success"
    error = "error"
    timeout = "timeout"
