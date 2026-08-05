"""Atom-boundary DSL: projection (prompt) and parsing (no fact authoring).

The model only ever marks *boundaries* and assigns *discrete labels*. It writes
call-local ``p####`` piece aliases (from the per-window manifest), a
``content_kind`` and controlled ``tags``. It never emits atom ids, atom content,
provenance or confidence.

Grammar (one atom per ``A`` row)::

    A | <piece_range> | <content_kind> | <tags>

``<piece_range>`` is an inclusive, contiguous alias range ``p0001`` or
``p0001-p0003``; ``<tags>`` is a comma list (possibly empty).

Parsing expands ``p####`` aliases back to global ``piece_id``s via the manifest;
the resulting :class:`AtomSpec` carries *only* piece refs and labels -- no text --
so atom content is rebuilt from the source pieces by code, never from this
response. Any hard grammar/partition violation raises :class:`WindowParseError`
(fail-and-report); unknown tags degrade to warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    PIECE_ALIASES,
    AliasManifest,
    ContentKind,
    SourcePiece,
)
from tracecast.prompt_components import (
    load_ingestion_content_kind_catalog,
    load_ingestion_tag_catalog,
)
from tracecast.prompts import substitute_prompt_blocks

from .inputs import SourceProfile
from .results import (
    LEVEL_WARNING,
    UNKNOWN_TAG,
    IngestionItem,
)


@dataclass(frozen=True)
class AtomSpec:
    """A parsed atom row: ordered global piece refs plus discrete labels only."""

    piece_refs: tuple[str, ...]
    content_kind: ContentKind
    tags: tuple[str, ...]


@dataclass
class ParsedWindow:
    """Successful parse of one window's DSL response."""

    atoms: list[AtomSpec] = field(default_factory=list)
    warnings: list[IngestionItem] = field(default_factory=list)


@dataclass
class PartialParsedWindow:
    """Best-effort atom parse plus classified diagnostics for a failed window.

    Only a partition failure across otherwise protocol-valid rows may expose
    ``repair_spans``.  A malformed row or invalid cell is a protocol failure;
    omitting that row can also create an apparent gap, but that gap must not
    authorize the model to choose new atom boundaries.
    """

    atoms: list[AtomSpec] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[IngestionItem] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    protocol_violations: list[str] = field(default_factory=list)
    partition_violations: list[str] = field(default_factory=list)
    repair_spans: list[tuple[int, int]] = field(default_factory=list)


class WindowParseError(Exception):
    """Hard, auditable DSL/partition failure for one window.

    Carries the specific violations so the orchestrator can record them and
    surface the affected pieces -- never silently repair or fabricate.
    """

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations) or "unparseable DSL response")
        self.violations = violations


class _RowError(Exception):
    """Internal: a single malformed ``A`` row (promoted to a window violation)."""


# --- projection ------------------------------------------------------------


def build_manifest(piece_ids: list[str]) -> AliasManifest:
    """Mint ``p0001..`` aliases in window order for one call."""
    return AliasManifest.number(PIECE_ALIASES, piece_ids)


