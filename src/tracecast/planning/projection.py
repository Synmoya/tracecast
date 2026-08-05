"""Model-facing projections and the fixed system layer for each call.

The LLM never reads artifact JSON. Each call gets a compact, alias-keyed
projection:

- planner (per window): material blocks *with* content (TXT) so the model can
  cluster, keyed by ``M###`` aliases.
- selector (whole graph): compact node blocks *without* atom content
  ("不喂原子"), keyed by ``C###`` aliases, plus any existing compatibility
  edges carried by the input graph.

The fixed prompt text for each call is **not** authored here: it lives in
versioned prompt files under ``prompt/`` (one per route) and is loaded by
``tracecast.prompts``. This module builds the user-side projections and fills the
route-specific user templates that those fixed prompts are paired with.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    CANDIDATE_ALIASES,
    MATERIAL_ALIASES,
    AliasManifest,
    AliasScheme,
    CandidateEdge,
    CandidateNode,
    MaterialAtom,
    SourceWindow,
)
from tracecast.prompt_components import load_planning_protocol_catalog
from tracecast.prompts import substitute_prompt_blocks

from .vocab import DIRECTIONAL_EDGE_ROLES


@dataclass(frozen=True)
class Projection:
    """A rendered model-facing block plus the alias manifest that built it."""

    text: str
    manifest: AliasManifest
    metadata: dict[str, Any] = field(default_factory=dict)


def _csv(values: list[str]) -> str:
    return ",".join(values)


def render_planner_user_prompt(
    *,
    template: str,
    projection: Projection,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Fill the planner user-prompt template for one material window."""
    return substitute_prompt_blocks(
        template,
        build_planner_blocks(
            projection=projection,
            control_prompt_locale=control_prompt_locale,
        ),
        prompt_name="planner",
    )


