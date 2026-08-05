"""Model-facing user prompt projection for one ``NodeEditBatch``.

The LLM never reads artifact JSON. Each batch is projected into a sequence of
``NODE ... ENDNODE`` blocks: per node a thin header
(role/treatment/fn/source-local range), its optional ``DIR`` context, and one
slimmed ``M``/``TXT`` row per mounted material. The prompt prose is not authored
here -- fixed boundaries live in
``prompt/node_edit/node_edit.system.{locale}.md`` and the user-side template lives in
``prompt/node_edit/node_edit.user.{locale}.md``.

Alias discipline: candidate aliases
(``C###``) are minted per batch so every NODE block is addressable; material
aliases (``M###``) are minted batch-wide in node-then-source order, so an
``M`` alias belongs to exactly one node and a cross-node reference is detectable
when the parser's output is resolved against the node's own pool.
"""

from __future__ import annotations

from dataclasses import dataclass

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    CANDIDATE_ALIASES,
    MATERIAL_ALIASES,
    AliasManifest,
    CandidateNode,
    MaterialAtom,
    SelectionDecision,
)
from tracecast.prompt_components import (
    load_ingestion_content_kind_catalog,
    load_planning_protocol_catalog,
)
from tracecast.prompts import substitute_prompt_blocks

_ROLE_DESCRIPTIONS_ZH = {
    "core": "节点整体支撑状态、因果、身份、目标或 reveal；其中仍可能有可压缩材料。",
    "bridge": "节点整体只交代结果以维持连续；其中关键材料仍可 K。",
    "context": "节点整体提供世界、机制、背景或观点支撑。",
    "color": "节点整体改善听感或人物质感；低风险、可省略。",
}
_TREATMENT_DESCRIPTIONS_ZH = {
    "quote": "节点整体有原文表达价值；是否保留原文仍逐材料判断。",
    "paraphrase": "节点整体适合正常叙述；不等于逐条 K。",
    "summarize": "节点整体倾向压缩到结果或功能；不等于全部 C/O。",
}
_ROLE_DESCRIPTIONS_EN = {
    "core": (
        "The node supports a state, cause, identity, objective, or reveal; some "
        "materials may still be compressible."
    ),
    "bridge": (
        "The node mainly preserves continuity by stating a result; pivotal "
        "materials can still be K."
    ),
    "context": "The node supplies world, mechanism, background, or attributed-opinion context.",
    "color": "The node improves listening value or character texture and is low-risk to omit.",
}
_TREATMENT_DESCRIPTIONS_EN = {
    "quote": "The node has wording worth preserving; judge verbatim retention per material.",
    "paraphrase": "The node suits ordinary narration; this does not mean K every material.",
    "summarize": "The node tends toward a result or function; this does not mean C/O everything.",
}


