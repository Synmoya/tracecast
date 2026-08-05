"""Node Edit DSL parser: model text -> raw ``NODE`` blocks + grammar diagnostics.

Grammar::

    NODE <candidate_alias>
    K <material_alias_or_range>
    C <material_alias_range> | <reason>
    TXT <condensed content>
    O <material_alias_or_range> | <reason>
    ENDNODE

This layer owns *grammar* only: it groups rows into ``NODE ... ENDNODE`` blocks,
classifies each op row, expands inclusive ranges via the material-alias scheme,
and binds a ``C`` row's following ``TXT`` line as its condensed content. It never
resolves an alias to a stable id, never checks pool membership or the partition,
and never repairs -- a structurally broken block is flagged (``malformed`` plus
human-readable ``issues``) for the builder's per-node recovery, not silently
fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tracecast.domain import CANDIDATE_ALIASES, MATERIAL_ALIASES

# Sanity ceiling on an expanded inclusive range -- a parse-safety guard against a
# runaway ``M001-M999999``, not a semantic budget. A real node's pool is small.
_MAX_RANGE_SPAN = 1000

_OP_MARKERS = frozenset({"K", "C", "O"})


@dataclass
class RawOp:
    """One parsed ``K``/``C``/``O`` row (aliases range-expanded, unresolved)."""

    op: str
    aliases: list[str]
    raw_ref: str
    reason: str | None
    content: str | None  # condensed TXT body, only ever set for a ``C`` op
    line: str


@dataclass
class RawNodeBlock:
    """A parsed ``NODE ... ENDNODE`` block; ``malformed`` triggers recovery."""

    candidate_alias: str | None
    ops: list[RawOp]
    malformed: bool = False
    issues: list[str] = field(default_factory=list)
    raw_text: str = ""


@dataclass
class NodeEditParse:
    """All blocks plus any lines that fell outside a ``NODE ... ENDNODE`` block."""

    blocks: list[RawNodeBlock] = field(default_factory=list)
    stray: list[str] = field(default_factory=list)


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


def _rest(line: str) -> str:
    parts = line.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else ""


def _normalize_candidate_alias(token: str) -> str:
    """Canonicalize a candidate alias (``C1`` -> ``C001``); pass raw through on miss."""
    try:
        return CANDIDATE_ALIASES.format(CANDIDATE_ALIASES.parse(token))
    except ValueError:
        return token


def _expand_ref(token: str) -> tuple[list[str], str | None]:
    """Expand a ref token into canonical material aliases, or return an error.

    Accepts a single alias (``M003``), an inclusive range (``M003-M005``), or a
    comma list of either; aliases are normalized to the scheme's canonical width
    so they match the projection manifest.
    """
    aliases: list[str] = []
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_str, hi_str = part.split("-", 1)
            try:
                lo = MATERIAL_ALIASES.parse(lo_str.strip())
                hi = MATERIAL_ALIASES.parse(hi_str.strip())
            except ValueError:
                return [], f"malformed range {part!r}"
            if hi < lo:
                return [], f"descending range {part!r}"
            if hi - lo > _MAX_RANGE_SPAN:
                return [], f"range {part!r} exceeds the {_MAX_RANGE_SPAN}-alias guard"
            aliases.extend(MATERIAL_ALIASES.format(n) for n in range(lo, hi + 1))
        else:
            try:
                aliases.append(MATERIAL_ALIASES.format(MATERIAL_ALIASES.parse(part)))
            except ValueError:
                return [], f"malformed alias {part!r}"
    if not aliases:
        return [], "no material alias on the row"
    return aliases, None


def _parse_op_row(line: str) -> tuple[RawOp, str | None]:
    """Parse one ``K``/``C``/``O`` row into a :class:`RawOp` plus an optional issue."""
    op = _marker(line)
    body = _rest(line)
    ref_part, sep, reason_part = body.partition("|")
    reason = reason_part.strip() if sep else None
    aliases, error = _expand_ref(ref_part.strip())
    raw_op = RawOp(
        op=op,
        aliases=aliases,
        raw_ref=ref_part.strip(),
        reason=reason or None,
        content=None,
        line=line,
    )
    return raw_op, error


def parse_node_edit_dsl(text: str) -> NodeEditParse:
    """Parse a node-edit response into raw blocks for the builder to validate.

    Blocks are delimited by ``NODE``/``ENDNODE``; a ``NODE`` seen while a block is
    still open closes the previous one as ``malformed`` (missing ``ENDNODE``). A
    ``C`` row binds the immediately following ``TXT`` line as its content; a
    ``TXT`` that does not follow a ``C`` and any unrecognized in-block line mark
    the block malformed.
    """
    result = NodeEditParse()
    current: RawNodeBlock | None = None
    block_lines: list[str] = []
    pending_condense: RawOp | None = None

    def close() -> None:
        nonlocal current, pending_condense
        if current is None:
            return
        current.raw_text = "\n".join(block_lines)
        result.blocks.append(current)
        current = None
        pending_condense = None

    for line in _clean_lines(text):
        marker = _marker(line)
        if marker == "NODE":
            if current is not None:
                current.malformed = True
                current.issues.append("missing ENDNODE before the next NODE")
                close()
            alias_token = _rest(line).split(None, 1)
            alias = _normalize_candidate_alias(alias_token[0]) if alias_token else None
            current = RawNodeBlock(candidate_alias=alias, ops=[])
            block_lines = [line]
            pending_condense = None
            if alias is None:
                current.malformed = True
                current.issues.append("NODE row has no candidate alias")
            continue
        if marker == "ENDNODE":
            if current is None:
                result.stray.append(line)
                continue
            block_lines.append(line)
            close()
            continue
        if current is None:
            result.stray.append(line)
            continue

        block_lines.append(line)
        if marker in _OP_MARKERS:
            raw_op, error = _parse_op_row(line)
            if error is not None:
                current.malformed = True
                current.issues.append(error)
            current.ops.append(raw_op)
            pending_condense = raw_op if marker == "C" else None
        elif marker == "TXT":
            if pending_condense is not None:
                pending_condense.content = _rest(line)
                pending_condense = None
            else:
                current.malformed = True
                current.issues.append("TXT line does not follow a C row")
        else:
            current.malformed = True
            current.issues.append(f"unrecognized line marker {marker!r}")

    if current is not None:
        current.malformed = True
        current.issues.append("block not closed by ENDNODE")
        close()
    return result