def build_planner_blocks(
    *,
    projection: Projection,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Return the exact inspectable blocks for a Planning seed unit."""
    locale = coerce_control_prompt_locale(control_prompt_locale)
    descriptions = load_planning_protocol_catalog(locale).narrative_function_descriptions
    separator = "：" if locale is ControlPromptLocale.zh else ": "
    return {
        "alias_range": _alias_range(projection.manifest, locale),
        "narrative_function_descriptions": "\n".join(
            f"- {name}{separator}{description}"
            for name, description in descriptions
        ),
        "material_blocks": projection.text,
    }


def render_node_compress_user_prompt(
    *,
    template: str,
    projection: Projection,
    input_child_count: int,
    target_parent_count: int,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Fill the node-compression user-prompt template for one source window."""
    return substitute_prompt_blocks(
        template,
        build_node_compress_blocks(
            projection=projection,
            input_child_count=input_child_count,
            target_parent_count=target_parent_count,
            control_prompt_locale=control_prompt_locale,
        ),
        prompt_name="node-compress",
    )


def build_node_compress_blocks(
    *,
    projection: Projection,
    input_child_count: int,
    target_parent_count: int,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Return the exact inspectable blocks for a Planning compression unit."""
    return {
        "alias_range": _alias_range(
            projection.manifest,
            coerce_control_prompt_locale(control_prompt_locale),
        ),
        "input_child_count": str(input_child_count),
        "target_parent_count": str(target_parent_count),
        "node_blocks": projection.text,
    }


def render_selector_user_prompt(
    *,
    template: str,
    projection: Projection,
    persona_block: str,
    source_token_estimate: int | None = None,
    target_output_kchars: float | int | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Fill the selector user-prompt template for the whole graph."""
    return substitute_prompt_blocks(
        template,
        build_selector_blocks(
            projection=projection,
            persona_block=persona_block,
            source_token_estimate=source_token_estimate,
            target_output_kchars=target_output_kchars,
            control_prompt_locale=control_prompt_locale,
        ),
        prompt_name="selector",
    )


def build_selector_blocks(
    *,
    projection: Projection,
    persona_block: str,
    source_token_estimate: int | None = None,
    target_output_kchars: float | int | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Return the exact inspectable blocks for the Selection graph unit."""
    locale = coerce_control_prompt_locale(control_prompt_locale)
    return {
        "source_token_estimate_line": _source_token_estimate_line(
            source_token_estimate,
            locale,
        ),
        "target_output_estimate_line": _target_output_estimate_line(
            target_output_kchars,
            locale,
        ),
        "source_aliases": _source_alias_legend(projection),
        "persona_block": persona_block,
        "narrative_function_descriptions": _narrative_function_selector_block(locale),
        "graph_blocks": projection.text,
    }


def project_selector_directive_path(
    nodes: list[CandidateNode],
    locked_path: Sequence[tuple[str, str, str]],
) -> Projection:
    """Project one locked path with fresh ``P###`` aliases for the DIR call."""
    node_by_id = {node.candidate_id: node for node in nodes}
    manifest = AliasManifest.number(
        AliasScheme("P", 3),
        [candidate_id for candidate_id, _role, _treatment in locked_path],
    )
    blocks: list[str] = []
    for candidate_id, role, treatment in locked_path:
        node = node_by_id[candidate_id]
        alias = manifest.alias_for(candidate_id)
        blocks.append(
            "\n".join(
                [
                    f"P {alias} | role={role} | treatment={treatment}",
                    f"SUM {_one_line(node.summary)}",
                ]
            )
        )
    return Projection(text="\n\n".join(blocks), manifest=manifest)


def render_selector_directive_user_prompt(
    *,
    template: str,
    projection: Projection,
    persona_block: str,
) -> str:
    """Fill the initial whole-path directive prompt."""
    return substitute_prompt_blocks(
        template,
        {
            "persona_block": persona_block,
            "path_blocks": projection.text,
        },
        prompt_name="selector-directive",
    )


def _one_line(value: str) -> str:
    return " ".join(value.splitlines()).strip()


def _alias_range(
    manifest: AliasManifest,
    locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    aliases = [alias for alias, _global_id in manifest.items()]
    if not aliases:
        return "(empty)"
    connector = " 到 " if locale is ControlPromptLocale.zh else " through "
    return f"{aliases[0]}{connector}{aliases[-1]}"


def _narrative_function_selector_block(locale: ControlPromptLocale) -> str:
    lines = [
        "## narrative_function 说明"
        if locale is ControlPromptLocale.zh
        else "## narrative_function reference"
    ]
    separator = "：" if locale is ControlPromptLocale.zh else ": "
    lines.extend(
        f"- {name}{separator}{description}"
        for name, description in load_planning_protocol_catalog(
            locale
        ).narrative_function_descriptions
    )
    return "\n".join(lines)


def _source_token_estimate_line(
    value: int | None,
    locale: ControlPromptLocale,
) -> str:
    if value is None or value <= 0:
        return (
            "- 原始素材 token 量级未配置；按候选图规模做软判断。"
            if locale is ControlPromptLocale.zh
            else "- Source-token scale is unavailable; use graph scale as a soft reference."
        )
    return (
        f"- 原始素材约 {value} token"
        if locale is ControlPromptLocale.zh
        else f"- Source material: approximately {value} tokens"
    )


def _target_output_estimate_line(
    value: float | int | None,
    locale: ControlPromptLocale,
) -> str:
    if value is None or value <= 0:
        return (
            "- 目标成文 k 字数未配置；按 persona 与下游 chunk policy 做软判断。"
            if locale is ControlPromptLocale.zh
            else (
                "- Target output kchars is unavailable; use the persona and "
                "downstream chunk policy as soft references."
            )
        )
    formatted = f"{value:g}" if isinstance(value, float) else str(value)
    return (
        f"- 目标成文约 {formatted} k字"
        if locale is ControlPromptLocale.zh
        else f"- Target output: approximately {formatted} kchars"
    )


def _source_alias_legend(projection: Projection) -> str:
    source_aliases = projection.metadata.get("source_aliases", {})
    if not isinstance(source_aliases, dict) or not source_aliases:
        return "(none)"

    def sort_key(item: tuple[str, str]) -> tuple[int, int | str]:
        alias = item[0]
        if alias.startswith("S") and alias[1:].isdigit():
            return (0, int(alias[1:]))
        return (1, alias)

    ordered = sorted(
        ((str(alias), str(source_ref)) for source_ref, alias in source_aliases.items()),
        key=sort_key,
    )
    return "\n".join(f"- {alias}: {source_ref}" for alias, source_ref in ordered)


def project_window(window: SourceWindow, atoms: list[MaterialAtom]) -> Projection:
    """Project one window's atoms into the planner user block (``M###`` aliases).

    ``atoms`` must already be ordered (the caller passes source-order). The
    manifest mints ``M001..`` in that order; the model references them in ``N``
    rows and the parser expands them back to stable atom ids.
    """
    manifest = AliasManifest.number(MATERIAL_ALIASES, [atom.id for atom in atoms])
    blocks: list[str] = []
    for atom in atoms:
        alias = manifest.alias_for(atom.id)
        header = (
            f"M {alias} | src={window.source_id} | so={atom.source_order} "
            f"| k={atom.content_kind.value} | tg={_csv(atom.tags)}"
        )
        blocks.append(f"{header}\nTXT {atom.content}")
    return Projection(text="\n\n".join(blocks), manifest=manifest)


def _source_aliases(nodes: list[CandidateNode]) -> dict[str, str]:
    source_refs: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        source_ref = str(node.metadata.get("source_ref", ""))
        if source_ref and source_ref not in seen:
            seen.add(source_ref)
            source_refs.append(source_ref)
    return {source_ref: f"S{index}" for index, source_ref in enumerate(source_refs, start=1)}


def _node_block(
    node: CandidateNode,
    alias: str,
    source_alias_by_ref: dict[str, str],
    *,
    include_material_count: bool = True,
    include_material_tokens: bool = False,
) -> str:
    source_ref = node.metadata.get("source_ref", "")
    source_alias = source_alias_by_ref.get(str(source_ref), "S?")
    fields = [
        f"C {alias}",
        f"fn={node.narrative_function or ''}",
    ]
    if include_material_count:
        fields.append(f"m={len(node.material_ids)}")
    if include_material_tokens:
        value = (
            str(node.material_estimated_tokens)
            if node.material_estimated_tokens is not None
            else "?"
        )
        fields.append(f"mt={value}")
    placement = (
        f"{source_alias}:{node.source_span.start}-{node.source_span.end}"
        if node.source_span is not None
        else f"{source_alias}:?"
    )
    fields.append(f"sp={placement}")
    header = " | ".join(fields)
    lines = [header]
    lines.append(f"SUM {node.summary}")
    return "\n".join(lines)


def _compression_node_block(
    node: CandidateNode,
    alias: str,
    source_alias_by_ref: dict[str, str],
    seed_summaries: list[str],
) -> str:
    source_ref = node.metadata.get("source_ref", "")
    source_alias = source_alias_by_ref.get(str(source_ref), "S?")
    if node.source_span is not None:
        placement = f"{source_alias}:{node.source_span.start}-{node.source_span.end}"
    else:
        placement = f"{source_alias}:?"
    lines = [f"C {alias} | m={len(node.material_ids)} | sp={placement}"]
    lines.extend(f"SUM {summary}" for summary in seed_summaries)
    return "\n".join(lines)


def project_compression_nodes(
    nodes: list[CandidateNode],
    seed_summaries_by_node_id: dict[str, list[str]],
) -> Projection:
    """Compact node projection for node-compress calls.

    Unlike edge/selector projection, compression does not need ``fn``, ``src``
    or ``win``. For iterative compression, the SUM lines always expand back to
    the original seed-node summaries covered by the current child node.
    """
    manifest = AliasManifest.number(CANDIDATE_ALIASES, [n.candidate_id for n in nodes])
    source_alias_by_ref = _source_aliases(nodes)
    blocks = [
        _compression_node_block(
            node,
            manifest.alias_for(node.candidate_id),
            source_alias_by_ref,
            seed_summaries_by_node_id.get(node.candidate_id, [node.summary]),
        )
        for node in nodes
    ]
    return Projection(
        text="\n\n".join(blocks),
        manifest=manifest,
        metadata={"source_aliases": source_alias_by_ref},
    )


def project_selector_graph(nodes: list[CandidateNode], edges: list[CandidateEdge]) -> Projection:
    """Project selector input, placing edge hints after their most relevant node.

    Directional edges are shown after their origin role (``setup``/``cause``).
    Symmetric edges have no origin, so they are shown after the earlier endpoint
    in graph order. The edge remains read-only context either way.
    """
    manifest = AliasManifest.number(CANDIDATE_ALIASES, [n.candidate_id for n in nodes])
    source_alias_by_ref = _source_aliases(nodes)
    order_by_candidate = {node.candidate_id: index for index, node in enumerate(nodes)}
    edge_lines_by_anchor: dict[str, list[str]] = {}
    for edge in edges:
        rendered = _edge_line(edge, manifest)
        if rendered is None:
            continue
        anchor = _edge_anchor(edge, order_by_candidate)
        if anchor is None or anchor not in manifest:
            continue
        edge_lines_by_anchor.setdefault(anchor, []).append(rendered)

    blocks: list[str] = []
    for node in nodes:
        alias = manifest.alias_for(node.candidate_id)
        lines = [
            _node_block(
                node,
                alias,
                source_alias_by_ref,
                include_material_tokens=True,
            )
        ]
        lines.extend(edge_lines_by_anchor.get(node.candidate_id, ()))
        blocks.append("\n".join(lines))
    return Projection(
        text="\n\n".join(blocks),
        manifest=manifest,
        metadata={"source_aliases": source_alias_by_ref},
    )


def _edge_role_order(edge: CandidateEdge) -> tuple[str, ...]:
    directional = DIRECTIONAL_EDGE_ROLES.get(edge.edge_type)
    if directional is not None:
        return directional
    if set(edge.endpoints) == {"a", "b"}:
        return ("a", "b")
    return tuple(sorted(edge.endpoints))


def _edge_line(edge: CandidateEdge, manifest: AliasManifest) -> str | None:
    parts: list[str] = []
    for role in _edge_role_order(edge):
        candidate = edge.endpoints.get(role)
        if candidate is None:
            return None
        try:
            parts.append(f"{role}={manifest.alias_for(candidate)}")
        except KeyError:
            return None
    line = f"E {edge.edge_type} | " + " | ".join(parts)
    if edge.reason:
        line += f" | {edge.reason}"
    return line


def _edge_anchor(edge: CandidateEdge, order_by_candidate: dict[str, int]) -> str | None:
    directional = DIRECTIONAL_EDGE_ROLES.get(edge.edge_type)
    if directional is not None:
        return edge.endpoints.get(directional[0])
    candidates = [
        candidate for candidate in edge.endpoints.values() if candidate in order_by_candidate
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: order_by_candidate[candidate])


def project_edges(edges: list[CandidateEdge], manifest: AliasManifest) -> str:
    """Render existing edges as read-only context for the selector projection.

    Edges whose endpoints are not in ``manifest`` are skipped (they cannot be
    shown without an alias); this is projection only, never a fact source.
    """
    lines: list[str] = []
    for edge in edges:
        line = _edge_line(edge, manifest)
        if line is not None:
            lines.append(line)
    return "\n".join(lines)