def node_edit_field_notes_version(
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    locale = coerce_control_prompt_locale(control_prompt_locale)
    return (
        "node_edit_field_notes@v2"
        f"+{load_ingestion_content_kind_catalog(locale).version}"
        f"+{load_planning_protocol_catalog(locale).version}"
    )


NODE_EDIT_FIELD_NOTES_VERSION = node_edit_field_notes_version()


@dataclass(frozen=True)
class BatchNode:
    """One accepted node prepared for editing: decision, node, and ordered pool.

    ``pool`` is the node's resolved materials ordered by ``source_order`` -- the
    exact input set the partition invariant is checked against.
    """

    candidate_id: str
    decision: SelectionDecision
    node: CandidateNode
    pool: list[MaterialAtom]


@dataclass(frozen=True)
class BatchProjection:
    """A rendered batch block plus the two alias manifests that built it."""

    text: str
    candidate_manifest: AliasManifest
    material_manifest: AliasManifest
    source_aliases: dict[str, str]
    roles: tuple[str, ...]
    treatments: tuple[str, ...]
    narrative_functions: tuple[str, ...]
    content_kinds: tuple[str, ...]


def _csv(values: list[str]) -> str:
    return ",".join(values)


def render_node_edit_user_prompt(
    *,
    template: str,
    projection: BatchProjection,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Fill the node-edit user prompt template with dynamic batch data."""
    return substitute_prompt_blocks(
        template,
        build_node_edit_blocks(
            projection,
            control_prompt_locale=control_prompt_locale,
        ),
        prompt_name="node-edit",
    )


def build_node_edit_blocks(
    projection: BatchProjection,
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Return the exact inspectable blocks for a Node Edit batch unit."""
    return {
        "candidate_alias_range": _alias_range(
            projection.candidate_manifest,
            control_prompt_locale,
        ),
        "material_alias_range": _alias_range(
            projection.material_manifest,
            control_prompt_locale,
        ),
        "source_aliases": _source_alias_legend(projection.source_aliases),
        "field_notes": _field_notes(projection, control_prompt_locale),
        "node_blocks": projection.text,
    }


def _alias_range(
    manifest: AliasManifest,
    locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    aliases = [alias for alias, _global_id in manifest.items()]
    if not aliases:
        return "(empty)"
    connector = " 到 " if locale is ControlPromptLocale.zh else " through "
    return f"{aliases[0]}{connector}{aliases[-1]}"


def _ordered_unique(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _descriptions(
    values: tuple[str, ...],
    descriptions: dict[str, str],
    locale: ControlPromptLocale,
) -> str:
    missing = (
        "未在说明集中定义"
        if locale is ControlPromptLocale.zh
        else "not defined in this reference"
    )
    parts = [f"{value}={descriptions.get(value, missing)}" for value in values]
    separator = "；" if locale is ControlPromptLocale.zh else "; "
    empty = "本批次无" if locale is ControlPromptLocale.zh else "none in this batch"
    return separator.join(parts) if parts else empty


def _content_kind_descriptions(
    values: tuple[str, ...],
    locale: ControlPromptLocale,
) -> str:
    catalog = load_ingestion_content_kind_catalog(locale)
    descriptions = {
        kind.value: description
        for kind, description in catalog.descriptions.items()
    }
    return _descriptions(values, descriptions, locale)


def _narrative_function_descriptions(
    values: tuple[str, ...],
    locale: ControlPromptLocale,
) -> str:
    descriptions = dict(
        load_planning_protocol_catalog(locale).narrative_function_descriptions
    )
    return _descriptions(values, descriptions, locale)


def _field_notes(
    projection: BatchProjection,
    locale: ControlPromptLocale,
) -> str:
    locale = coerce_control_prompt_locale(locale)
    if locale is not ControlPromptLocale.zh:
        return "\n".join(
            [
                "- `DIR`: highest-priority node-level control, subject to the evidence "
                "boundary; it cannot prove that other materials already cover a fact.",
                "- `role`: coarse node-level narrative function; it changes the "
                "contribution threshold, not an individual material's K/C/O. This batch: "
                f"{_descriptions(projection.roles, _ROLE_DESCRIPTIONS_EN, locale)}",
                "- `treatment`: node-level compression/expression reference; it changes "
                "the retention threshold, not the material operation. This batch: "
                f"{_descriptions(projection.treatments, _TREATMENT_DESCRIPTIONS_EN, locale)}",
                "- `fn`: upstream narrative-function reference used only to understand "
                "node granularity. This batch: "
                f"{_narrative_function_descriptions(projection.narrative_functions, locale)}",
                "- `k`: material content kind used only for interpretation, never as an "
                "importance or operation mapping. This batch: "
                f"{_content_kind_descriptions(projection.content_kinds, locale)}",
                "- `so`: material order within one source; `tg`: auxiliary tags, not importance.",
            ]
        )
    return "\n".join(
        [
            "- `DIR`: node-level controls 中优先，但服从事实边界，也不能充当其他材料已覆盖的依据。",
            "- `role`: 节点级大致叙述功能；影响材料贡献的判断阈值，不决定单条材料 K/C/O。"
            f"本批次：{_descriptions(projection.roles, _ROLE_DESCRIPTIONS_ZH, locale)}",
            "- `treatment`: 节点级压缩/表达强度参考；影响保留或压缩的判断阈值，不等于材料操作。"
            f"本批次：{_descriptions(projection.treatments, _TREATMENT_DESCRIPTIONS_ZH, locale)}",
            "- `fn`: 上游节点叙事功能参考，只帮助理解 node 粒度。"
            f"本批次：{_narrative_function_descriptions(projection.narrative_functions, locale)}",
            "- `k`: material 内容类型，只帮助理解材料，不是重要性或操作映射。"
            f"本批次：{_content_kind_descriptions(projection.content_kinds, locale)}",
            "- `so`: 同一 source 内的材料顺序；`tg`: 辅助标签，不代表重要度。",
        ]
    )


def _source_aliases(batch: list[BatchNode]) -> dict[str, str]:
    source_refs: list[str] = []
    seen: set[str] = set()
    for batch_node in batch:
        for atom in batch_node.pool:
            source_ref = atom.provenance.source_ref
            if source_ref and source_ref not in seen:
                seen.add(source_ref)
                source_refs.append(source_ref)
    return {source_ref: f"S{index}" for index, source_ref in enumerate(source_refs, start=1)}


def _source_alias_legend(source_aliases: dict[str, str]) -> str:
    if not source_aliases:
        return "(none)"

    def sort_key(item: tuple[str, str]) -> tuple[int, int | str]:
        alias = item[1]
        if alias.startswith("S") and alias[1:].isdigit():
            return (0, int(alias[1:]))
        return (1, alias)

    ordered = sorted(source_aliases.items(), key=sort_key)
    return "\n".join(f"- {alias}: {source_ref}" for source_ref, alias in ordered)


def _source_local_range(pool: list[MaterialAtom], source_aliases: dict[str, str]) -> str:
    """``source_alias:first_source_order-last_source_order`` over the node's pool.

    A local-position hint, not global story order. Empty when the pool is empty.
    """
    if not pool:
        return ""
    source_ref = pool[0].provenance.source_ref
    source_alias = source_aliases.get(source_ref, "S?")
    orders = [atom.source_order for atom in pool]
    return f"{source_alias}:{min(orders)}-{max(orders)}"


def _project_text(text: str) -> str:
    """Collapse source whitespace for a one-line prompt display only."""
    return " ".join(text.split())


def project_batch(batch: list[BatchNode]) -> BatchProjection:
    """Project a batch of nodes into the node-edit user block.

    Candidate aliases are minted in batch order; material aliases are minted
    node-by-node in pool order. The returned manifests expand the model's
    ``C``/``M`` references back to stable ids.
    """
    candidate_manifest = AliasManifest.number(
        CANDIDATE_ALIASES, [node.candidate_id for node in batch]
    )
    material_manifest = AliasManifest(MATERIAL_ALIASES)
    source_aliases = _source_aliases(batch)
    roles: list[str] = []
    treatments: list[str] = []
    narrative_functions: list[str] = []
    content_kinds: list[str] = []
    blocks: list[str] = []
    for batch_node in batch:
        c_alias = candidate_manifest.alias_for(batch_node.candidate_id)
        decision = batch_node.decision
        role = decision.role.value if decision.role else ""
        treatment = decision.treatment.value if decision.treatment else ""
        roles.append(role)
        treatments.append(treatment)
        if batch_node.node.narrative_function:
            narrative_functions.append(batch_node.node.narrative_function)
        header = (
            f"NODE {c_alias} | role={role} | treatment={treatment} "
            f"| fn={batch_node.node.narrative_function or ''} "
            f"| sp={_source_local_range(batch_node.pool, source_aliases)}"
        )
        lines = [header]
        if decision.directive:
            lines.append(f"DIR {decision.directive}")
        for atom in batch_node.pool:
            m_alias = material_manifest.mint(atom.id)
            content_kinds.append(atom.content_kind.value)
            lines.append(
                f"M {m_alias} | so={atom.source_order} | k={atom.content_kind.value} "
                f"| tg={_csv(atom.tags)}"
            )
            lines.append(f"TXT {_project_text(atom.content)}")
        lines.append("ENDNODE")
        blocks.append("\n".join(lines))
    return BatchProjection(
        text="\n\n".join(blocks),
        candidate_manifest=candidate_manifest,
        material_manifest=material_manifest,
        source_aliases=source_aliases,
        roles=_ordered_unique(roles),
        treatments=_ordered_unique(treatments),
        narrative_functions=_ordered_unique(narrative_functions),
        content_kinds=_ordered_unique(content_kinds),
    )
