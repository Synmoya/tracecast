"""Ingestion orchestration: source -> pieces -> windows -> atoms.

The single entry the driver calls. It performs the deterministic split and
windowing (no LLM), runs the atom-boundary DSL once per window through the
injected ``call`` seam (default :func:`tracecast.llm.call_llm`, route
``atom_boundary_dsl``), parses the response, and code-builds the artifacts. It
returns the built artifacts plus a stage-local report; the driver persists them
(``call_llm`` already persists its own raw payloads and ``LlmCallRecord``s).

Failure handling is deliberately narrow: only a connected partition-topology
failure across otherwise protocol-valid rows
may receive one localized repair call. Protocol/cell failures and disconnected
failure spans hard-fail the window without authorizing new atom boundaries. No
path silently fabricates filler atoms, and other windows' atoms are kept.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace

from tracecast.concurrency import route_max_concurrency, run_ordered_concurrently
from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    AliasManifest,
    LlmCallRecord,
    LlmWorkUnitRef,
    MaterialAtom,
    SourcePiece,
    SourceSnapshot,
)
from tracecast.llm import LlmTransportError, call_llm
from tracecast.prompt_components import (
    load_ingestion_content_kind_catalog,
    load_ingestion_tag_catalog,
)
from tracecast.prompts import (
    load_route_prompt,
    load_system_prompt,
    load_user_prompt,
    substitute_prompt_blocks,
)
from tracecast.storage import (
    MATERIAL_ATOMS,
    SOURCE_PIECES,
    SOURCE_SNAPSHOTS,
    SOURCE_WINDOWS,
    RunStore,
    RuntimeConfig,
    content_hash,
)

from .atoms import build_atom
from .dsl import (
    AtomSpec,
    ParsedWindow,
    PartialParsedWindow,
    build_manifest,
    parse_window_response_partial,
    project_window,
)
from .inputs import (
    OPERATION_ROUTE,
    IngestionBudgets,
    LanguageDetector,
    SourceInput,
    SourceProfile,
    source_language_detector,
)
from .results import (
    LEVEL_ERROR,
    LEVEL_WARNING,
    SOURCE_INTEGRITY_FAILED,
    SOURCE_PRECONDITION_FAILED,
    WINDOW_PARTITION_FAILED,
    WINDOW_PARTITION_REPAIRED,
    WINDOW_PROTOCOL_FAILED,
    WINDOW_TRANSPORT_FAILED,
    IngestionItem,
    IngestResult,
    SourceIngestResult,
)
from .slicing import split_source
from .windowing import pack_windows

#: The llm-call seam: same signature as :func:`tracecast.llm.call_llm`. Injectable
#: so tests can supply a fake provider with no network.
CallLlm = Callable[..., tuple[str, LlmCallRecord]]

_DEFAULT_PARTITION_REPAIR_ATTEMPTS = 1


@dataclass(frozen=True)
class _WindowCall:
    """Prepared atom-boundary call for one source window."""

    window_id: str
    window_piece_ids: list[str]
    manifest: AliasManifest
    messages: list[dict[str, str]]


@dataclass(frozen=True)
class _WindowResult:
    """Local result from one window call; merged by the caller in window order."""

    parsed: ParsedWindow | None
    report: list[IngestionItem]
    call_records: list[LlmCallRecord]


def _load_repair_prompt(
    config: RuntimeConfig,
    control_prompt_locale: ControlPromptLocale,
) -> tuple[str | None, str | None]:
    route = config.route(OPERATION_ROUTE)
    if route.repair_prompt is None:
        return None, None
    return load_route_prompt(
        OPERATION_ROUTE,
        config,
        "repair_prompt",
        control_prompt_locale=control_prompt_locale,
    )


def ingest_source(
    *,
    snapshot: SourceSnapshot,
    raw_text: str,
    profile: SourceProfile,
    source_language: str,
    config: RuntimeConfig,
    store: RunStore,
    budgets: IngestionBudgets | None = None,
    detect_language: LanguageDetector = source_language_detector,
    call: CallLlm = call_llm,
    partition_repair_attempts: int = _DEFAULT_PARTITION_REPAIR_ATTEMPTS,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> SourceIngestResult:
    """Ingest one source into pieces / windows / atoms plus audit reports."""
    if partition_repair_attempts not in {0, 1}:
        raise ValueError("partition_repair_attempts must be 0 or 1")
    budgets = budgets or IngestionBudgets.from_config(config)
    control_prompt_locale = coerce_control_prompt_locale(control_prompt_locale)
    source_id = snapshot.source_id
    report: list[IngestionItem] = []
    call_records: list[LlmCallRecord] = []

    if not _check_content_hash(snapshot, raw_text, report):
        return SourceIngestResult(
            snapshot=snapshot,
            items=report,
            call_records=call_records,
        )

    pieces = split_source(source_id, raw_text, budgets.source_piece_max_tokens)
    if not pieces:
        report.append(
            IngestionItem(
                source_id=source_id,
                kind=SOURCE_PRECONDITION_FAILED,
                level=LEVEL_ERROR,
                message="source produced no pieces (empty or whitespace-only)",
                usable=False,
                metadata={
                    "failure_class": "precondition",
                    "reason_code": "empty_source",
                    "recovery": "none",
                },
            )
        )
        return SourceIngestResult(
            snapshot=snapshot,
            items=report,
            call_records=call_records,
        )

    windows = pack_windows(
        source_id,
        pieces,
        max_input_tokens=budgets.atom_window_max_input_tokens,
        pieces_per_call_soft_limit=budgets.pieces_per_call_soft_limit,
    )
    piece_by_id = {piece.piece_id: piece for piece in pieces}
    system_prompt, system_version = load_system_prompt(
        OPERATION_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    user_prompt_template, user_version = load_user_prompt(
        OPERATION_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    content_kind_version = load_ingestion_content_kind_catalog(
        control_prompt_locale
    ).version
    tag_version = load_ingestion_tag_catalog(control_prompt_locale).version
    prompt_version = (
        f"{system_version}+{user_version}+{content_kind_version}+{tag_version}"
    )
    repair_prompt_template, repair_version = _load_repair_prompt(
        config,
        control_prompt_locale,
    )

    calls: list[_WindowCall] = []
    for window in windows:
        manifest = build_manifest(window.piece_ids)
        messages = project_window(
            manifest=manifest,
            piece_by_id=piece_by_id,
            profile=profile,
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            control_prompt_locale=control_prompt_locale,
        )
        calls.append(
            _WindowCall(
                window_id=window.window_id,
                window_piece_ids=window.piece_ids,
                manifest=manifest,
                messages=messages,
            )
        )

    pending: list[tuple[int, AtomSpec]] = []  # (first-piece order_index, spec)
    for result in _run_windows(
        calls,
        profile=profile,
        source_id=source_id,
        piece_by_id=piece_by_id,
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        config=config,
        store=store,
        call=call,
        prompt_version=prompt_version,
        repair_prompt_template=repair_prompt_template,
        repair_prompt_version=repair_version,
        partition_repair_attempts=partition_repair_attempts,
        control_prompt_locale=control_prompt_locale,
    ):
        report.extend(result.report)
        call_records.extend(result.call_records)
        if result.parsed is None:
            continue
        report.extend(
            _attach_call_to_item(item, result.call_records[-1])
            if result.call_records
            else item
            for item in result.parsed.warnings
        )
        for spec in result.parsed.atoms:
            pending.append((piece_by_id[spec.piece_refs[0]].order_index, spec))

    pending.sort(key=lambda item: item[0])
    atoms: list[MaterialAtom] = []
    for source_order, (_, spec) in enumerate(pending):
        atom = build_atom(
            spec,
            snapshot=snapshot,
            piece_by_id=piece_by_id,
            source_order=source_order,
            detect_language=detect_language,
            source_language=source_language,
        )
        atoms.append(atom)

    _check_source_tiling(source_id, pieces, atoms, report)
    return SourceIngestResult(
        snapshot=snapshot,
        pieces=pieces,
        windows=windows,
        atoms=atoms,
        items=report,
        call_records=call_records,
    )


def _run_windows(
    window_calls: list[_WindowCall],
    *,
    profile: SourceProfile,
    source_id: str,
    piece_by_id: dict[str, SourcePiece],
    system_prompt: str,
    user_prompt_template: str,
    config: RuntimeConfig,
    store: RunStore,
    call: CallLlm,
    prompt_version: str,
    repair_prompt_template: str | None,
    repair_prompt_version: str | None,
    partition_repair_attempts: int,
    control_prompt_locale: ControlPromptLocale,
) -> list[_WindowResult]:
    """Run window calls concurrently but return results in original window order."""
    if not window_calls:
        return []
    def run_one(window_call: _WindowCall) -> _WindowResult:
        return _run_window(
            window_id=window_call.window_id,
            window_piece_ids=window_call.window_piece_ids,
            messages=window_call.messages,
            manifest=window_call.manifest,
            profile=profile,
            source_id=source_id,
            piece_by_id=piece_by_id,
            system_prompt=system_prompt,
            user_prompt_template=user_prompt_template,
            config=config,
            store=store,
            call=call,
            prompt_version=prompt_version,
            repair_prompt_template=repair_prompt_template,
            repair_prompt_version=repair_prompt_version,
            partition_repair_attempts=partition_repair_attempts,
            control_prompt_locale=control_prompt_locale,
        )

    return run_ordered_concurrently(
        window_calls,
        run_one,
        max_workers=route_max_concurrency(config, OPERATION_ROUTE),
    )


def _block_repair_messages(
    *,
    repair_piece_ids: list[str],
    piece_by_id: dict[str, SourcePiece],
    profile: SourceProfile,
    system_prompt: str,
    user_prompt_template: str,
    repair_prompt_template: str,
    violations: list[str],
    control_prompt_locale: ControlPromptLocale,
) -> tuple[list[dict[str, str]], AliasManifest]:
    """Project only the failed block and append file-owned repair instructions."""
    manifest = build_manifest(repair_piece_ids)
    messages = project_window(
        manifest=manifest,
        piece_by_id=piece_by_id,
        profile=profile,
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        control_prompt_locale=control_prompt_locale,
    )
    repair_prompt = _render_repair_prompt(
        template=repair_prompt_template,
        violations=violations,
        manifest=manifest,
        control_prompt_locale=control_prompt_locale,
    )
    messages.append({"role": "user", "content": repair_prompt})
    return messages, manifest


def _render_repair_prompt(
    *,
    template: str,
    violations: list[str],
    manifest: AliasManifest,
    control_prompt_locale: ControlPromptLocale,
) -> str:
    return substitute_prompt_blocks(
        template,
        build_repair_blocks(
            violations=violations,
            manifest=manifest,
            control_prompt_locale=control_prompt_locale,
        ),
        prompt_name="atom-repair",
    )


def build_repair_blocks(
    *,
    violations: list[str],
    manifest: AliasManifest,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Return the exact inspectable blocks for a localized ingestion repair."""
    locale = coerce_control_prompt_locale(control_prompt_locale)
    unknown = "- unknown parse error" if locale is not ControlPromptLocale.zh else "- 未知解析错误"
    return {
        "violations": "\n".join(f"- {violation}" for violation in violations)
        or unknown,
        "alias_range": _manifest_alias_range(manifest, locale),
    }


