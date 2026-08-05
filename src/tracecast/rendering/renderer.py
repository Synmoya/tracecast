"""Rendering stage: turn each ``RenderContract`` into verbatim chunk body text.

Main chain: project one contract into a slim natural-language semantic block
(never the raw artifact JSON), inject the replaceable host-thinking and
host-expression layers into the renderer user template, call the provider once per chunk
for target-language prose, then let code wrap a
:class:`~tracecast.domain.ChunkArtifact` around the body **verbatim** -- no
auto-polish, no repair.

The renderer is an expression layer, not a fact layer: only material content is a
fact source. Node directives are inserted as local editorial annotations, while
the pre-render fact-context card, directives and persona only shape *how* the
material is connected and told.

Chunk bodies are independent calls over code-owned contracts and precomputed
fact-context cards. They may therefore run concurrently, and no generated body
text is fed into another body-render call.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tracecast.concurrency import route_max_concurrency, run_ordered_concurrently
from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    AuditReportItem,
    ChunkArtifact,
    LlmCallRecord,
    LlmWorkUnitRef,
    NameResolutionPlan,
    RenderContract,
    RenderContractMaterial,
    RenderFactContext,
    estimate_tokens,
    issue_id,
)
from tracecast.hosts import HostProfile, SegmentPosition
from tracecast.llm import call_llm
from tracecast.name_resolution import (
    resolve_contract_names,
    resolve_fact_context_names,
)
from tracecast.prompt_components import load_rendering_protocol_catalog
from tracecast.prompts import load_system_prompt, load_user_prompt, substitute_prompt_blocks
from tracecast.storage import RunStore, RuntimeConfig, content_hash

from .degenerate import is_degenerate_repetition
from .persona import RenderPersona, coerce_persona
from .results import RenderResult

#: LLM route name (configured in ``runtime.json``) and budget bucket.
RENDER_ROUTE = "render_chunk"
STAGE_RENDERING = "rendering"
#: Config key under ``stage_budgets.rendering`` bounding projection input size.
RENDER_INPUT_BUDGET_KEY = "render_max_input_tokens"
#: Total attempts (initial + plain retries) allowed when a chunk body comes back
#: as degenerate repeated-character output. Same request, no repair prompt --
#: matches ``llm/client.py``'s own transport-retry default attempt count.
RENDER_DEGENERATE_MAX_ATTEMPTS = 3
#: Dynamic blocks whose values are invariant across chunks and can therefore be
#: carried from a single-chunk experiment into a full concurrent render safely.
RENDER_PORTABLE_BLOCK_KEYS = (
    "host_thinking_block",
    "host_expression_block",
)


@dataclass(frozen=True)
class RenderChunkPosition:
    """Code-derived position scalars for one contract in a complete render batch."""

    chunk_index: int
    chunk_count: int
    segment_position: SegmentPosition


def _catalog(
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
):
    """Fetch the rendering protocol catalog on demand.

    The loader itself is ``@cache``d; reading through a function (instead of a
    module-level snapshot) lets tooling clear that cache and pick up edits to
    ``rendering_protocol.{locale}.json`` without re-importing this module.
    """
    return load_rendering_protocol_catalog(control_prompt_locale)


#: Patchable provider-call seam (signature-compatible with ``llm.call_llm``).
LlmCall = Callable[..., tuple[str, LlmCallRecord]]


def render_chunks(
    contracts: Iterable[RenderContract],
    *,
    persona: RenderPersona | HostProfile | Mapping[str, Any],
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall = call_llm,
    name_resolution: NameResolutionPlan,
    output_language: str,
    fact_contexts: Sequence[RenderFactContext] = (),
    block_overrides: Mapping[str, str] | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> RenderResult:
    """Render every contract concurrently into artifacts plus explicit reports.

    Contracts are returned in ``order_index`` order, with one independent call
    per chunk. No generated chunk body is projected into another render call.
    Each model body is wrapped **verbatim** into a ``ChunkArtifact`` with computed
    ``input_hash`` (over the contract projection) and ``output_hash`` (over the
    body). ``persona`` is a :class:`RenderPersona` or its mapping form; ``call`` is
    the patchable provider seam (default :func:`llm.call_llm`). Optional
    ``block_overrides`` may replace only the two chunk-invariant host blocks;
    position-selected style examples, evidence, fact context and forbidden claims
    remain contract-derived for every chunk.
    """
    persona = coerce_persona(persona)
    control_prompt_locale = coerce_control_prompt_locale(control_prompt_locale)
    system_prompt, system_version = load_system_prompt(
        RENDER_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    user_prompt_template, user_version = load_user_prompt(
        RENDER_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    prompt_version = (
        f"{system_version}+{user_version}+{_catalog(control_prompt_locale).version}"
    )
    if persona.host_version:
        prompt_version += f"+{persona.host_version}"
    budget = _input_budget(config)

    ordered = sorted(contracts, key=lambda c: c.order_index)
    positions_by_chunk = derive_render_chunk_positions(ordered)
    if name_resolution.output_language != output_language:
        raise ValueError("NameResolutionPlan output_language does not match rendering")
    if any(contract.project_id != name_resolution.project_id for contract in ordered):
        raise ValueError(
            "NameResolutionPlan project_id does not match rendering contracts"
        )
    expected_contract_ids = [contract.contract_id for contract in ordered]
    if name_resolution.contract_ids != expected_contract_ids:
        raise ValueError(
            "NameResolutionPlan contract coverage/order must match contracts exactly"
        )
    context_by_chunk: dict[str, RenderFactContext] = {}
    for context in fact_contexts:
        if context.chunk_id in context_by_chunk:
            raise ValueError(f"duplicate fact context for {context.chunk_id}")
        context_by_chunk[context.chunk_id] = context
    if context_by_chunk:
        expected = {contract.chunk_id for contract in ordered}
        actual = set(context_by_chunk)
        if actual != expected:
            raise ValueError(
                "fact contexts must match render contracts exactly "
                f"(missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
            )
        for contract in ordered:
            context = context_by_chunk[contract.chunk_id]
            if context.order_index != contract.order_index:
                raise ValueError(
                    f"fact context order mismatch for {contract.chunk_id}: "
                    f"{context.order_index} != {contract.order_index}"
                )
    def run_one(contract: RenderContract) -> tuple[ChunkArtifact, list[AuditReportItem]]:
        position = positions_by_chunk[contract.chunk_id]
        style_example_sample_key = f"{store.run_id}:{contract.contract_id}"
        projection = render_chunk_user_prompt(
            template=user_prompt_template,
            contract=contract,
            persona=persona,
            output_language=output_language,
            name_resolution=name_resolution,
            fact_context=context_by_chunk.get(contract.chunk_id),
            block_overrides=block_overrides,
            chunk_index=position.chunk_index,
            chunk_count=position.chunk_count,
            segment_position=position.segment_position,
            style_example_sample_key=style_example_sample_key,
            control_prompt_locale=control_prompt_locale,
        )

        over_budget = budget is not None and estimate_tokens(projection) > budget
        input_refs = [
            *[node.source_node_id for node in contract.nodes],
            *[material.source_material_id for material in contract.materials],
        ]

        def _call_render(attempt: int) -> tuple[str, LlmCallRecord]:
            return call(
                operation_route=RENDER_ROUTE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": projection},
                ],
                config=config,
                store=store,
                prompt_version=prompt_version,
                work_unit=LlmWorkUnitRef(
                    kind="render_chunk",
                    unit_id=contract.chunk_id,
                    order_index=contract.order_index,
                    input_refs=input_refs,
                    metadata={
                        "name_resolution_plan_id": name_resolution.plan_id,
                        "output_language": output_language,
                        "attempt": attempt,
                        "chunk_index": position.chunk_index,
                        "chunk_count": position.chunk_count,
                        "segment_position": position.segment_position,
                        "style_example_sample_key": style_example_sample_key,
                    },
                ),
            )

        text, record = _call_render(1)
        degenerate_attempts = 1
        while (
            is_degenerate_repetition(text)
            and degenerate_attempts < RENDER_DEGENERATE_MAX_ATTEMPTS
        ):
            degenerate_attempts += 1
            text, record = _call_render(degenerate_attempts)

        local_reports: list[AuditReportItem] = []
        if over_budget:
            local_reports.append(
                _render_report(
                    kind="render_over_budget",
                    message=f"chunk projection exceeds {RENDER_INPUT_BUDGET_KEY}",
                    chunk_id=contract.chunk_id,
                    record=record,
                )
            )
        if degenerate_attempts > 1:
            still_degenerate = is_degenerate_repetition(text)
            local_reports.append(
                _render_report(
                    kind="render_degenerate_output",
                    message=(
                        "chunk body still a repeated-character run after exhausting "
                        "plain retries"
                        if still_degenerate
                        else "chunk body degenerated once but recovered after a plain retry"
                    ),
                    chunk_id=contract.chunk_id,
                    record=record,
                    level="error" if still_degenerate else "warning",
                    usable=not still_degenerate,
                    metadata={"attempts": degenerate_attempts},
                )
            )
        artifact = ChunkArtifact(
            chunk_id=contract.chunk_id,
            order_index=contract.order_index,
            contract_id=contract.contract_id,
            text=text,
            input_hash=content_hash(projection),
            output_hash=content_hash(text),
            generated_at=datetime.now(tz=UTC),
            metadata=_artifact_metadata(
                contract,
                persona,
                prompt_version,
                record.call_id,
                fact_context=context_by_chunk.get(contract.chunk_id),
                name_resolution=name_resolution,
                output_language=output_language,
                chunk_position=position,
                style_example_sample_key=style_example_sample_key,
            ),
        )
        return artifact, local_reports

    completed = run_ordered_concurrently(
        ordered,
        run_one,
        max_workers=route_max_concurrency(config, RENDER_ROUTE),
    )
    return RenderResult(
        artifacts=[artifact for artifact, _reports in completed],
        reports=[report for _artifact, reports in completed for report in reports],
    )


def render_host_thinking_block(
    persona: RenderPersona,
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Render the abstract host-thinking block injected into the user prompt.

    The complete block comes from the selected external host.  System-owned
    evidence and position rules remain in the route prompt/catalog.
    """
    lines: list[str] = []
    if persona.viewpoint:
        label = (
            "Host 观点层(viewpoint)"
            if control_prompt_locale is ControlPromptLocale.zh
            else "Host viewpoint"
        )
        lines.append(f"- {label}: {persona.viewpoint}")
    lines.extend(f"- {rule}" for rule in persona.thinking_rules)
    if not lines:
        raise ValueError("selected host has no host_thinking content")
    return "\n".join(lines)


