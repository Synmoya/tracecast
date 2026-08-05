"""FastAPI app for the local debugging console (thin routing layer).

All behavior lives in the service modules (``inventory``/``ingestion_run``/
``derive``/``chunklab``/``host_build``/``diffs``); this module only wires routes,
request models, and error mapping.
GET endpoints never spend; POST endpoints follow the two-phase ``confirm``
protocol enforced inside the services. The app is intended for
``127.0.0.1``-only use -- there is no auth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from tracecast.host_builder import DEFAULT_PIECE_MAX_TOKENS
from tracecast.llm import call_llm
from tracecast.storage import CANDIDATE_GRAPH, CHUNK_ARTIFACTS, SELECTION_PLAN, RunStore

from . import chunklab as chunklab_service
from . import (
    config_editor,
    diffs,
    host_catalog,
    inventory,
    project_drafts,
    reports,
    setup,
    stage_trace,
)
from . import derive as derive_service
from . import host_build as host_build_service
from . import ingestion_run as ingestion_run_service
from .context import ConsoleContext
from .errors import ConsoleError, NotFound
from .host_build import UploadedPreferenceFile
from .overrides import ConfigOverrides, PersonaPatch, reload_component_caches
from .project_drafts import UploadedProjectFile

_STATIC_DIR = Path(__file__).parent / "static"
_BUNDLED_PROFILES = Path(__file__).parent / "resources" / "source_profile_templates.json"


# --- request models -----------------------------------------------------------


class _SpendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_run_id: str
    config_overrides: ConfigOverrides | None = None
    run_label: str | None = None
    confirm: bool = False


class DeriveSelectionRequest(_SpendRequest):
    host_id: str
    persona_patch: PersonaPatch | None = None


class DeriveNodeEditRequest(_SpendRequest):
    pass


class DeriveRenderRequest(_SpendRequest):
    persona_patch: PersonaPatch | None = None
    render_block_overrides: dict[str, str] = Field(default_factory=dict)
    name_resolution_mode: Literal["overrides_only", "name_table"] | None = None


class ChunklabRenderRequest(_SpendRequest):
    chunk_id: str
    blocks: dict[str, str] = Field(default_factory=dict)


class CreateIngestionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    host_id: str | None = None
    source_ids: list[str] | None = None
    output_language: str | None = None
    name_resolution_mode: Literal["overrides_only", "name_table"] | None = None
    target_output_kchars: float | None = Field(default=None, gt=0)
    selection_human_directive: list[str] | None = None
    persona_patch: PersonaPatch | None = None
    config_overrides: ConfigOverrides | None = None
    run_label: str | None = None
    mode: Literal["ingestion", "full"] = "ingestion"
    confirm: bool = False


class HostBuildFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    content: str


class HostBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[HostBuildFileRequest]
    host_id: str
    display_name: str | None = None
    version: str | None = None
    seed: int | None = None
    piece_max_tokens: int = Field(default=DEFAULT_PIECE_MAX_TOKENS, gt=0)
    config_overrides: ConfigOverrides | None = None
    confirm: bool = False


class RuntimeSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_url: str
    api_key: SecretStr = Field(json_schema_extra={"writeOnly": True})
    standard_model: str
    selection_model: str | None = None
    completion_path: str = "/chat/completions"
    max_output_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    extra_body: dict[str, Any] | None = None
    confirm: bool = False


class ProjectDraftFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    data_base64: str
    encoding: str | None = None
    profile: str | None = None
    language: str | None = None


class CreateProjectDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    output_language: str
    name_resolution_mode: Literal["overrides_only", "name_table"] = "overrides_only"
    mode: Literal["manual", "auto"]
    files: list[ProjectDraftFileRequest]


class CommitProjectDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[dict[str, Any]] | None = None


# --- app factory ----------------------------------------------------------------


def create_app(
    *,
    runs_root: str | Path = "runs",
    projects_dir: str | Path = "projects",
    hosts_dir: str | Path = "hosts",
    config_path: str | Path = "configs/runtime.json",
    profiles_path: str | Path = "configs/source_profile_templates.json",
    env_path: str | Path | None = None,
    call: Any = call_llm,
) -> FastAPI:
    """Build the console app; ``call`` is the injectable no-spend seam for tests."""
    resolved_config_path = Path(config_path)
    resolved_profiles_path = Path(profiles_path)
    if (
        resolved_profiles_path == Path("configs/source_profile_templates.json")
        and not resolved_profiles_path.is_file()
    ):
        resolved_profiles_path = _BUNDLED_PROFILES
    if env_path is None:
        resolved_env_path = (
            resolved_config_path.parent.parent / ".env"
            if resolved_config_path.parent.name == "configs"
            else resolved_config_path.parent / ".env"
        )
    else:
        resolved_env_path = Path(env_path)
    ctx = ConsoleContext(
        runs_root=Path(runs_root),
        projects_dir=Path(projects_dir),
        hosts_dir=Path(hosts_dir),
        config_path=resolved_config_path,
        env_path=resolved_env_path,
        profiles_path=resolved_profiles_path,
        call=call,
    )
    app = FastAPI(title="tracecast console", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def _disable_static_cache(request: Request, call_next: Any) -> Any:
        """Prevent mixed versions of the no-build ES-module graph during development."""

        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Clear-Site-Data"] = '"cache"'
        return response

    @app.exception_handler(ConsoleError)
    async def _console_error(_request: Request, exc: ConsoleError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": str(exc),
                "error": {"code": exc.code, "message": str(exc)},
            },
        )

    # -- static page ------------------------------------------------------------

    @app.get("/")
    def index() -> Any:
        page = _STATIC_DIR / "index.html"
        if page.exists():
            return FileResponse(page)
        return PlainTextResponse("tracecast console: static/index.html missing", status_code=404)

    # -- read-only --------------------------------------------------------------

    @app.get("/api/setup")
    def setup_view() -> dict[str, Any]:
        return setup.setup_status(ctx)

    @app.get("/api/projects")
    def projects() -> list[dict[str, Any]]:
        return inventory.list_projects(ctx.projects_dir)

    @app.get("/api/hosts")
    def hosts() -> list[dict[str, Any]]:
        return host_catalog.host_summaries(ctx)

    @app.get("/api/hosts/{host_id}")
    def host(host_id: str) -> dict[str, Any]:
        return host_catalog.host_detail(ctx, host_id)

    @app.get("/api/projects/{project_id}/sources")
    def project_sources(project_id: str) -> list[dict[str, Any]]:
        return inventory.list_project_sources(ctx.projects_dir, project_id)

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return inventory.list_runs(ctx.runs_root)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        return inventory.run_inventory(inventory.open_run(ctx.runs_root, run_id))

    @app.get("/api/runs/{run_id}/artifact")
    def run_artifact(run_id: str, relpath: str, limit: int | None = None) -> dict[str, Any]:
        store = inventory.open_run(ctx.runs_root, run_id)
        return inventory.read_artifact(store, relpath, limit=limit)

    @app.get("/api/runs/{run_id}/ingestion/audit")
    def run_ingestion_audit(run_id: str, sample_size: int = 16) -> dict[str, Any]:
        store = inventory.open_run(ctx.runs_root, run_id)
        return inventory.ingestion_audit(store, sample_size=sample_size)

    @app.get("/api/runs/{run_id}/reports/{stage}")
    def run_stage_report(run_id: str, stage: str) -> dict[str, Any]:
        return reports.stage_report(ctx.runs_root, run_id, stage)

    @app.get("/api/runs/{run_id}/trace")
    def run_stage_trace(run_id: str, chunk_id: str | None = None) -> dict[str, Any]:
        return stage_trace.trace_index(ctx, run_id, chunk_id=chunk_id)

    @app.get("/api/runs/{run_id}/trace/{stage}/{unit_id}")
    def run_stage_trace_unit(run_id: str, stage: str, unit_id: str) -> dict[str, Any]:
        return stage_trace.trace_unit(ctx, run_id, stage, unit_id)

    @app.get("/api/runs/{run_id}/llm/calls")
    def run_llm_calls(run_id: str) -> list[dict[str, Any]]:
        return inventory.llm_calls(inventory.open_run(ctx.runs_root, run_id))

    @app.get("/api/runs/{run_id}/llm/calls/{call_id}/raw")
    def run_llm_raw(run_id: str, call_id: str, role: str = "request") -> dict[str, Any]:
        store = inventory.open_run(ctx.runs_root, run_id)
        return inventory.raw_payload(store, call_id, role)

    @app.get("/api/runs/{run_id}/chunks")
    def run_chunks(run_id: str) -> list[dict[str, Any]]:
        return inventory.list_chunks(inventory.open_run(ctx.runs_root, run_id))

    @app.get("/api/runs/{run_id}/chunks/{chunk_id}/blocks")
    def run_chunk_blocks(run_id: str, chunk_id: str) -> dict[str, Any]:
        return chunklab_service.chunk_blocks(ctx, run_id, chunk_id)

    @app.get("/api/config")
    def config_view() -> dict[str, Any]:
        config = ctx.load_config()
        return {
            "config_path": str(ctx.config_path),
            "providers": {
                name: {
                    "base_url": provider.base_url,
                    "chat_completions_path": provider.chat_completions_path,
                    "max_output_tokens_field": provider.max_output_tokens_field,
                    "credentials_configured": bool(provider.keys),
                }
                for name, provider in config.providers.items()
            },
            "routes": {
                name: route.model_dump(exclude_none=True)
                for name, route in config.routes.items()
            },
            "stage_budgets": config.stage_budgets,
            "editor": config_editor.editor_schema(config),
        }

    # -- diffs (free) -------------------------------------------------------------

    @app.get("/api/diff/selection")
    def diff_selection_view(run_a: str, run_b: str) -> dict[str, Any]:
        store_a = inventory.open_run(ctx.runs_root, run_a)
        store_b = inventory.open_run(ctx.runs_root, run_b)
        plan_a = _read_selection(ctx, store_a)
        plan_b = _read_selection(ctx, store_b)
        labels: dict[str, str] = {}
        for store in (store_a, store_b):
            labels.update(_graph_labels(ctx, store))
        result = diffs.diff_selection(plan_a, plan_b, labels=labels)
        result["plan_a"]["run_id"] = run_a
        result["plan_b"]["run_id"] = run_b
        return result

    @app.get("/api/diff/chunks")
    def diff_chunks_view(run_a: str, chunk_a: str, run_b: str, chunk_b: str) -> dict[str, Any]:
        text_a = _read_chunk_text(inventory.open_run(ctx.runs_root, run_a), chunk_a)
        text_b = _read_chunk_text(inventory.open_run(ctx.runs_root, run_b), chunk_b)
        result = diffs.diff_texts(text_a, text_b)
        result["a"] = {"run_id": run_a, "chunk_id": chunk_a}
        result["b"] = {"run_id": run_b, "chunk_id": chunk_b}
        return result

    @app.get("/api/diff/blocks")
    def diff_blocks_view(run_a: str, chunk_a: str, run_b: str, chunk_b: str) -> dict[str, Any]:
        blocks_a = _effective_blocks(ctx, run_a, chunk_a)
        blocks_b = _effective_blocks(ctx, run_b, chunk_b)
        return {
            "a": {"run_id": run_a, "chunk_id": chunk_a},
            "b": {"run_id": run_b, "chunk_id": chunk_b},
            "blocks": diffs.diff_blocks(blocks_a, blocks_b, label_a=run_a, label_b=run_b),
        }

    @app.get("/api/diff/prompt")
    def diff_prompt_view(run_a: str, call_a: str, run_b: str, call_b: str) -> dict[str, Any]:
        request_a = _read_raw_request(inventory.open_run(ctx.runs_root, run_a), call_a)
        request_b = _read_raw_request(inventory.open_run(ctx.runs_root, run_b), call_b)
        result = diffs.diff_prompt_requests(
            request_a, request_b, label_a=f"{run_a}:{call_a}", label_b=f"{run_b}:{call_b}"
        )
        result["a"] = {"run_id": run_a, "call_id": call_a}
        result["b"] = {"run_id": run_b, "call_id": call_b}
        return result

    # -- spend (two-phase confirm) --------------------------------------------------

    @app.put("/api/setup/runtime")
    def setup_runtime(payload: RuntimeSetupRequest, request: Request) -> dict[str, Any]:
        setup.authorize_setup_write(
            client_host=request.client.host if request.client else None,
            origin=request.headers.get("origin"),
            request_url=str(request.url),
        )
        return setup.update_runtime(
            ctx,
            gateway_url=payload.gateway_url,
            api_key=payload.api_key.get_secret_value(),
            standard_model=payload.standard_model,
            selection_model=payload.selection_model,
            completion_path=payload.completion_path,
            max_output_tokens_field=payload.max_output_tokens_field,
            extra_body=payload.extra_body,
            confirm=payload.confirm,
        )

    @app.post("/api/project-drafts")
    def create_project_draft(payload: CreateProjectDraftRequest) -> dict[str, Any]:
        return project_drafts.create_project_draft(
            ctx,
            project_id=payload.project_id,
            output_language=payload.output_language,
            name_resolution_mode=payload.name_resolution_mode,
            mode=payload.mode,
            files=[
                UploadedProjectFile(
                    name=row.name,
                    data_base64=row.data_base64,
                    encoding=row.encoding,
                    profile=row.profile,
                    language=row.language,
                )
                for row in payload.files
            ],
        )

    @app.post("/api/project-drafts/{draft_id}/estimate")
    def estimate_project_draft(draft_id: str) -> dict[str, Any]:
        return project_drafts.estimate_project_draft(ctx, draft_id)

    @app.post("/api/project-drafts/{draft_id}/confirm")
    def confirm_project_draft(draft_id: str) -> dict[str, Any]:
        return project_drafts.confirm_project_draft(ctx, draft_id)

    @app.post("/api/project-drafts/{draft_id}/commit")
    def commit_project_draft(
        draft_id: str, payload: CommitProjectDraftRequest
    ) -> dict[str, Any]:
        return project_drafts.commit_project_draft(
            ctx, draft_id, sources=payload.sources
        )

    @app.post("/api/hosts/build")
    def build_host(payload: HostBuildRequest) -> dict[str, Any]:
        return host_build_service.build_host(
            ctx,
            files=[
                UploadedPreferenceFile(name=row.name, content=row.content)
                for row in payload.files
            ],
            host_id=payload.host_id,
            display_name=payload.display_name,
            version=payload.version,
            seed=payload.seed,
            piece_max_tokens=payload.piece_max_tokens,
            config_overrides=payload.config_overrides,
            confirm=payload.confirm,
        )

    @app.post("/api/runs/ingestion")
    def create_ingestion_run(payload: CreateIngestionRunRequest) -> dict[str, Any]:
        return ingestion_run_service.create_ingestion_run(
            ctx,
            project_id=payload.project_id,
            host_id=payload.host_id,
            source_ids=payload.source_ids,
            output_language=payload.output_language,
            name_resolution_mode=payload.name_resolution_mode,
            target_output_kchars=payload.target_output_kchars,
            selection_human_directive=payload.selection_human_directive,
            persona_patch=payload.persona_patch,
            config_overrides=payload.config_overrides,
            run_label=payload.run_label,
            mode=payload.mode,
            confirm=payload.confirm,
        )

    @app.post("/api/derive/selection")
    def derive_selection(payload: DeriveSelectionRequest) -> dict[str, Any]:
        return derive_service.derive_selection(
            ctx,
            base_run_id=payload.base_run_id,
            host_id=payload.host_id,
            persona_patch=payload.persona_patch,
            config_overrides=payload.config_overrides,
            run_label=payload.run_label,
            confirm=payload.confirm,
        )

    @app.post("/api/derive/node-edit")
    def derive_node_edit(payload: DeriveNodeEditRequest) -> dict[str, Any]:
        return derive_service.derive_node_edit(
            ctx,
            base_run_id=payload.base_run_id,
            config_overrides=payload.config_overrides,
            run_label=payload.run_label,
            confirm=payload.confirm,
        )

    @app.post("/api/derive/render")
    def derive_render(payload: DeriveRenderRequest) -> dict[str, Any]:
        return derive_service.derive_render(
            ctx,
            base_run_id=payload.base_run_id,
            persona_patch=payload.persona_patch,
            config_overrides=payload.config_overrides,
            render_block_overrides=payload.render_block_overrides,
            name_resolution_mode=payload.name_resolution_mode,
            run_label=payload.run_label,
            confirm=payload.confirm,
        )

    @app.post("/api/chunklab/render")
    def chunklab_render(payload: ChunklabRenderRequest) -> dict[str, Any]:
        return chunklab_service.chunklab_render(
            ctx,
            base_run_id=payload.base_run_id,
            chunk_id=payload.chunk_id,
            blocks=payload.blocks,
            config_overrides=payload.config_overrides,
            run_label=payload.run_label,
            confirm=payload.confirm,
        )

    # -- jobs / ops -------------------------------------------------------------------

    @app.get("/api/jobs")
    def jobs() -> list[dict[str, Any]]:
        return ctx.registry.list()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        return ctx.registry.get(job_id).snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> dict[str, Any]:
        return ctx.registry.request_cancel(job_id).snapshot()

    @app.post("/api/reload")
    def reload_caches() -> dict[str, Any]:
        return {"cleared": reload_component_caches()}

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    return app


# --- route helpers -------------------------------------------------------------------


def _read_selection(ctx: ConsoleContext, store: RunStore) -> Any:
    sources = inventory.resolve_artifact_sources(
        ctx.runs_root,
        store.run_id,
        (SELECTION_PLAN,),
    )
    return sources[SELECTION_PLAN.relpath].read_model(SELECTION_PLAN)


def _graph_labels(ctx: ConsoleContext, store: RunStore) -> dict[str, str]:
    """Candidate summaries for diff labels; follow a derived run's base if needed."""
    sources = inventory.resolve_artifact_sources(
        ctx.runs_root,
        store.run_id,
        (CANDIDATE_GRAPH,),
        required=False,
    )
    source = sources.get(CANDIDATE_GRAPH.relpath)
    if source is None:
        return {}
    graph = source.read_model(CANDIDATE_GRAPH)
    return {node.candidate_id: node.summary for node in graph.nodes}


