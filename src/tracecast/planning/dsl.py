"""DSL parsers: model text -> structured tokens + grammar diagnostics.

This layer owns *grammar* only: it splits rows, enforces the fixed field count,
checks key prefixes, and records a review diagnostic for any line it cannot
parse. It never
resolves aliases, never derives ids, and never invents a node/decision --
malformed rows are dropped to the diagnostics list, not silently repaired.

Each row is split on ``|`` and the field count must match the grammar exactly.
A short row is missing a field; an
over-count row means a ``|`` leaked into a free field and would otherwise
misalign the row, so it is reported rather than absorbed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracecast.domain import CANDIDATE_ALIASES, MATERIAL_ALIASES, NODE_ALIASES, AliasScheme

from .vocab import STAGE_PLANNING, STAGE_SELECTION, diagnostic

# Exact ``|``-separated field counts per row grammar.
_N_FIELDS = 4  # N N1 | fn=.. | mats=.. | summary
_CN_FIELDS = 3  # N N1 | kids=.. | summary
_P_FIELDS = 3  # P <alias> | role | treatment
_D_FIELDS = 2  # D <path_alias> | directive


@dataclass(frozen=True)
class RawNode:
    """A parsed planner ``N`` row (aliases unresolved, vocab unchecked)."""

    row: int
    handle: str
    narrative_function: str
    material_aliases: list[str]
    summary: str


@dataclass(frozen=True)
class RawCompressedNode:
    """A parsed node-compression ``N`` row with unresolved child aliases."""

    row: int
    handle: str
    child_aliases: list[str]
    summary: str


@dataclass(frozen=True)
class RawDecision:
    """A parsed selector ``P`` row (alias/role/treatment unresolved/unchecked)."""

    alias: str
    role: str
    treatment: str


@dataclass(frozen=True)
class RawDirective:
    """A parsed global-directive ``D`` row (path alias unresolved)."""

    alias: str
    directive: str | None


@dataclass
class PlannerParse:
    nodes: list[RawNode] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


@dataclass
class CompressionParse:
    nodes: list[RawCompressedNode] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


@dataclass
class SelectorParse:
    decisions: list[RawDecision] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


@dataclass
class SelectorDirectiveParse:
    directives: list[RawDirective] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)


def _clean_lines(text: str) -> list[str]:
    """Yield non-blank, non-fence content lines (tolerate ``` code fences)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        out.append(line)
    return out


def _marker(line: str) -> str:
    return line.split(None, 1)[0]


def _fields(line: str, expected: int) -> list[str] | None:
    """Split on ``|`` requiring exactly ``expected`` fields; else ``None``."""
    parts = line.split("|")
    if len(parts) != expected:
        return None
    return [part.strip() for part in parts]


def _value(token: str, key: str) -> str | None:
    prefix = f"{key}="
    if not token.startswith(prefix):
        return None
    return token[len(prefix):].strip()


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _alias_csv(value: str, scheme: AliasScheme) -> tuple[list[str], list[dict]]:
    """Parse comma-separated aliases with inclusive ranges like ``M001-M006``."""
    aliases: list[str] = []
    diagnostics: list[dict] = []
    for item in _csv(value):
        if "-" not in item:
            try:
                scheme.parse(item)
            except ValueError as exc:
                diagnostics.append(
                    diagnostic(
                        STAGE_PLANNING,
                        "alias_bad_shape",
                        str(exc),
                        alias=item,
                    )
                )
                continue
            aliases.append(item)
            continue

        start, end = (part.strip() for part in item.split("-", 1))
        try:
            start_number = scheme.parse(start)
            end_number = scheme.parse(end)
        except ValueError as exc:
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "alias_range_bad_shape",
                    str(exc),
                    range=item,
                )
            )
            continue
        if end_number < start_number:
            diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "alias_range_descending",
                    "alias range end is before start",
                    range=item,
                )
            )
            continue
        aliases.extend(scheme.format(number) for number in range(start_number, end_number + 1))
    return aliases, diagnostics


def _valid_node_handle(value: str) -> bool:
    try:
        NODE_ALIASES.parse(value)
    except ValueError:
        return False
    return True


def _is_node_row_marker(marker: str) -> bool:
    return marker == "N" or _valid_node_handle(marker)


def _node_handle_from_head(value: str) -> str | None:
    head = value.split(None, 1)
    if len(head) == 2 and head[0] == "N":
        return head[1].strip()
    if len(head) == 1 and _valid_node_handle(head[0]):
        return head[0]
    return None


def parse_planner_dsl(text: str) -> PlannerParse:
    """Parse a per-window seed planner response into raw nodes.

    ``N`` rows carry explicit response-local handles (``N N1 | ...``). Aliases
    and vocab are validated later by the graph builder.
    """
    result = PlannerParse()
    n_row = 0
    handles: set[str] = set()
    for line in _clean_lines(text):
        marker = _marker(line)
        if _is_node_row_marker(marker):
            n_row += 1
            parts = _fields(line, _N_FIELDS)
            if parts is None:
                result.diagnostics.append(
                    diagnostic(STAGE_PLANNING, "node_field_count", "N row field count mismatch",
                               line=line, row=n_row)
                )
                continue
            handle = _node_handle_from_head(parts[0])
            if handle is None:
                result.diagnostics.append(
                    diagnostic(STAGE_PLANNING, "node_missing_handle",
                               "N row expects an explicit response-local handle",
                               line=line, row=n_row)
                )
                continue
            if not _valid_node_handle(handle):
                result.diagnostics.append(
                    diagnostic(STAGE_PLANNING, "node_bad_handle",
                               f"N row handle {handle!r} is not shaped like N1",
                               line=line, row=n_row, handle=handle)
                )
                continue
            if handle in handles:
                result.diagnostics.append(
                    diagnostic(STAGE_PLANNING, "node_duplicate_handle",
                               f"N row handle {handle!r} is duplicated",
                               line=line, row=n_row, handle=handle)
                )
                continue
            handles.add(handle)
            fn = _value(parts[1], "fn")
            mats = _value(parts[2], "mats")
            summary = parts[3]
            if fn is None or mats is None:
                result.diagnostics.append(
                    diagnostic(STAGE_PLANNING, "node_bad_keys",
                               "N row expects fn=/mats= in order", line=line, row=n_row)
                )
                continue
            material_aliases, range_diagnostics = _alias_csv(mats, MATERIAL_ALIASES)
            result.diagnostics.extend(
                dict(item, line=line, row=n_row) for item in range_diagnostics
            )
            if not material_aliases:
                result.diagnostics.append(
                    diagnostic(STAGE_PLANNING, "node_no_material",
                               "N row references no material", line=line, row=n_row)
                )
                continue
            result.nodes.append(
                RawNode(row=n_row, handle=handle, narrative_function=fn,
                        material_aliases=material_aliases, summary=summary)
            )
        elif marker == "E":
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "unexpected_edge_in_node_pass",
                    "planner node pass does not accept E rows; default Planning does not "
                    "generate edges",
                    line=line,
                )
            )
        else:
            result.diagnostics.append(
                diagnostic(STAGE_PLANNING, "unrecognized_line", "line is not an N row",
                           line=line)
            )
    return result

def parse_node_compression_dsl(text: str) -> CompressionParse:
    """Parse a node-compression response into parent nodes with child aliases."""
    result = CompressionParse()
    n_row = 0
    handles: set[str] = set()
    for line in _clean_lines(text):
        marker = _marker(line)
        if not _is_node_row_marker(marker):
            result.diagnostics.append(
                diagnostic(STAGE_PLANNING, "unrecognized_line", "line is not an N row", line=line)
            )
            continue
        n_row += 1
        parts = _fields(line, _CN_FIELDS)
        if parts is None:
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_node_field_count",
                    "compression N row field count mismatch",
                    line=line,
                    row=n_row,
                )
            )
            continue
        handle = _node_handle_from_head(parts[0])
        if handle is None:
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_node_missing_handle",
                    "compression N row expects an explicit response-local handle",
                    line=line,
                    row=n_row,
                )
            )
            continue
        if not _valid_node_handle(handle):
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_node_bad_handle",
                    f"compression N row handle {handle!r} is not shaped like N1",
                    line=line,
                    row=n_row,
                    handle=handle,
                )
            )
            continue
        if handle in handles:
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_node_duplicate_handle",
                    f"compression N row handle {handle!r} is duplicated",
                    line=line,
                    row=n_row,
                    handle=handle,
                )
            )
            continue
        handles.add(handle)
        kids = _value(parts[1], "kids")
        summary = parts[2]
        if kids is None:
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_node_bad_keys",
                    "compression N row expects kids= in order",
                    line=line,
                    row=n_row,
                )
            )
            continue
        child_aliases, range_diagnostics = _alias_csv(kids, CANDIDATE_ALIASES)
        result.diagnostics.extend(dict(item, line=line, row=n_row) for item in range_diagnostics)
        if not child_aliases:
            result.diagnostics.append(
                diagnostic(
                    STAGE_PLANNING,
                    "compress_node_no_child",
                    "compression N row references no child node",
                    line=line,
                    row=n_row,
                )
            )
            continue
        result.nodes.append(
            RawCompressedNode(
                row=n_row,
                handle=handle,
                child_aliases=child_aliases,
                summary=summary,
            )
        )
    return result


def parse_selector_dsl(text: str) -> SelectorParse:
    """Parse the selector response into raw path decisions (``P`` rows).

    Alias resolution, role/treatment vocab and duplicate-path checks are the
    builder's job; here we only enforce the row grammar.
    """
    result = SelectorParse()
    for line in _clean_lines(text):
        if _marker(line) != "P":
            result.diagnostics.append(
                diagnostic(STAGE_SELECTION, "unrecognized_line", "line is not a P row", line=line)
            )
            continue
        parts = _fields(line, _P_FIELDS)
        if parts is None:
            result.diagnostics.append(
                diagnostic(STAGE_SELECTION, "path_field_count", "P row field count mismatch",
                           line=line)
            )
            continue
        head = parts[0].split(None, 1)
        if len(head) != 2:
            result.diagnostics.append(
                diagnostic(STAGE_SELECTION, "path_missing_alias", "P row missing candidate alias",
                           line=line)
            )
            continue
        result.decisions.append(
            RawDecision(
                alias=head[1].strip(),
                role=parts[1],
                treatment=parts[2],
            )
        )
    return result


def parse_selector_directive_dsl(text: str) -> SelectorDirectiveParse:
    """Parse global path-level directive output into unresolved ``D`` rows."""
    result = SelectorDirectiveParse()
    for line in _clean_lines(text):
        if _marker(line) != "D":
            result.diagnostics.append(
                diagnostic(
                    STAGE_SELECTION,
                    "directive_unrecognized_line",
                    "line is not a D row",
                    line=line,
                )
            )
            continue
        parts = _fields(line, _D_FIELDS)
        if parts is None:
            result.diagnostics.append(
                diagnostic(
                    STAGE_SELECTION,
                    "directive_field_count",
                    "D row field count mismatch",
                    line=line,
                )
            )
            continue
        head = parts[0].split(None, 1)
        if len(head) != 2:
            result.diagnostics.append(
                diagnostic(
                    STAGE_SELECTION,
                    "directive_missing_alias",
                    "D row missing path alias",
                    line=line,
                )
            )
            continue
        if not parts[1]:
            result.diagnostics.append(
                diagnostic(
                    STAGE_SELECTION,
                    "directive_missing_value",
                    "D row directive must be non-empty or use '-' for no directive",
                    line=line,
                )
            )
            continue
        result.directives.append(
            RawDirective(
                alias=head[1].strip(),
                directive=None if parts[1] == "-" else parts[1],
            )
        )
    return result