def _manifest_alias_range(
    manifest: AliasManifest,
    locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    aliases = [alias for alias, _piece_id in manifest.items()]
    if not aliases:
        return "(empty)"
    connector = " 到 " if locale is ControlPromptLocale.zh else " through "
    return f"{aliases[0]}{connector}{aliases[-1]}"


def _repair_block_piece_ids(
    partial: PartialParsedWindow, window_piece_ids: list[str]
) -> list[str]:
    """Return one connected partition-repair block, or no authorized block.

    Each partition failure span first expands through every atom it intersects,
    because replacing only part of a valid atom would duplicate or drop pieces.
    Expanded spans may be combined only when they overlap or touch.  A remaining
    hole means multiple independent failures; widening across that hole would
    discard an otherwise valid atom and is therefore forbidden.
    """
    piece_count = len(window_piece_ids)
    if piece_count == 0 or not partial.repair_spans:
        return []
    spans = [_clamp_span(span, piece_count) for span in partial.repair_spans]
    spans = [span for span in spans if span is not None]
    if not spans:
        return []
    expanded_spans = sorted(
        _expand_span_through_atoms(span, partial.spans) for span in spans
    )
    start, end = expanded_spans[0]
    for next_start, next_end in expanded_spans[1:]:
        if next_start > end + 1:
            return []
        end = max(end, next_end)
    return window_piece_ids[start - 1 : end]


def _expand_span_through_atoms(
    span: tuple[int, int], atom_spans: list[tuple[int, int]]
) -> tuple[int, int]:
    start, end = span
    changed = True
    while changed:
        changed = False
        for atom_start, atom_end in atom_spans:
            if _spans_intersect((start, end), (atom_start, atom_end)):
                expanded = (min(start, atom_start), max(end, atom_end))
                if expanded != (start, end):
                    start, end = expanded
                    changed = True
    return start, end


def _clamp_span(span: tuple[int, int], piece_count: int) -> tuple[int, int] | None:
    start, end = span
    start = min(max(start, 1), piece_count)
    end = min(max(end, 1), piece_count)
    if start > end:
        return None
    return start, end


def _spans_intersect(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _merge_repaired_block(
    *,
    partial: PartialParsedWindow,
    repaired: ParsedWindow,
    repair_piece_ids: list[str],
    window_piece_ids: list[str],
) -> tuple[ParsedWindow | None, list[str]]:
    repair_set = set(repair_piece_ids)
    atom_order = {piece_id: index for index, piece_id in enumerate(window_piece_ids)}
    atoms = [
        atom
        for atom in partial.atoms
        if not any(piece_id in repair_set for piece_id in atom.piece_refs)
    ]
    atoms.extend(repaired.atoms)
    atoms.sort(key=lambda atom: atom_order[atom.piece_refs[0]])

    violations = _partition_violations_for_specs(atoms, window_piece_ids)
    if violations:
        return None, violations

    warnings = [
        warning
        for warning in partial.warnings
        if not warning.piece_refs
        or not any(piece_id in repair_set for piece_id in warning.piece_refs)
    ]
    warnings.extend(repaired.warnings)
    return ParsedWindow(atoms=atoms, warnings=warnings), []


def _partition_violations_for_specs(
    atoms: list[AtomSpec], window_piece_ids: list[str]
) -> list[str]:
    order = {piece_id: index + 1 for index, piece_id in enumerate(window_piece_ids)}
    ranges: list[tuple[int, int]] = []
    violations: list[str] = []
    for atom in atoms:
        try:
            numbers = [order[piece_id] for piece_id in atom.piece_refs]
        except KeyError as exc:
            violations.append(f"unknown repaired piece ref {exc.args[0]!r}")
            continue
        expected = list(range(numbers[0], numbers[-1] + 1))
        if numbers != expected:
            violations.append(f"non-contiguous repaired atom pieces {list(atom.piece_refs)!r}")
            continue
        ranges.append((numbers[0], numbers[-1]))

    expected_start = 1
    for start, end in sorted(ranges):
        if start > expected_start:
            missing = window_piece_ids[expected_start - 1 : start - 1]
            violations.append(f"gap after block repair: pieces {missing} not covered")
        elif start < expected_start:
            overlap = window_piece_ids[start - 1 : min(end, expected_start - 1)]
            violations.append(f"overlap after block repair: pieces {overlap} covered twice")
        expected_start = max(expected_start, end + 1)
    if expected_start <= len(window_piece_ids):
        missing = window_piece_ids[expected_start - 1 :]
        violations.append(f"gap after block repair: trailing pieces {missing} not covered")
    return violations


def _transport_reason_code(exc: LlmTransportError) -> str:
    record = exc.record
    if record.provider_status.value == "timeout":
        return "timeout_exhausted"
    status = record.metadata.get("http_status")
    if isinstance(status, int):
        return f"http_{status}_exhausted"
    if record.error and "empty response body" in record.error:
        return "empty_response_body_exhausted"
    return "transport_attempts_exhausted"


def _window_transport_failed(
    *,
    exc: LlmTransportError,
    source_id: str,
    window_id: str,
    window_piece_ids: list[str],
    prior_records: list[LlmCallRecord] | None = None,
    failure_context: str = "window",
) -> _WindowResult:
    call_records = list(prior_records or [])
    if exc.record is not None:
        call_records.append(exc.record)
    record = exc.record
    return _WindowResult(
        parsed=None,
        report=[
            IngestionItem(
                source_id=source_id,
                kind=WINDOW_TRANSPORT_FAILED,
                level=LEVEL_ERROR,
                message=f"provider transport failed for {failure_context}: {exc}",
                window_id=window_id,
                piece_refs=tuple(window_piece_ids),
                call_id=record.call_id if record is not None else None,
                raw_response_path=(
                    record.raw_response_path if record is not None else None
                ),
                usable=False,
                metadata={
                    "failure_class": "transport",
                    "reason_code": _transport_reason_code(exc),
                    "phase": (
                        "partition_repair"
                        if failure_context == "localized partition repair"
                        else "initial"
                    ),
                    "attempts": exc.attempts,
                    "recovery": "retry_same_request",
                },
            )
        ],
        call_records=call_records,
    )


def _run_window(
    *,
    window_id: str,
    window_piece_ids: list[str],
    messages: list[dict[str, str]],
    manifest: AliasManifest,
    profile: SourceProfile,
    source_id: str,
    piece_by_id: dict[str, SourcePiece],
    system_prompt: str,
    user_prompt_template: str,
    config: RuntimeConfig,
    store: RunStore,
    call: CallLlm,
    prompt_version: str,
    repair_prompt_template: str | None,
    repair_prompt_version: str | None,
    partition_repair_attempts: int,
    control_prompt_locale: ControlPromptLocale,
) -> _WindowResult:
    """One window: full call, then at most one authorized topology repair."""
    report: list[IngestionItem] = []
    call_records: list[LlmCallRecord] = []
    try:
        text, record = call(
            operation_route=OPERATION_ROUTE,
            messages=messages,
        config=config,
        store=store,
        prompt_version=prompt_version,
        work_unit=LlmWorkUnitRef(
            kind="ingestion_window",
            unit_id=window_id,
            input_refs=list(window_piece_ids),
        ),
        )
    except LlmTransportError as exc:
        return _window_transport_failed(
            exc=exc,
            source_id=source_id,
            window_id=window_id,
            window_piece_ids=window_piece_ids,
        )
    call_records.append(record)

    partial = parse_window_response_partial(
        text,
        manifest=manifest,
        profile=profile,
        window_piece_ids=window_piece_ids,
        source_id=source_id,
        window_id=window_id,
    )
    if not partial.violations:
        return _WindowResult(
            parsed=ParsedWindow(
                atoms=partial.atoms,
                warnings=partial.warnings,
            ),
            report=report,
            call_records=call_records,
        )

    last_violations = list(partial.violations)
    failure_kind = (
        WINDOW_PROTOCOL_FAILED
        if partial.protocol_violations
        else WINDOW_PARTITION_FAILED
    )
    reason_code = (
        "no_atom_rows"
        if partial.protocol_violations == ["no atom rows in model response"]
        else "row_protocol_or_cell_invalid"
        if partial.protocol_violations
        else "partition_invalid"
    )
    if partition_repair_attempts and repair_prompt_template is not None:
        repair_piece_ids = _repair_block_piece_ids(partial, window_piece_ids)
        if repair_piece_ids:
            block_messages, block_manifest = _block_repair_messages(
                repair_piece_ids=repair_piece_ids,
                piece_by_id=piece_by_id,
                profile=profile,
                system_prompt=system_prompt,
                user_prompt_template=user_prompt_template,
                repair_prompt_template=repair_prompt_template,
                violations=partial.partition_violations,
                control_prompt_locale=control_prompt_locale,
            )
            block_prompt_version = prompt_version
            if repair_prompt_version is not None:
                block_prompt_version = f"{prompt_version}+{repair_prompt_version}"
            try:
                block_text, block_record = call(
                    operation_route=OPERATION_ROUTE,
                    messages=block_messages,
                    config=config,
                    store=store,
                    prompt_version=block_prompt_version,
                    work_unit=LlmWorkUnitRef(
                        kind="ingestion_repair",
                        unit_id=f"{window_id}:repair",
                        input_refs=list(repair_piece_ids),
                        metadata={"source_window_id": window_id},
                    ),
                )
            except LlmTransportError as exc:
                return _window_transport_failed(
                    exc=exc,
                    source_id=source_id,
                    window_id=window_id,
                    # The failed provider work unit was local, but no merged
                    # result exists, so the complete source window is unusable.
                    window_piece_ids=window_piece_ids,
                    prior_records=call_records,
                    failure_context="localized partition repair",
                )
            call_records.append(block_record)

            repaired_partial = parse_window_response_partial(
                block_text,
                manifest=block_manifest,
                profile=profile,
                window_piece_ids=repair_piece_ids,
                source_id=source_id,
                window_id=window_id,
            )
            if repaired_partial.violations:
                reason_code = (
                    "partition_repair_protocol_invalid"
                    if repaired_partial.protocol_violations
                    else "partition_repair_still_invalid"
                )
                last_violations = [
                    *partial.violations,
                    "block repair failed: " + "; ".join(repaired_partial.violations),
                ]
            else:
                repaired = ParsedWindow(
                    atoms=repaired_partial.atoms,
                    warnings=repaired_partial.warnings,
                )
                merged, merge_violations = _merge_repaired_block(
                    partial=partial,
                    repaired=repaired,
                    repair_piece_ids=repair_piece_ids,
                    window_piece_ids=window_piece_ids,
                )
                if merged is not None:
                    report.append(
                        IngestionItem(
                            source_id=source_id,
                            kind=WINDOW_PARTITION_REPAIRED,
                            level=LEVEL_WARNING,
                            message="one connected partition block was repaired and revalidated",
                            window_id=window_id,
                            piece_refs=tuple(repair_piece_ids),
                            call_id=block_record.call_id,
                            raw_response_path=block_record.raw_response_path,
                            metadata={
                                "failure_class": "partition",
                                "reason_code": "connected_partition_repaired",
                                "recovery": "repair_connected_partition",
                                "original_call_id": record.call_id,
                                "repair_call_id": block_record.call_id,
                            },
                        )
                    )
                    return _WindowResult(
                        parsed=merged,
                        report=report,
                        call_records=call_records,
                    )
                last_violations = [
                    *partial.violations,
                    "block repair merge failed: " + "; ".join(merge_violations),
                ]
                reason_code = "partition_repair_merge_invalid"
        elif partial.protocol_violations:
            last_violations.append(
                "localized repair not attempted: row protocol or cell failures "
                "cannot authorize new atom boundaries"
            )
        elif partial.repair_spans:
            reason_code = "partition_failure_spans_disconnected"
            last_violations.append(
                "localized repair not attempted: partition failure spans are "
                "disconnected"
            )
    elif not partial.protocol_violations:
        reason_code = "partition_repair_unavailable"

    last_record = call_records[-1] if call_records else None
    report.append(
        IngestionItem(
            source_id=source_id,
            kind=failure_kind,
            level=LEVEL_ERROR,
            message=(
                f"atom DSL failed after {len(call_records)} model call(s); "
                f"no atoms built for this window: {'; '.join(last_violations)}"
            ),
            window_id=window_id,
            piece_refs=tuple(window_piece_ids),
            call_id=last_record.call_id if last_record is not None else None,
            raw_response_path=(
                last_record.raw_response_path if last_record is not None else None
            ),
            usable=False,
            metadata={
                "failure_class": (
                    "protocol" if failure_kind == WINDOW_PROTOCOL_FAILED else "partition"
                ),
                "reason_code": reason_code,
                "recovery": (
                    "none"
                    if failure_kind == WINDOW_PROTOCOL_FAILED
                    else "repair_connected_partition"
                ),
                "model_calls": len(call_records),
            },
        )
    )
    return _WindowResult(parsed=None, report=report, call_records=call_records)


def ingest_sources(
    sources: Iterable[SourceInput],
    *,
    config: RuntimeConfig,
    store: RunStore,
    budgets: IngestionBudgets | None = None,
    detect_language: LanguageDetector = source_language_detector,
    call: CallLlm = call_llm,
    partition_repair_attempts: int = _DEFAULT_PARTITION_REPAIR_ATTEMPTS,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> IngestResult:
    """Ingest many sources, returning a per-source aggregate result."""
    budgets = budgets or IngestionBudgets.from_config(config)
    results = [
        ingest_source(
            snapshot=source.snapshot,
            raw_text=source.raw_text,
            profile=source.profile,
            source_language=source.language,
            config=config,
            store=store,
            budgets=budgets,
            detect_language=detect_language,
            call=call,
            partition_repair_attempts=partition_repair_attempts,
            control_prompt_locale=control_prompt_locale,
        )
        for source in sources
    ]
    return IngestResult(sources=results)


def _attach_call_to_item(
    item: IngestionItem,
    record: LlmCallRecord,
) -> IngestionItem:
    """Attach the provider attempt that produced one parser warning."""
    if item.call_id is not None:
        return item
    return replace(
        item,
        call_id=record.call_id,
        raw_response_path=record.raw_response_path,
    )


def persist_ingest_result(store: RunStore, result: IngestResult) -> None:
    """Write the aggregate ingestion artifacts to the run store.

    ``SourceSnapshot`` / piece / window diagnostics are always published.  The
    formal evidence pack is published only when every selected source proved its
    complete tiling invariant; a failed run must not expose partial atoms through
    the canonical ``MATERIAL_ATOMS`` path.
    """
    store.write_records(SOURCE_SNAPSHOTS, result.snapshots)
    store.write_records(SOURCE_PIECES, result.pieces)
    store.write_records(SOURCE_WINDOWS, result.windows)
    if result.ok:
        store.write_records(MATERIAL_ATOMS, result.atoms)


def _check_content_hash(
    snapshot: SourceSnapshot, raw_text: str, report: list[IngestionItem]
) -> bool:
    """Fail a real SHA-256 mismatch; ignore placeholders that cannot be checked."""
    declared = snapshot.content_hash
    bare = declared.split(":", 1)[1] if declared.startswith("sha256:") else declared
    bare = bare.lower()
    if len(bare) != 64 or any(char not in "0123456789abcdef" for char in bare):
        return True  # placeholder / non-sha256 hash: nothing reliable to compare
    if content_hash(raw_text) != bare:
        report.append(
            IngestionItem(
                source_id=snapshot.source_id,
                kind=SOURCE_PRECONDITION_FAILED,
                level=LEVEL_ERROR,
                message="raw text hash does not match snapshot.content_hash",
                usable=False,
                metadata={
                    "failure_class": "precondition",
                    "reason_code": "content_hash_mismatch",
                    "recovery": "none",
                },
            )
        )
        return False
    return True


def _check_source_tiling(
    source_id: str,
    pieces: list[SourcePiece],
    atoms: list[MaterialAtom],
    report: list[IngestionItem],
) -> None:
    """Source-level hard invariant: atoms must tile every piece exactly once."""
    index_by_id = {piece.piece_id: piece.order_index for piece in pieces}
    id_by_index = {piece.order_index: piece.piece_id for piece in pieces}
    covered: set[int] = set()
    duplicated: set[int] = set()
    unknown_refs: list[str] = []
    for atom in atoms:
        for piece_ref in atom.provenance.piece_refs:
            index = index_by_id.get(piece_ref)
            if index is None:
                unknown_refs.append(piece_ref)
                continue
            if index in covered:
                duplicated.add(index)
            covered.add(index)

    missing = sorted(set(index_by_id.values()) - covered)
    caused_by = list(
        dict.fromkeys(
            item.window_id
            for item in report
            if not item.usable and item.window_id is not None
        )
    )
    if unknown_refs:
        report.append(
            IngestionItem(
                source_id=source_id,
                kind=SOURCE_INTEGRITY_FAILED,
                level=LEVEL_ERROR,
                message="atom provenance references pieces outside this source",
                piece_refs=tuple(dict.fromkeys(unknown_refs)),
                usable=False,
                metadata={
                    "failure_class": "integrity",
                    "reason_code": "atom_unknown_piece_ref",
                    "recovery": "none",
                },
            )
        )
    if missing:
        report.append(
            IngestionItem(
                source_id=source_id,
                kind=SOURCE_INTEGRITY_FAILED,
                level=LEVEL_ERROR,
                message="pieces left uncovered by any atom (likely a failed window)",
                piece_refs=tuple(id_by_index[index] for index in missing),
                usable=False,
                metadata={
                    "failure_class": "integrity",
                    "reason_code": "source_tiling_gap",
                    "recovery": "none",
                    "caused_by": caused_by,
                },
            )
        )
    if duplicated:
        report.append(
            IngestionItem(
                source_id=source_id,
                kind=SOURCE_INTEGRITY_FAILED,
                level=LEVEL_ERROR,
                message="pieces covered by more than one atom (overlap)",
                piece_refs=tuple(id_by_index[index] for index in sorted(duplicated)),
                usable=False,
                metadata={
                    "failure_class": "integrity",
                    "reason_code": "source_tiling_overlap",
                    "recovery": "none",
                    "caused_by": caused_by,
                },
            )
        )