def _read_chunk_text(store: RunStore, chunk_id: str) -> str:
    sources = inventory.resolve_artifact_sources(
        store.run_dir.parent,
        store.run_id,
        (CHUNK_ARTIFACTS,),
        required=False,
    )
    origin = sources.get(CHUNK_ARTIFACTS.relpath)
    if origin is None:
        raise NotFound(f"run lineage {store.run_id!r} has no {CHUNK_ARTIFACTS.relpath}")
    for chunk in origin.read_records(CHUNK_ARTIFACTS):
        if chunk.chunk_id == chunk_id:
            return chunk.text
    raise NotFound(f"chunk {chunk_id!r} not found in run {store.run_id!r}")


def _read_raw_request(store: RunStore, call_id: str) -> dict[str, Any]:
    payload = inventory.raw_payload(store, call_id, "request")
    if payload.get("format") != "json":
        raise NotFound(f"raw request for call {call_id!r} is not a JSON envelope")
    return payload["data"]


def _effective_blocks(ctx: ConsoleContext, run_id: str, chunk_id: str) -> dict[str, str]:
    """Effective blocks loaded by Chunk Lab, including recorded experiment input."""
    payload = chunklab_service.chunk_blocks(ctx, run_id, chunk_id)
    return dict(payload["blocks"])
