"""Server-derived baseline inputs shared by Chunk Lab reads and writes."""

from __future__ import annotations

from dataclasses import dataclass

from tracecast.control_prompt import control_prompt_locale_from_record
from tracecast.domain import (
    ChunkArtifact,
    NameResolutionPlan,
    RenderContract,
    RenderFactContext,
)
from tracecast.hosts import HostProfile
from tracecast.prompt_components import load_rendering_protocol_catalog
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.rendering import (
    RENDER_ROUTE,
    RenderChunkPosition,
    RenderPersona,
    bind_render_output_language,
    build_render_blocks,
    coerce_persona,
    derive_render_chunk_positions,
)
from tracecast.storage import (
    CHUNK_ARTIFACTS,
    NAME_RESOLUTION_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    RunStore,
    RuntimeConfig,
)

from .context import ConsoleContext, resolve_lineage_effective
from .errors import BadRequest, NotFound
from .host_catalog import resolve_run_host
from .inventory import resolve_artifact_sources

CONTRACT_SNAPSHOT_RELPATH = "console/contract.json"


@dataclass(frozen=True)
class ChunkBaseline:
    contracts: tuple[RenderContract, ...]
    contract: RenderContract
    host: HostProfile
    persona: RenderPersona
    blocks: dict[str, str]
    system_prompt: str
    template: str
    prompt_version: str
    baseline_artifact: ChunkArtifact | None
    fact_context: RenderFactContext | None
    name_resolution: NameResolutionPlan
    output_language: str
    chunk_position: RenderChunkPosition
    artifact_sources: dict[str, str]


def load_chunk_baseline(
    ctx: ConsoleContext, store: RunStore, chunk_id: str, config: RuntimeConfig
) -> ChunkBaseline:
    sources = resolve_artifact_sources(
        ctx.runs_root,
        store.run_id,
        (
            RENDER_CONTRACTS,
            CHUNK_ARTIFACTS,
            NAME_RESOLUTION_PLAN,
            RENDER_FACT_CONTEXTS,
        ),
        required=False,
    )
    contract_store = sources.get(RENDER_CONTRACTS.relpath)
    if contract_store is None:
        raise NotFound(f"run lineage {store.run_id!r} has no {RENDER_CONTRACTS.relpath}")
    contracts = sorted(
        contract_store.read_records(RENDER_CONTRACTS), key=lambda item: item.order_index
    )
    contract = next((item for item in contracts if item.chunk_id == chunk_id), None)
    if contract is None:
        raise NotFound(f"chunk {chunk_id!r} not found in {RENDER_CONTRACTS.relpath}")

    chunk_store = sources.get(CHUNK_ARTIFACTS.relpath)
    chunks = chunk_store.read_records(CHUNK_ARTIFACTS) if chunk_store is not None else []
    baseline = next((chunk for chunk in chunks if chunk.chunk_id == chunk_id), None)

    lineage_effective = resolve_lineage_effective(ctx, store.run_id)
    plan_store = sources.get(NAME_RESOLUTION_PLAN.relpath)
    if plan_store is None:
        raise NotFound(f"run lineage {store.run_id!r} has no {NAME_RESOLUTION_PLAN.relpath}")
    name_resolution = plan_store.read_model(NAME_RESOLUTION_PLAN)
    if name_resolution.project_id != contract.project_id:
        raise BadRequest("NameResolutionPlan project_id does not match the render contract")
    lineage_output_language = lineage_effective.get("output_language")
    if (
        lineage_output_language is not None
        and name_resolution.output_language != lineage_output_language
    ):
        raise BadRequest("NameResolutionPlan output_language does not match run lineage")
    if name_resolution.contract_ids != [item.contract_id for item in contracts]:
        raise BadRequest(
            "NameResolutionPlan contract coverage/order does not match "
            "the render-contract artifact"
        )
    fact_context = None
    fact_store = sources.get(RENDER_FACT_CONTEXTS.relpath)
    if fact_store is not None:
        fact_context = next(
            (
                item
                for item in fact_store.read_records(RENDER_FACT_CONTEXTS)
                if item.chunk_id == chunk_id
            ),
            None,
        )

    host = resolve_run_host(
        ctx,
        store.run_id,
        contract.persona_id,
        output_language=name_resolution.output_language,
    )
    persona = coerce_persona(host)
    chunk_position = derive_render_chunk_positions(contracts)[contract.chunk_id]
    style_example_sample_key = (
        baseline.metadata.get("style_example_sample_key")
        if baseline is not None
        else f"{store.run_id}:{contract.contract_id}"
    )
    blocks = build_render_blocks(
        contract=contract,
        persona=persona,
        name_resolution=name_resolution,
        fact_context=fact_context,
        chunk_index=chunk_position.chunk_index,
        chunk_count=chunk_position.chunk_count,
        segment_position=chunk_position.segment_position,
        style_example_sample_key=style_example_sample_key,
    )
    control_prompt_locale = control_prompt_locale_from_record(
        lineage_effective.get("control_prompt_decision")
    )
    system_prompt, system_version = load_system_prompt(
        RENDER_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    template, user_version = load_user_prompt(
        RENDER_ROUTE, config, control_prompt_locale=control_prompt_locale
    )
    catalog = load_rendering_protocol_catalog()
    return ChunkBaseline(
        contracts=tuple(contracts),
        contract=contract,
        host=host,
        persona=persona,
        blocks=blocks,
        system_prompt=system_prompt,
        template=bind_render_output_language(template, name_resolution.output_language),
        prompt_version=(
            f"{system_version}+{user_version}+{catalog.version}+{host.version}"
        ),
        baseline_artifact=baseline,
        fact_context=fact_context,
        name_resolution=name_resolution,
        output_language=name_resolution.output_language,
        chunk_position=chunk_position,
        artifact_sources={relpath: origin.run_id for relpath, origin in sources.items()},
    )
