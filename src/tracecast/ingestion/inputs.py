"""Per-source injected inputs and stage budgets for Ingestion.

Everything in this module is operator/driver-supplied context, never hardcoded:

- :class:`IngestionBudgets` are read from ``RuntimeConfig.stage_budgets['ingestion']``
  (piece token ceiling, window token budget, soft piece-per-call cap).
- :class:`SourceProfile` is the per-source label constraint set, sourced from
  ``configs/source_profile_templates`` (``allowed_content_kinds`` and
  ``allowed_tags``). It constrains what the model may emit; it does not
  introduce facts.
- ``language`` is program-supplied (the model never authors it). Without a
  per-atom detector library, the default is the source-declared language; a
  real per-atom detector can be injected via :data:`LanguageDetector`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tracecast.domain import ContentKind, SourceSnapshot
from tracecast.project import ProjectConfig
from tracecast.storage import RuntimeConfig, content_hash

#: The configured route name for the atom-boundary DSL call (see runtime config).
OPERATION_ROUTE = "atom_boundary_dsl"
#: Stamped into ``Provenance.extractor_version`` for every atom this stage builds.
EXTRACTOR_VERSION = "atom_boundary_dsl@v1"

_BUDGET_STAGE = "ingestion"


def _require_positive_int(budgets: Mapping[str, Any], key: str) -> int:
    try:
        value = budgets[key]
    except KeyError:
        raise KeyError(
            f"runtime config stage_budgets[{_BUDGET_STAGE!r}] is missing {key!r}"
        ) from None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"stage_budgets[{_BUDGET_STAGE!r}][{key!r}] must be a positive int, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class IngestionBudgets:
    """Deterministic split / windowing budgets for one ingestion run."""

    source_piece_max_tokens: int
    atom_window_max_input_tokens: int
    pieces_per_call_soft_limit: int

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> IngestionBudgets:
        """Read the ingestion budgets from a runtime config (fail fast if absent)."""
        budgets = config.stage_budgets.get(_BUDGET_STAGE, {})
        return cls(
            source_piece_max_tokens=_require_positive_int(
                budgets, "source_piece_max_tokens"
            ),
            atom_window_max_input_tokens=_require_positive_int(
                budgets, "atom_window_max_input_tokens"
            ),
            pieces_per_call_soft_limit=_require_positive_int(budgets, "pieces_per_call_soft_limit"),
        )


@dataclass(frozen=True)
class SourceProfile:
    """Per-source label constraints injected from the source-profile templates.

    ``allowed_content_kinds`` and ``allowed_tags`` bound what the DSL may emit. A
    disallowed ``content_kind`` is a hard parse violation (the label is mandatory
    and not droppable); an unknown tag is dropped with a warning (a tag is not a
    fact source, so dropping one loses no evidence).
    """

    allowed_content_kinds: frozenset[ContentKind]
    allowed_tags: frozenset[str]

    @classmethod
    def from_template(cls, template: Mapping[str, Any]) -> SourceProfile:
        """Build from one entry of ``source_profile_templates['profiles']``."""
        kinds = template.get("allowed_content_kinds", ())
        tags = template.get("allowed_tags", ())
        return cls(
            allowed_content_kinds=frozenset(ContentKind(kind) for kind in kinds),
            allowed_tags=frozenset(str(tag) for tag in tags),
        )

    @classmethod
    def build(
        cls,
        *,
        allowed_content_kinds: Iterable[ContentKind | str],
        allowed_tags: Iterable[str] = (),
    ) -> SourceProfile:
        """Build directly from explicit allowed sets (test/driver convenience)."""
        return cls(
            allowed_content_kinds=frozenset(ContentKind(kind) for kind in allowed_content_kinds),
            allowed_tags=frozenset(str(tag) for tag in allowed_tags),
        )


#: ``(atom_content, source_language) -> language`` -- program-supplied per-atom
#: language. The default returns the source language; inject a real detector
#: (e.g. an ``eld`` wrapper) to upgrade to per-atom detection.
LanguageDetector = Callable[[str, str], str]


def source_language_detector(content: str, source_language: str) -> str:
    """Default detector: fall back to the source-declared language for every atom."""
    return source_language


@dataclass(frozen=True)
class SourceInput:
    """One source's full ingestion input bundle (driver-assembled).

    ``raw_text`` is the already-fetched source text (intake/adapter owns fetch and
    ``content_hash``); core ingestion only accepts a source that already declares
    its profile. ``language`` is the source-declared language for atom labelling.
    """

    snapshot: SourceSnapshot
    raw_text: str
    profile: SourceProfile
    language: str


def build_source_inputs(
    project: ProjectConfig,
    project_dir: str | Path,
    *,
    max_sources: int | None = None,
    limit_source_chars: int | None = None,
) -> list[SourceInput]:
    """Resolve project source declarations into provenance-anchored inputs."""

    profiles = project.profile_templates["profiles"]
    project_dir = Path(project_dir)
    selected = project.sources if max_sources is None else project.sources[:max_sources]
    inputs: list[SourceInput] = []
    for source in selected:
        template = profiles[source.profile]
        raw = (project_dir / source.path).read_text(encoding="utf-8")
        if limit_source_chars is not None:
            raw = raw[:limit_source_chars]
        actual_hash = content_hash(raw)
        snapshot_hash = source.content_hash or actual_hash
        if limit_source_chars is not None:
            snapshot_hash = actual_hash
        metadata = dict(source.metadata or {})
        if source.note:
            metadata["note"] = source.note
        inputs.append(
            SourceInput(
                snapshot=SourceSnapshot(
                    source_id=source.source_id,
                    source_type=source.source_type or "local_text",
                    source_class=source.source_class
                    or template["default_source_class"],
                    trust_tier=source.trust_tier or template["default_trust_tier"],
                    path=source.path,
                    title=source.title or source.source_id,
                    content_hash=snapshot_hash,
                    metadata=metadata,
                ),
                raw_text=raw,
                profile=SourceProfile.from_template(template),
                language=source.language,
            )
        )
    return inputs