def project_window(
    *,
    manifest: AliasManifest,
    piece_by_id: dict[str, SourcePiece],
    profile: SourceProfile,
    system_prompt: str,
    user_prompt_template: str,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> list[dict[str, str]]:
    """Build the OpenAI-style ``messages`` for one window's atom-DSL call.

    Prompt prose is owned by files. This function only fills dynamic values into
    the user template and serializes source pieces behind call-local aliases.
    """
    user_prompt = render_window_user_prompt(
        template=user_prompt_template,
        manifest=manifest,
        piece_by_id=piece_by_id,
        profile=profile,
        control_prompt_locale=control_prompt_locale,
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def render_window_user_prompt(
    *,
    template: str,
    manifest: AliasManifest,
    piece_by_id: dict[str, SourcePiece],
    profile: SourceProfile,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Fill the atom user-prompt template with this window's dynamic data."""
    return substitute_prompt_blocks(
        template,
        build_window_blocks(
            manifest=manifest,
            piece_by_id=piece_by_id,
            profile=profile,
            control_prompt_locale=control_prompt_locale,
        ),
        prompt_name="atom",
    )


def build_window_blocks(
    *,
    manifest: AliasManifest,
    piece_by_id: dict[str, SourcePiece],
    profile: SourceProfile,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Return the exact inspectable blocks used by one ingestion window call."""
    locale = coerce_control_prompt_locale(control_prompt_locale)
    content_kind_catalog = load_ingestion_content_kind_catalog(locale)
    tag_catalog = load_ingestion_tag_catalog(locale)
    separator = "：" if locale is ControlPromptLocale.zh else ": "
    return {
        "alias_range": _alias_range(manifest, locale),
        "allowed_content_kinds": "\n".join(
            f"- {kind.value}"
            for kind in sorted(profile.allowed_content_kinds, key=lambda kind: kind.value)
        ),
        "content_kind_descriptions": "\n".join(
            f"- {kind.value}{separator}{content_kind_catalog.descriptions[kind]}"
            for kind in sorted(profile.allowed_content_kinds, key=lambda kind: kind.value)
        ),
        "allowed_tags": "\n".join(f"- {tag}" for tag in sorted(profile.allowed_tags)),
        "tag_descriptions": "\n".join(
            f"- {tag}{separator}{tag_catalog.descriptions[tag]}"
            for tag in sorted(profile.allowed_tags)
        ),
        "piece_lines": "\n".join(
            f"{alias} | {_project_text(piece_by_id[piece_id].text)}"
            for alias, piece_id in manifest.items()
        ),
    }


def _alias_range(manifest: AliasManifest, locale: ControlPromptLocale) -> str:
    aliases = [alias for alias, _piece_id in manifest.items()]
    if not aliases:
        return "(empty)"
    connector = " 到 " if locale is ControlPromptLocale.zh else " through "
    return f"{aliases[0]}{connector}{aliases[-1]}"


def _project_text(text: str) -> str:
    """Keep source line boundaries visible inside the compact one-line projection."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return " [NL] ".join(" ".join(line.split()) for line in normalized.split("\n"))


# --- parsing ---------------------------------------------------------------


def parse_window_response(
    response_text: str,
    *,
    manifest: AliasManifest,
    profile: SourceProfile,
    window_piece_ids: list[str],
    source_id: str,
    window_id: str,
) -> ParsedWindow:
    """Parse a window response; raise :class:`WindowParseError` on hard violations."""
    partial = parse_window_response_partial(
        response_text,
        manifest=manifest,
        profile=profile,
        window_piece_ids=window_piece_ids,
        source_id=source_id,
        window_id=window_id,
    )
    if partial.violations:
        raise WindowParseError(partial.violations)
    return ParsedWindow(
        atoms=partial.atoms,
        warnings=partial.warnings,
    )


def parse_window_response_partial(
    response_text: str,
    *,
    manifest: AliasManifest,
    profile: SourceProfile,
    window_piece_ids: list[str],
    source_id: str,
    window_id: str,
) -> PartialParsedWindow:
    """Parse rows and expose repair spans only for pure partition failures."""
    lines = _strip_fences(response_text.splitlines())

    protocol_violations: list[str] = []
    warnings: list[IngestionItem] = []
    specs: list[AtomSpec] = []
    ranges: list[tuple[int, int]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            spec, span = _parse_atom_line(
                line,
                manifest=manifest,
                profile=profile,
                warnings=warnings,
                source_id=source_id,
                window_id=window_id,
            )
        except _RowError as exc:
            protocol_violations.append(str(exc))
            continue
        specs.append(spec)
        ranges.append(span)

    # An empty/format-only response is not a topology judgement.  Treating it as
    # a whole-window gap would incorrectly authorize a semantic boundary repair.
    if not specs and not protocol_violations:
        protocol_violations.append("no atom rows in model response")

    partition_violations, partition_spans = _check_partition(ranges, window_piece_ids)
    violations = [*protocol_violations, *partition_violations]

    return PartialParsedWindow(
        atoms=specs,
        spans=ranges,
        warnings=warnings,
        violations=violations,
        protocol_violations=protocol_violations,
        partition_violations=partition_violations,
        repair_spans=partition_spans if not protocol_violations else [],
    )


def _strip_fences(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.strip().startswith("```")]


def _parse_atom_line(
    line: str,
    *,
    manifest: AliasManifest,
    profile: SourceProfile,
    warnings: list[IngestionItem],
    source_id: str,
    window_id: str,
) -> tuple[AtomSpec, tuple[int, int]]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 4 or parts[0] != "A":
        raise _RowError(f"malformed atom row: {line!r}")
    start_num, end_num, piece_refs = _resolve_range(parts[1], manifest)
    content_kind = _resolve_kind(parts[2], profile)

    tags = _resolve_tags(
        parts[3],
        profile,
        warnings,
        source_id=source_id,
        window_id=window_id,
        piece_refs=piece_refs,
    )
    spec = AtomSpec(
        piece_refs=tuple(piece_refs),
        content_kind=content_kind,
        tags=tags,
    )
    return spec, (start_num, end_num)


def _parse_range_numbers(raw: str) -> tuple[int, int]:
    tokens = raw.split("-")
    if len(tokens) == 1:
        start_alias = end_alias = tokens[0].strip()
    elif len(tokens) == 2:
        start_alias, end_alias = tokens[0].strip(), tokens[1].strip()
    else:
        raise _RowError(f"malformed piece range: {raw!r}")
    try:
        start_num = PIECE_ALIASES.parse(start_alias)
        end_num = PIECE_ALIASES.parse(end_alias)
    except ValueError as exc:
        raise _RowError(f"malformed piece alias in range {raw!r}: {exc}") from None
    if start_num > end_num:
        raise _RowError(f"reversed piece range: {raw!r}")
    return start_num, end_num


def _resolve_range(raw: str, manifest: AliasManifest) -> tuple[int, int, list[str]]:
    start_num, end_num = _parse_range_numbers(raw)
    piece_refs: list[str] = []
    for number in range(start_num, end_num + 1):
        alias = PIECE_ALIASES.format(number)
        try:
            piece_refs.append(manifest.resolve(alias))
        except (KeyError, ValueError):
            raise _RowError(f"unknown piece alias {alias!r} (not in this window)") from None
    return start_num, end_num, piece_refs


def _resolve_kind(raw: str, profile: SourceProfile) -> ContentKind:
    try:
        kind = ContentKind(raw)
    except ValueError:
        raise _RowError(f"unknown content_kind {raw!r}") from None
    if kind not in profile.allowed_content_kinds:
        raise _RowError(f"content_kind {kind.value!r} not allowed for this source")
    return kind


def _resolve_tags(
    tags_raw: str,
    profile: SourceProfile,
    warnings: list[IngestionItem],
    *,
    source_id: str,
    window_id: str,
    piece_refs: list[str],
) -> tuple[str, ...]:
    kept: list[str] = []
    for token in tags_raw.split(","):
        tag = token.strip()
        if not tag:
            continue
        if tag in profile.allowed_tags:
            kept.append(tag)
        else:
            warnings.append(
                IngestionItem(
                    source_id=source_id,
                    kind=UNKNOWN_TAG,
                    level=LEVEL_WARNING,
                    message=f"dropped tag {tag!r} (outside controlled set)",
                    window_id=window_id,
                    piece_refs=tuple(piece_refs),
                    metadata={
                        "failure_class": "degradation",
                        "reason_code": "unknown_tag_dropped",
                        "recovery": "drop_optional_label",
                    },
                )
            )
    return tuple(kept)


def _check_partition(
    ranges: list[tuple[int, int]], window_piece_ids: list[str]
) -> tuple[list[str], list[tuple[int, int]]]:
    """Verify atom ranges tile ``[1..N]`` exactly (no gap, no overlap)."""
    piece_count = len(window_piece_ids)

    def piece_label(number: int) -> str:
        return window_piece_ids[number - 1]

    if not ranges:
        return [f"no atoms cover window pieces {window_piece_ids}"], [(1, piece_count)]

    violations: list[str] = []
    repair_spans: list[tuple[int, int]] = []
    expected = 1
    for start, end in sorted(ranges):
        if start > expected:
            missing = [piece_label(i) for i in range(expected, start)]
            violations.append(f"gap: pieces {missing} not covered by any atom")
            repair_spans.append((expected, start - 1))
        elif start < expected:
            overlap = [piece_label(i) for i in range(start, min(end, expected - 1) + 1)]
            violations.append(f"overlap: pieces {overlap} covered by more than one atom")
            repair_spans.append((start, min(end, expected - 1)))
        expected = max(expected, end + 1)
    if expected <= piece_count:
        missing = [piece_label(i) for i in range(expected, piece_count + 1)]
        violations.append(f"gap: trailing pieces {missing} not covered by any atom")
        repair_spans.append((expected, piece_count))
    return violations, repair_spans