def render_host_expression_block(
    persona: RenderPersona,
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Render the concrete host-expression block injected into the user prompt.

    ``expression`` and its rules come from the selected external host.
    ``viewpoint_profile`` is not projected here; it already steered Selection.
    """
    lines: list[str] = []
    if persona.expression:
        label = (
            "Host 表达特征(expression)"
            if control_prompt_locale is ControlPromptLocale.zh
            else "Host expression"
        )
        lines.append(f"- {label}: {persona.expression}")
    lines.extend(f"- {rule}" for rule in persona.expression_rules)
    if not lines:
        raise ValueError("selected host has no host_expression content")
    return "\n".join(lines)


def render_chunk_position_block(
    *,
    chunk_index: int = 1,
    chunk_count: int = 1,
    segment_position: SegmentPosition | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Render the code-derived segment authority for one chunk.

    Standalone callers default to a one-chunk ``single`` batch. Batch and Console
    callers pass the explicit position derived from the complete ordered contract
    sequence.
    """
    position = _coerce_chunk_position(
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        segment_position=segment_position,
    )
    locale = coerce_control_prompt_locale(control_prompt_locale)
    description = _catalog(locale).segment_positions[position.segment_position]
    return "\n".join(
        (
            f"chunk_index: {position.chunk_index}",
            f"chunk_count: {position.chunk_count}",
            f"segment_position: {position.segment_position}",
            (
                f"段落权说明：{description}"
                if locale is ControlPromptLocale.zh
                else f"segment authority: {description}"
            ),
        )
    )


def render_style_examples_block(
    persona: RenderPersona,
    segment_position: SegmentPosition = "single",
    *,
    sample_key: str | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Render the style examples selected for this segment position.

    Style examples are prompt-only expression guidance. They never enter
    ``RenderContract`` and never authorize facts beyond the current materials.
    New v2 runs pass a stable per-run key and project one example for a normal
    position, or one opening plus one ending example for a single chunk. An
    omitted key preserves legacy full-pool reconstruction.
    """
    if (
        segment_position != "single"
        and segment_position not in persona.style_examples_by_position
    ):
        raise ValueError(
            f"selected host has no style examples for {segment_position!r}"
        )
    example_positions = set(persona.style_examples_by_position)
    if segment_position == "single" and not (
        "single" in example_positions or {"opening", "ending"} <= example_positions
    ):
        raise ValueError("selected host has no style examples for 'single'")
    locale = coerce_control_prompt_locale(control_prompt_locale)
    protocol = _catalog(locale).style_example_protocol
    if locale is ControlPromptLocale.zh:
        lines = [
            f"选择规则：{protocol.selection}",
            "",
            "使用规则：",
            *(f"- {rule}" for rule in protocol.usage),
        ]
    else:
        lines = [
            f"Selection rule: {protocol.selection}",
            "",
            "Usage rules:",
            *(f"- {rule}" for rule in protocol.usage),
        ]
    for index, (example_position, example) in enumerate(
        persona.style_examples_for(segment_position, sample_key=sample_key), start=1
    ):
        if locale is ControlPromptLocale.zh:
            labels = (
                f"示例 {index}（来源位置：{example_position}）",
                "[外部转换示例的输入｜不是当前材料]",
                "[外部转换示例的正文｜不是当前事实来源]",
                "[学习点]",
            )
        else:
            labels = (
                f"Example {index} (source position: {example_position})",
                "[EXTERNAL TRANSFORMATION INPUT | NOT CURRENT MATERIAL]",
                "[EXTERNAL TRANSFORMATION OUTPUT | NOT A CURRENT FACT SOURCE]",
                "[LESSON]",
            )
        lines.extend(
            (
                "",
                labels[0],
                labels[1],
                example.source,
                labels[2],
                example.render,
            )
        )
        # Only Host v1 owns a lesson; v2 lets the pair carry the demonstration.
        if example.lesson is not None:
            lines.extend((labels[3], example.lesson))
    return "\n".join(lines)


def render_chunk_user_prompt(
    *,
    template: str,
    contract: RenderContract,
    persona: RenderPersona,
    output_language: str,
    name_resolution: NameResolutionPlan | None = None,
    fact_context: RenderFactContext | None = None,
    block_overrides: Mapping[str, str] | None = None,
    chunk_index: int = 1,
    chunk_count: int = 1,
    segment_position: SegmentPosition | None = None,
    style_example_sample_key: str | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Fill the renderer user template with audited dynamic blocks.

    A standalone call is explicitly a one-chunk ``single`` segment by default.
    Callers projecting one member of a larger batch must pass its complete-batch
    position scalars.
    """
    replacements = build_render_blocks(
        contract=contract,
        persona=persona,
        name_resolution=name_resolution,
        fact_context=fact_context,
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        segment_position=segment_position,
        style_example_sample_key=style_example_sample_key,
        control_prompt_locale=control_prompt_locale,
    )
    replacements = apply_render_block_overrides(replacements, block_overrides)
    return substitute_render_blocks(
        bind_render_output_language(template, output_language),
        replacements,
        control_prompt_locale=control_prompt_locale,
    )


def bind_render_output_language(template: str, output_language: str) -> str:
    placeholder = "{{output_language}}"
    if not output_language.strip():
        raise ValueError("output_language must be non-empty")
    if template.count(placeholder) != 1:
        raise ValueError("render template must contain output_language exactly once")
    return template.replace(placeholder, output_language)


def apply_render_block_overrides(
    blocks: Mapping[str, str],
    overrides: Mapping[str, str] | None,
) -> dict[str, str]:
    """Apply the safe, chunk-invariant subset of render block overrides.

    Single-chunk labs may edit all seven audited blocks, but only host thinking
    and host expression are portable to a batch render. Position, its selected
    style examples, evidence and constraints must be rebuilt for every chunk.
    """
    result = dict(blocks)
    if not overrides:
        return result
    unknown = sorted(set(overrides) - set(RENDER_PORTABLE_BLOCK_KEYS))
    if unknown:
        raise ValueError(
            "full render block overrides only allow "
            + ", ".join(RENDER_PORTABLE_BLOCK_KEYS)
            + "; unsupported: "
            + ", ".join(unknown)
        )
    non_strings = sorted(key for key, value in overrides.items() if not isinstance(value, str))
    if non_strings:
        raise ValueError(
            "render block override values must be strings: " + ", ".join(non_strings)
        )
    result.update(overrides)
    return result


def build_render_blocks(
    *,
    contract: RenderContract,
    persona: RenderPersona,
    name_resolution: NameResolutionPlan | None = None,
    fact_context: RenderFactContext | None = None,
    chunk_index: int = 1,
    chunk_count: int = 1,
    segment_position: SegmentPosition | None = None,
    style_example_sample_key: str | None = None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Build the audited dynamic-block values for one contract.

    Exposed separately from substitution so tooling can inspect or override
    individual blocks before assembling the user prompt; the block key set is
    still audited against the rendering protocol catalog at substitution time.
    ``name_resolution`` is optional only for dry-run token estimation before a
    real plan exists; every actual render call has one.  Resolution is applied
    to temporary prompt copies only; persisted contracts and fact contexts stay raw.
    """
    control_prompt_locale = coerce_control_prompt_locale(control_prompt_locale)
    prompt_contract = (
        resolve_contract_names(contract, name_resolution)
        if name_resolution is not None
        else contract
    )
    prompt_context = (
        resolve_fact_context_names(
            fact_context,
            name_resolution,
            contract=contract,
        )
        if name_resolution is not None
        else fact_context
    )
    position = _coerce_chunk_position(
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        segment_position=segment_position,
    )
    materials_block = project_chunk(
        prompt_contract,
        control_prompt_locale=control_prompt_locale,
    )
    return {
        "chunk_position_block": render_chunk_position_block(
            chunk_index=position.chunk_index,
            chunk_count=position.chunk_count,
            segment_position=position.segment_position,
            control_prompt_locale=control_prompt_locale,
        ),
        "host_thinking_block": render_host_thinking_block(
            persona,
            control_prompt_locale=control_prompt_locale,
        ),
        "host_expression_block": render_host_expression_block(
            persona,
            control_prompt_locale=control_prompt_locale,
        ),
        "style_examples_block": render_style_examples_block(
            persona,
            position.segment_position,
            sample_key=style_example_sample_key,
            control_prompt_locale=control_prompt_locale,
        ),
        "fact_context_block": _fact_context_block(
            prompt_context,
            control_prompt_locale,
        ),
        "annotated_materials_block": materials_block,
        "forbidden_block": _forbidden_block(prompt_contract),
    }


def substitute_render_blocks(
    template: str,
    replacements: Mapping[str, str],
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Substitute audited dynamic blocks into the renderer user template.

    Rejects a block set that does not exactly match the catalog's
    ``dynamic_blocks``, a template missing any ``{{placeholder}}``, and any
    placeholder left unresolved after substitution.
    """
    _validate_render_blocks(replacements, control_prompt_locale)
    return substitute_prompt_blocks(
        template,
        replacements,
        prompt_name="render",
    )


def _validate_render_blocks(
    replacements: Mapping[str, str],
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> None:
    expected = {
        key for key, _description in _catalog(control_prompt_locale).dynamic_blocks
    }
    actual = set(replacements)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("extra: " + ", ".join(extra))
        raise ValueError("render prompt block mismatch (" + "; ".join(details) + ")")


def project_chunk(
    contract: RenderContract,
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    """Project one continuous material block with inline directive annotations.

    Artifact fields remain available upstream and on disk, but the model sees no
    node/atom labels, roles, treatments, tags, aliases, lineage or per-material
    markers. Every contract material appears exactly once in contract order.
    """
    blocks: list[str] = []
    grouped = _group_materials(contract)
    for node in sorted(contract.nodes, key=lambda n: n.order_in_chunk):
        if node.directive:
            marker = (
                f"【叙事提示｜{node.directive.strip()}】"
                if control_prompt_locale is ControlPromptLocale.zh
                else f"[NARRATIVE NOTE | {node.directive.strip()}]"
            )
            blocks.append(marker)
        for material in grouped.get(node.node_ref, ()):
            blocks.append(material.content)

    orphans = grouped.get(None, ())
    blocks.extend(material.content for material in orphans)

    return "\n\n".join(blocks) if blocks else "none"


def _group_materials(
    contract: RenderContract,
) -> dict[str | None, list[RenderContractMaterial]]:
    """Group materials under their owning node (contract order preserved).

    Materials whose ``node_ref`` matches no node are keyed under ``None`` so the
    projection still contains every contract material exactly once (the
    projection-materials-equal-contract-materials invariant)."""
    node_refs = {node.node_ref for node in contract.nodes}
    grouped: dict[str | None, list[RenderContractMaterial]] = {}
    for material in contract.materials:
        key = material.node_ref if material.node_ref in node_refs else None
        grouped.setdefault(key, []).append(material)
    return grouped


def _fact_context_block(
    context: RenderFactContext | None,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> str:
    header = (
        "计划去重提示（非事实、非命令，不能授权删除当前材料新增量）："
        if control_prompt_locale is ControlPromptLocale.zh
        else (
            "Planned-duplication hints (not facts or commands; never permission "
            "to remove new material contribution):"
        )
    )
    if context is None or not context.node_hints:
        return f"{header}\nnone"
    # Stable ids remain artifact lineage and are intentionally absent here.
    return header + "\n" + "\n".join(
        f"- {hint.established.strip()}" for hint in context.node_hints
    )


def _forbidden_block(contract: RenderContract) -> str:
    if not contract.forbidden_claims:
        return "none"
    return "\n".join(f"- {claim}" for claim in contract.forbidden_claims)


def _input_budget(config: RuntimeConfig) -> int | None:
    value = (config.stage_budgets.get(STAGE_RENDERING) or {}).get(RENDER_INPUT_BUDGET_KEY)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def derive_render_chunk_positions(
    contracts: Sequence[RenderContract],
) -> dict[str, RenderChunkPosition]:
    """Derive 1-based position scalars from the complete ordered contract batch."""
    ordered = sorted(contracts, key=lambda item: item.order_index)
    chunk_ids = [contract.chunk_id for contract in ordered]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("render contracts must have unique chunk ids")
    order_indices = [contract.order_index for contract in ordered]
    if len(order_indices) != len(set(order_indices)):
        raise ValueError("render contracts must have unique order_index values")
    chunk_count = len(ordered)
    return {
        contract.chunk_id: _coerce_chunk_position(
            chunk_index=chunk_index,
            chunk_count=chunk_count,
        )
        for chunk_index, contract in enumerate(ordered, start=1)
    }


def _coerce_chunk_position(
    *,
    chunk_index: int,
    chunk_count: int,
    segment_position: SegmentPosition | None = None,
) -> RenderChunkPosition:
    if (
        isinstance(chunk_index, bool)
        or isinstance(chunk_count, bool)
        or not isinstance(chunk_index, int)
        or not isinstance(chunk_count, int)
    ):
        raise ValueError("chunk_index and chunk_count must be integers")
    if chunk_count < 1:
        raise ValueError("chunk_count must be at least 1")
    if chunk_index < 1 or chunk_index > chunk_count:
        raise ValueError("chunk_index must be within 1..chunk_count")
    if chunk_count == 1:
        derived: SegmentPosition = "single"
    elif chunk_index == 1:
        derived = "opening"
    elif chunk_index == chunk_count:
        derived = "ending"
    else:
        derived = "middle"
    if segment_position is not None and segment_position != derived:
        raise ValueError(
            "segment_position does not match chunk_index/chunk_count "
            f"({segment_position!r} != {derived!r})"
        )
    return RenderChunkPosition(
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        segment_position=derived,
    )


def _artifact_metadata(
    contract: RenderContract,
    persona: RenderPersona,
    prompt_version: str,
    call_id: str,
    *,
    fact_context: RenderFactContext | None,
    name_resolution: NameResolutionPlan,
    output_language: str,
    chunk_position: RenderChunkPosition,
    style_example_sample_key: str,
) -> dict[str, Any]:
    """Code-owned run metadata for one chunk (never model-authored)."""
    metadata: dict[str, Any] = {
        "persona_id": persona.persona_id,
        "prompt_version": prompt_version,
        "call_id": call_id,
        "node_refs": [node.node_ref for node in contract.nodes],
        "source_node_ids": [node.source_node_id for node in contract.nodes],
        "name_resolution_plan_id": name_resolution.plan_id,
        "output_language": output_language,
        "chunk_index": chunk_position.chunk_index,
        "chunk_count": chunk_position.chunk_count,
        "segment_position": chunk_position.segment_position,
        "style_example_sample_key": style_example_sample_key,
    }
    if persona.host_version is not None:
        metadata["host_version"] = persona.host_version
    if persona.host_content_hash is not None:
        metadata["host_content_hash"] = persona.host_content_hash
    if fact_context is not None:
        metadata["fact_context"] = {
            "semantics": fact_context.metadata.get("semantics"),
            "context_tokens": fact_context.metadata.get("context_tokens"),
        }
    return metadata


def _render_report(
    *,
    kind: str,
    message: str,
    chunk_id: str,
    record: LlmCallRecord,
    level: str = "warning",
    usable: bool = True,
    metadata: dict[str, Any] | None = None,
) -> AuditReportItem:
    return AuditReportItem(
        issue_id=issue_id(STAGE_RENDERING, kind, chunk_id, message),
        stage=STAGE_RENDERING,
        kind=kind,
        level=level,
        message=message,
        usable=usable,
        artifact_ref=chunk_id,
        trace_refs=[chunk_id],
        call_id=record.call_id,
        raw_response_path=record.raw_response_path,
        metadata=metadata or {},
    )
