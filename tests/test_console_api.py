"""Console API tests: synthetic base run + fake ``call`` seam, zero spend.

The fixture builds a real artifact layout with ``RunStore`` (graph, selection,
node-edit outputs, packed contracts, chunk artifacts with matching raw request
payloads) plus a minimal project dir and a runtime config pointing at tiny
versioned prompt templates -- everything ``create_app`` needs, entirely inside
``tmp_path``. All spend endpoints run against an injected fake provider call.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tracecast.console.app import create_app  # noqa: E402
from tracecast.console.context import (  # noqa: E402
    ConsoleContext,
    resolve_lineage_effective,
)
from tracecast.console.errors import Conflict  # noqa: E402
from tracecast.console.jobs import JobRegistry  # noqa: E402
from tracecast.control_prompt import (  # noqa: E402
    ControlPromptSample,
    resolve_control_prompt_locale,
)
from tracecast.domain import (  # noqa: E402
    AssemblyReport,
    AuditReportItem,
    CandidateGraph,
    CandidateNode,
    ChunkArtifact,
    ContentKind,
    LlmCallRecord,
    LlmWorkUnitRef,
    MaterialAtom,
    MaterialEdit,
    NameResolutionPlan,
    NameResolutionRule,
    NodeEditOperation,
    NodeEditPlan,
    NodeEditResult,
    Provenance,
    ProviderStatus,
    RenderFactContext,
    RenderFactContextNodeHint,
    Role,
    SelectionDecision,
    SelectionPlan,
    SelectionStatus,
    SourcePiece,
    SourceWindow,
    TrustTier,
    estimate_tokens,
)
from tracecast.hosts import HostProfile  # noqa: E402
from tracecast.packing import DEFAULT_CHUNK_POLICY, pack  # noqa: E402
from tracecast.pipeline import RunResult  # noqa: E402
from tracecast.prompts import load_system_prompt, load_user_prompt  # noqa: E402
from tracecast.rendering import (  # noqa: E402
    coerce_persona,
    derive_render_chunk_positions,
    render_chunk_user_prompt,
)
from tracecast.storage import (  # noqa: E402
    ASSEMBLY_REPORT,
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    EDITED_MATERIALS,
    LLM_CALL_RECORDS,
    MATERIAL_ATOMS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SCRIPT,
    SELECTION_PLAN,
    SOURCE_PIECES,
    SOURCE_SNAPSHOTS,
    SOURCE_WINDOWS,
    RunStore,
    RuntimeConfig,
    content_hash,
    sha256_json,
)

BASE_RUN = "proj-base"
HOST = {
    "schema_version": "tracecast-host/1",
    "host_id": "host",
    "display_name": "测试 Host",
    "version": "test_host@v1",
    "output_language": "zh-CN",
    "host_thinking": {
        "viewpoint": "以结构变化讲故事",
        "prefer": [],
        "avoid": [],
        "human_directive": [],
        "rules": ["只围绕材料形成判断。"],
    },
    "host_expression": {
        "description": "口语化、短句",
        "rules": ["句式自然，不写总结腔。"],
    },
    "style_examples_by_position": {
        position: [
            {
                "source": f"{position} 虚构材料 {index}",
                "render": f"{position} 正文示例 {index}",
                "lesson": f"{position} 学习点 {index}",
            }
            for index in (1, 2)
        ]
        for position in ("opening", "middle", "ending", "single")
    },
}
HOST_PROFILE = HostProfile.model_validate(HOST)

RENDER_USER_TEMPLATE = """<!-- version: test_render_user@v1 -->
目标语言：{{output_language}}
{{chunk_position_block}}
{{host_thinking_block}}
{{host_expression_block}}
{{style_examples_block}}
{{fact_context_block}}
{{annotated_materials_block}}
{{forbidden_block}}
"""

SELECTOR_USER_TEMPLATE = """<!-- version: test_selector_user@v1 -->
{{source_token_estimate_line}}
{{target_output_estimate_line}}
{{persona_block}}
{{source_aliases}}
{{narrative_function_descriptions}}
{{graph_blocks}}
"""


class FakeCall:
    """Provider seam stub: records kwargs, answers per operation_route."""

    def __init__(self, responses: dict[str, str], gate: threading.Event | None = None) -> None:
        self.responses = responses
        self.calls: list[dict] = []
        self.gate = gate

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        route = kwargs["operation_route"]
        record = LlmCallRecord(
            call_id=f"fake-{len(self.calls)}",
            stage=kwargs.get("stage") or route,
            operation_route=route,
            provider="test",
            model="fake",
            input_hash="sha256:fake",
            provider_status=ProviderStatus.success,
            created_at=datetime.now(tz=UTC),
        )
        if route in self.responses:
            return self.responses[route], record
        defaults = {
            "name_table": json.dumps(
                {"replacements": [], "protected_surfaces": []}
            )
        }
        return defaults[route], record


def _atom(
    atom_id: str,
    content: str,
    *,
    source_order: int = 0,
    piece_refs: list[str] | None = None,
) -> MaterialAtom:
    return MaterialAtom(
        id=atom_id,
        content=content,
        language="zh",
        content_kind=ContentKind.narration,
        source_order=source_order,
        trust_tier=TrustTier.high,
        provenance=Provenance(
            source_ref="s1",
            source_type="local_text",
            source_class="primary",
            source_content_hash="sha256:src",
            piece_refs=piece_refs or [],
        ),
    )


def _prompt_file(path: Path, version: str, body: str) -> Path:
    path.write_text(f"<!-- version: {version} -->\n{body}", encoding="utf-8")
    return path


def build_env(tmp_path: Path) -> dict:
    """Materialize project + config + a complete synthetic base run."""
    prompts = tmp_path / "prompt"
    prompts.mkdir()
    render_sys = _prompt_file(prompts / "render.system.md", "test_render_sys@v1", "渲染系统层。")
    render_user = prompts / "render.user.md"
    render_user.write_text(RENDER_USER_TEMPLATE, encoding="utf-8")
    context_sys = _prompt_file(
        prompts / "context.system.md", "test_context_sys@v1", "联系事实位置。"
    )
    context_user = _prompt_file(
        prompts / "context.user.md",
        "test_context_user@v1",
        "{{output_language}}\n{{node_outline}}",
    )
    name_table_sys = _prompt_file(
        prompts / "name-table.system.md",
        "test_name_table_sys@v1",
        "从完整入选材料编辑专名表，只输出约定 JSON。",
    )
    name_table_user = _prompt_file(
        prompts / "name-table.user.md",
        "test_name_table_user@v1",
        (
            "{{output_language}}\n{{max_rules}}\n"
            "{{max_aliases_per_rule}}\n{{materials_block}}"
        ),
    )
    selector_sys = _prompt_file(
        prompts / "selector.system.md", "test_selector_sys@v1", "选择系统层。"
    )
    selector_user = prompts / "selector.user.md"
    selector_user.write_text(SELECTOR_USER_TEMPLATE, encoding="utf-8")
    selector_directive_sys = _prompt_file(
        prompts / "selector-directive.system.md",
        "test_selector_directive_sys@v1",
        "为完整路径生成导演手记。",
    )
    selector_directive_user = _prompt_file(
        prompts / "selector-directive.user.md",
        "test_selector_directive_user@v1",
        "{{persona_block}}\n{{path_blocks}}",
    )
    ne_sys = _prompt_file(prompts / "ne.system.md", "test_ne_sys@v1", "节点编辑系统层。")
    ne_user = _prompt_file(
        prompts / "ne.user.md",
        "test_ne_user@v1",
        (
            "候选 {{candidate_alias_range}}\n"
            "材料 {{material_alias_range}}\n"
            "来源 {{source_aliases}}\n"
            "字段 {{field_notes}}\n"
            "{{node_blocks}}"
        ),
    )
    ne_repair_user = _prompt_file(
        prompts / "ne.repair.user.md",
        "test_ne_repair_user@v1",
        "失败 {{initial_failure}}\n{{node_edit_prompt}}",
    )

    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "test": {
                        "base_url": "http://localhost:1",
                        "keys": [{"key_id": "k1", "api_key_env": "TC_TEST_KEY"}],
                    }
                },
                "routes": {
                    "selector_path_dsl": {
                        "stage": "selection",
                        "provider": "test",
                        "model": "fake-selector",
                        "system_prompt": str(selector_sys),
                        "user_prompt": str(selector_user),
                    },
                    "selector_directive_dsl": {
                        "stage": "selection",
                        "provider": "test",
                        "model": "fake-selector",
                        "system_prompt": str(selector_directive_sys),
                        "user_prompt": str(selector_directive_user),
                    },
                    "render_chunk": {
                        "stage": "rendering",
                        "provider": "test",
                        "model": "fake-render",
                        "system_prompt": str(render_sys),
                        "user_prompt": str(render_user),
                    },
                    "render_fact_context_dsl": {
                        "stage": "rendering",
                        "provider": "test",
                        "model": "fake-context",
                        "temperature": 0,
                        "system_prompt": str(context_sys),
                        "user_prompt": str(context_user),
                    },
                    "name_table": {
                        "stage": "name_resolution",
                        "provider": "test",
                        "model": "fake-name-table",
                        "system_prompt": str(name_table_sys),
                        "user_prompt": str(name_table_user),
                    },
                    "node_edit": {
                        "stage": "node_edit",
                        "provider": "test",
                        "model": "fake-ne",
                        "system_prompt": str(ne_sys),
                        "user_prompt": str(ne_user),
                    },
                    "node_edit_block_repair": {
                        "stage": "node_edit",
                        "provider": "test",
                        "model": "fake-ne",
                        "system_prompt": str(ne_sys),
                        "user_prompt": str(ne_repair_user),
                    },
                },
                "stage_budgets": {
                    "rendering": {
                        "target_material_tokens": 1,
                        "max_material_tokens": 4500,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_text(json.dumps({"profiles": {}}), encoding="utf-8")

    project_dir = tmp_path / "projects" / "proj"
    project_dir.mkdir(parents=True)
    (project_dir / "sources.json").write_text(
        json.dumps(
            {
                "project_id": "proj",
                "output_language": "zh-CN",
                "name_resolution_mode": "name_table",
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    (hosts_dir / "host.json").write_text(
        json.dumps(HOST, ensure_ascii=False), encoding="utf-8"
    )

    runs_root = tmp_path / "runs"
    store = RunStore.for_run(runs_root, BASE_RUN)

    nodes = [
        CandidateNode(candidate_id="c1", source_window="w1", summary="主角登场",
                      material_ids=["m1"]),
        CandidateNode(candidate_id="c2", source_window="w1", summary="反派揭晓",
                      material_ids=["m2"]),
        CandidateNode(candidate_id="c3", source_window="w2", summary="支线花絮",
                      material_ids=["m3"]),
    ]
    graph = CandidateGraph(graph_id="g1", project_id="proj", nodes=nodes)
    store.write_model(CANDIDATE_GRAPH, graph)

    atoms = [
        _atom("m1", "主角在第一章的雨夜登场。"),
        _atom("m2", "反派身份在第三章被揭晓。"),
        _atom("m3", "设定集提到一段删除剧情。"),
    ]
    store.write_records(MATERIAL_ATOMS, atoms)

    decisions = [
        SelectionDecision(candidate_id="c1", status=SelectionStatus.accepted,
                          role=Role.core, treatment="paraphrase", directive="开场铺垫"),
        SelectionDecision(candidate_id="c2", status=SelectionStatus.accepted,
                          role=Role.bridge, treatment="summarize", directive="埋伏笔"),
        SelectionDecision(candidate_id="c3", status=SelectionStatus.not_selected),
    ]
    selection = SelectionPlan(
        plan_id="sel-1", project_id="proj", persona_id="host",
        selected_sequence=["c1", "c2"], decisions=decisions,
        metadata={"diagnostics": []},
    )
    store.write_model(SELECTION_PLAN, selection)

    node_edit = NodeEditPlan(
        plan_id="ne-1", project_id="proj", candidate_graph_id="g1",
        selection_plan_id="sel-1",
        node_edits=[
            NodeEditResult(
                candidate_id=cid,
                selection=decision,
                edits=[MaterialEdit(operation=NodeEditOperation.keep,
                                    source_material_ids=[mid])],
            )
            for cid, mid, decision in (("c1", "m1", decisions[0]), ("c2", "m2", decisions[1]))
        ],
    )
    store.write_model(NODE_EDIT_PLAN, node_edit)
    edited_materials = atoms[:2]
    store.write_records(EDITED_MATERIALS, edited_materials)

    policy = dataclasses.replace(DEFAULT_CHUNK_POLICY, target_material_tokens=1)
    packed = pack(
        selection,
        node_edit,
        edited_materials,
        policy,
        candidate_graph=graph,
    )
    assert len(packed.contracts) == 2  # one node per chunk under the tiny target
    store.write_model(CHUNK_PLAN, packed.chunk_plan)
    store.write_records(RENDER_CONTRACTS, packed.contracts)
    store.write_model(
        NAME_RESOLUTION_PLAN,
        NameResolutionPlan(
            plan_id="name-resolution-base",
            project_id="proj",
            output_language="zh-CN",
            input_hash="sha256:base",
            contract_ids=[contract.contract_id for contract in packed.contracts],
            rules=[],
        ),
    )

    config = RuntimeConfig.load(config_path, load_env=False)
    system_body, _ = load_system_prompt("render_chunk", config)
    template, _ = load_user_prompt("render_chunk", config)
    persona = coerce_persona(HOST_PROFILE)
    positions_by_chunk = derive_render_chunk_positions(packed.contracts)
    texts = ["第一段正文来了。有两句话。", "第二段接着讲。也有两句话。"]
    artifacts: list[ChunkArtifact] = []
    records: list[LlmCallRecord] = []
    for contract, text, call_id in zip(
        packed.contracts, texts, ("call-1", "call-2"), strict=True
    ):
        position = positions_by_chunk[contract.chunk_id]
        user = render_chunk_user_prompt(
            template=template,
            contract=contract,
            persona=persona,
            output_language="zh-CN",
            chunk_index=position.chunk_index,
            chunk_count=position.chunk_count,
            segment_position=position.segment_position,
        )
        store.write_raw_payload(call_id, "request", {
            "operation_route": "render_chunk",
            "model": "fake-render",
            "temperature": 0.7,
            "messages": [
                {"role": "system", "content": system_body},
                {"role": "user", "content": user},
            ],
        })
        store.write_raw_payload(call_id, "response", {"text": text})
        artifacts.append(ChunkArtifact(
            chunk_id=contract.chunk_id, order_index=contract.order_index,
            contract_id=contract.contract_id, text=text,
            input_hash=content_hash(user), output_hash=content_hash(text),
            generated_at=datetime.now(tz=UTC), metadata={
                "call_id": call_id,
                "chunk_index": position.chunk_index,
                "chunk_count": position.chunk_count,
                "segment_position": position.segment_position,
            },
        ))
        records.append(LlmCallRecord(
            call_id=call_id, stage="rendering", operation_route="render_chunk",
            provider="test", model="fake-render", input_hash=content_hash(user),
            provider_status=ProviderStatus.success, created_at=datetime.now(tz=UTC),
        ))
    store.write_records(CHUNK_ARTIFACTS, artifacts)
    store.write_records(LLM_CALL_RECORDS, records)

    return {
        "runs_root": runs_root,
        "projects_dir": tmp_path / "projects",
        "hosts_dir": hosts_dir,
        "host": HOST_PROFILE,
        "config_path": config_path,
        "profiles_path": profiles_path,
        "contracts": packed.contracts,
        "artifacts": artifacts,
        "texts": texts,
    }


def make_client(env: dict, call=None) -> TestClient:
    app = create_app(
        runs_root=env["runs_root"],
        projects_dir=env["projects_dir"],
        hosts_dir=env["hosts_dir"],
        config_path=env["config_path"],
        profiles_path=env["profiles_path"],
        call=call or FakeCall({}),
    )
    return TestClient(app)


def configure_ingestion_project(env: dict) -> None:
    """Add two declared raw sources and the atom route to ``build_env``."""
    prompt_dir = env["config_path"].parent / "prompt"
    atom_system = _prompt_file(
        prompt_dir / "atom.system.md", "test_atom_system@v1", "只输出 atom DSL。"
    )
    atom_user = _prompt_file(
        prompt_dir / "atom.user.md",
        "test_atom_user@v1",
        """范围 {{alias_range}}
种类 {{allowed_content_kinds}}
说明 {{content_kind_descriptions}}
标签 {{allowed_tags}}
标签说明 {{tag_descriptions}}
片段 {{piece_lines}}""",
    )
    atom_repair = _prompt_file(
        prompt_dir / "atom.repair.md",
        "test_atom_repair@v1",
        "失败 {{violations}}；范围 {{alias_range}}",
    )
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    config["routes"]["atom_boundary_dsl"] = {
        "stage": "ingestion",
        "provider": "test",
        "model": "fake-atom",
        "system_prompt": str(atom_system),
        "user_prompt": str(atom_user),
        "repair_prompt": str(atom_repair),
        "max_concurrency": 1,
    }
    config["stage_budgets"]["ingestion"] = {
        "source_piece_max_tokens": 120,
        "atom_window_max_input_tokens": 1000,
        "pieces_per_call_soft_limit": 8,
    }
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")

    env["profiles_path"].write_text(
        json.dumps(
            {
                "profiles": {
                    "script": {
                        "default_source_class": "script_transcript",
                        "default_trust_tier": "canonical",
                        "allowed_content_kinds": ["narration"],
                        "allowed_tags": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    project_dir = env["projects_dir"] / "proj"
    raw_dir = project_dir / "raw"
    raw_dir.mkdir()
    (raw_dir / "one.txt").write_text("第一来源只有一段。", encoding="utf-8")
    (raw_dir / "two.txt").write_text("第二来源也只有一段。", encoding="utf-8")
    (project_dir / "sources.json").write_text(
        json.dumps(
            {
                "project_id": "proj",
                "output_language": "zh-CN",
                "sources": [
                    {
                        "source_id": "s1",
                        "path": "raw/one.txt",
                        "profile": "script",
                        "language": "zh",
                    },
                    {
                        "source_id": "s2",
                        "path": "raw/two.txt",
                        "profile": "script",
                        "language": "zh",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def configure_host_builder(env: dict) -> None:
    """Add the two real Host Builder routes with tiny local prompt templates."""
    prompt_dir = env["config_path"].parent / "prompt"
    profile_system = _prompt_file(
        prompt_dir / "host-profile.system.md",
        "test_host_profile_system@v1",
        "提炼 Host profile。",
    )
    profile_user = _prompt_file(
        prompt_dir / "host-profile.user.md",
        "test_host_profile_user@v1",
        "{{samples}}",
    )
    examples_system = _prompt_file(
        prompt_dir / "host-examples.system.md",
        "test_host_examples_system@v1",
        "反推原始材料和学习点。",
    )
    examples_user = _prompt_file(
        prompt_dir / "host-examples.user.md",
        "test_host_examples_user@v1",
        "{{examples}}",
    )
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    config["routes"].update(
        {
            "host_profile_distill": {
                "stage": "host_builder",
                "provider": "test",
                "model": "fake-host-profile",
                "system_prompt": str(profile_system),
                "user_prompt": str(profile_user),
            },
            "host_style_example_reverse": {
                "stage": "host_builder",
                "provider": "test",
                "model": "fake-host-examples",
                "system_prompt": str(examples_system),
                "user_prompt": str(examples_user),
            },
        }
    )
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")


def host_builder_files() -> list[dict[str, str]]:
    return [
        {
            "name": f"偏好稿-{index}.txt",
            "content": (
                f"{char * 5}，开场有具体动作。"
                f"{char * 7}，中段出现转折！"
                f"{char * 9}，结尾留下判断？"
            ),
        }
        for index, char in enumerate("甲乙丙丁", start=1)
    ]


def host_profile_response() -> str:
    return "\n".join(
        (
            "OUTPUT_LANGUAGE zh-CN",
            "VIEWPOINT 关注人物的选择、行动与后果。",
            "PREFER 具体行动与可验证转折",
            "AVOID 空泛总结",
            "THINKING_RULE 区分材料事实与主持判断。",
            "EXPRESSION 自然、克制、清楚的中文播客口语。",
            "EXPRESSION_RULE 长短句交替，判断紧随证据。",
        )
    )


def host_examples_response() -> str:
    rows: list[str] = []
    for index in range(1, 7):
        alias = f"E{index:03d}"
        rows.extend(
            (
                f"EXAMPLE {alias}",
                "SOURCE",
                f"{alias} 的事实骨架。",
                "ENDSOURCE",
                "ENDEXAMPLE",
            )
        )
    return "\n".join(rows)


def wait_job(client: TestClient, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("pending", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def dir_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- read-only endpoints -------------------------------------------------------


def test_projects_runs_and_inventory(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)

    projects = client.get("/api/projects").json()
    assert projects == [
        {"project_id": "proj", "dir": "proj", "output_language": "zh-CN"}
    ]
    assert client.get("/api/projects/proj/persona").status_code == 404
    hosts = client.get("/api/hosts").json()
    assert [row["host_id"] for row in hosts] == [
        "starter-zh-cn", "starter-en-us", "host",
    ]
    assert all(row["read_only"] for row in hosts[:2])
    assert all(row["catalog_origin"] == "builtin" for row in hosts[:2])
    personal = hosts[2]
    assert personal["display_name"] == "测试 Host"
    assert personal["output_language"] == "zh-CN"
    assert personal["schema_version"] == "tracecast-host/1"
    assert personal["content_hash"] == HOST_PROFILE.snapshot()["content_hash"]
    assert personal["catalog_origin"] == "personal"
    assert personal["read_only"] is False
    detail = client.get("/api/hosts/host").json()
    assert detail == {
        **HOST_PROFILE.snapshot(), "catalog_origin": "personal", "read_only": False,
    }
    builtin = client.get("/api/hosts/starter-zh-cn").json()
    assert builtin["profile"]["host_id"] == "starter-zh-cn"
    assert builtin["catalog_origin"] == "builtin"
    assert builtin["read_only"] is True
    assert client.get("/api/hosts/unknown").status_code == 404

    runs = client.get("/api/runs").json()
    assert [r["run_id"] for r in runs] == [BASE_RUN]
    assert CANDIDATE_GRAPH.relpath in runs[0]["present"]
    assert runs[0]["console_manifest"] is None

    detail = client.get(f"/api/runs/{BASE_RUN}").json()
    slots = {s["relpath"]: s for s in detail["slots"]}
    assert slots[CHUNK_ARTIFACTS.relpath]["rows"] == 2
    assert slots["assembly/script.md"]["exists"] is False

    assert client.get("/api/runs/nope").status_code == 404


def test_run_list_manifest_summary_exposes_readable_identity_fields(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    manifest_path = store.run_dir / "console" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "kind": "full",
                "base_run_id": None,
                "status": "complete",
                "created_at": "2026-08-03T10:20:30+00:00",
                "request": {"project_id": "proj", "run_label": "方案 A"},
                "effective": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = make_client(env).get("/api/runs").json()[0]["console_manifest"]

    assert summary == {
        "kind": "full",
        "base_run_id": None,
        "status": "complete",
        "created_at": "2026-08-03T10:20:30+00:00",
        "run_label": "方案 A",
        "project_id": "proj",
    }


def test_artifact_read_and_traversal_guard(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)

    graph = client.get(
        f"/api/runs/{BASE_RUN}/artifact", params={"relpath": CANDIDATE_GRAPH.relpath}
    ).json()
    assert graph["data"]["graph_id"] == "g1"

    rows = client.get(
        f"/api/runs/{BASE_RUN}/artifact",
        params={"relpath": MATERIAL_ATOMS.relpath, "limit": 1},
    ).json()
    assert rows["total_rows"] == 3 and len(rows["rows"]) == 1

    for bad in ("../../secrets", "llm/raw/call-1.request.json", "planning/other.json"):
        res = client.get(f"/api/runs/{BASE_RUN}/artifact", params={"relpath": bad})
        assert res.status_code == 404, bad


def test_ingestion_audit_is_readable_and_non_persisted(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    texts = ["第一段材料。", "继续说明。", "独立变化。", "最后收束。"]
    pieces = [
        SourcePiece(
            piece_id=f"s1:{index:06d}",
            source_id="s1",
            order_index=index,
            text=text,
            span_start=sum(len(item) for item in texts[:index]),
            span_end=sum(len(item) for item in texts[: index + 1]),
            char_count=len(text),
        )
        for index, text in enumerate(texts)
    ]
    window = SourceWindow(
        window_id="w1",
        source_id="s1",
        piece_ids=[piece.piece_id for piece in pieces],
        order_index=0,
        estimated_input_tokens=32,
        metadata={"max_input_tokens": 1000},
    )
    atoms = [
        _atom(
            "a1",
            texts[0] + texts[1],
            source_order=0,
            piece_refs=[pieces[0].piece_id, pieces[1].piece_id],
        ),
        _atom("a2", texts[2], source_order=1, piece_refs=[pieces[2].piece_id]),
        _atom("a3", texts[3], source_order=2, piece_refs=[pieces[3].piece_id]),
    ]
    store.write_records(SOURCE_PIECES, pieces)
    store.write_records(SOURCE_WINDOWS, [window])
    store.write_records(MATERIAL_ATOMS, atoms)
    before = dir_digest(store.run_dir)

    response = make_client(env).get(
        f"/api/runs/{BASE_RUN}/ingestion/audit", params={"sample_size": 3}
    )

    assert response.status_code == 200
    audit = response.json()
    assert audit["summary"]["pieces"] == 4
    assert audit["summary"]["atoms"] == 3
    assert audit["summary"]["aggregation_factor"] == 1.333
    assert audit["sources"][0]["single_piece_atoms"] == 2
    assert [sample["content"] for sample in audit["samples"]] == [
        texts[0] + texts[1],
        texts[2],
        texts[3],
    ]
    stage_report = make_client(env).get(
        f"/api/runs/{BASE_RUN}/reports/ingestion"
    ).json()
    assert stage_report["available"] is True
    assert stage_report["summary"]["tiling_coverage"] == 1.0
    assert stage_report["origins"][MATERIAL_ATOMS.relpath] == BASE_RUN

    derived = RunStore.for_run(env["runs_root"], "proj-sparse-audit")
    derived.run_dir.mkdir(parents=True)
    (derived.run_dir / "console").mkdir()
    (derived.run_dir / "console" / "manifest.json").write_text(
        json.dumps({"kind": "selection", "base_run_id": BASE_RUN}),
        encoding="utf-8",
    )
    inherited = make_client(env).get(
        f"/api/runs/{derived.run_id}/ingestion/audit",
        params={"sample_size": 2},
    )
    assert inherited.status_code == 200
    assert inherited.json()["origins"] == {
        SOURCE_PIECES.relpath: BASE_RUN,
        SOURCE_WINDOWS.relpath: BASE_RUN,
        MATERIAL_ATOMS.relpath: BASE_RUN,
    }
    assert dir_digest(store.run_dir) == before


def test_llm_calls_and_raw_payloads(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)

    calls = client.get(f"/api/runs/{BASE_RUN}/llm/calls").json()
    assert [c["call_id"] for c in calls] == ["call-1", "call-2"]

    raw = client.get(f"/api/runs/{BASE_RUN}/llm/calls/call-1/raw").json()
    roles = [m["role"] for m in raw["data"]["messages"]]
    assert roles == ["system", "user"]
    assert client.get(f"/api/runs/{BASE_RUN}/llm/calls/ghost/raw").status_code == 404


def test_config_endpoint_hides_provider_key_details(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)
    config = client.get("/api/config").json()
    assert config["providers"]["test"] == {
        "base_url": "http://localhost:1",
        "chat_completions_path": "/chat/completions",
        "max_output_tokens_field": "max_tokens",
        "credentials_configured": True,
    }
    assert "api_key_env" not in json.dumps(config)
    editor = config["editor"]
    fields = {field["id"]: field for field in editor["budget_fields"]}
    assert "stage_budgets.rendering.max_nodes_per_chunk" in fields
    assert "derive_render" in fields["stage_budgets.rendering.max_material_tokens"]["surfaces"]
    assert fields["stage_budgets.selection.target_output_kchars"]["surfaces"] == [
        "derive_selection"
    ]
    render_route = next(route for route in editor["routes"] if route["name"] == "render_chunk")
    routes_by_name = {route["name"]: route for route in editor["routes"]}
    assert routes_by_name["node_edit_block_repair"]["surfaces"] == [
        "full_root",
        "derive_node_edit",
    ]
    assert "render_fact_context_repair" not in routes_by_name
    assert render_route["readonly"] == {
        "provider": "test",
        "cost_profile": None,
    }
    chunk_lab_fields = {
        field["id"]
        for field in editor["budget_fields"]
        if "chunk_lab" in field["surfaces"]
    }
    assert chunk_lab_fields == {"stage_budgets.rendering.render_max_input_tokens"}
    source_probe = next(
        (
            route
            for route in editor["routes"]
            if route["name"] == "source_profile_probe"
        ),
        None,
    )
    assert source_probe is None or source_probe["surfaces"] == []
    render_field_keys = {field["key"] for field in render_route["fields"]}
    assert render_field_keys >= {
        "model",
        "extra_body",
        "temperature",
        "max_output_tokens",
        "timeout_seconds",
        "max_concurrency",
        "transport_max_attempts",
        "transport_backoff_base_seconds",
        "transport_backoff_max_seconds",
        "transport_jitter_ratio",
        "system_prompt",
        "user_prompt",
    }
    assert "thinking" not in render_field_keys
    fact_context_route = routes_by_name["render_fact_context_dsl"]
    fact_context_temperature = next(
        field for field in fact_context_route["fields"] if field["key"] == "temperature"
    )
    assert fact_context_temperature == {
        "key": "temperature",
        "type": "float",
        "min": 0,
        "max": 0,
        "help": "Fact Context 稀疏 DSL 固定使用 temperature=0。",
        "value": 0.0,
        "readonly": True,
    }


def test_host_builder_surface_isolated_and_unknown_routes_are_global_only(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    configure_host_builder(env)
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    config["routes"]["future_route"] = {
        "stage": "future",
        "provider": "test",
        "model": "future-model",
        "system_prompt": config["routes"]["host_profile_distill"]["system_prompt"],
        "user_prompt": config["routes"]["host_profile_distill"]["user_prompt"],
    }
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")

    editor = make_client(env).get("/api/config").json()["editor"]
    routes = {row["name"]: row for row in editor["routes"]}
    assert routes["host_profile_distill"]["surfaces"] == ["host_build"]
    assert routes["host_style_example_reverse"]["surfaces"] == ["host_build"]
    assert "full_root" not in routes["host_profile_distill"]["surfaces"]
    assert routes["future_route"]["surfaces"] == []
    assert "host_build" in editor["surfaces"]


def test_host_builder_api_dry_run_then_two_call_install(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_host_builder(env)
    fake = FakeCall(
        {
            "host_profile_distill": host_profile_response(),
            "host_style_example_reverse": host_examples_response(),
        }
    )
    client = make_client(env, call=fake)
    files = host_builder_files()
    payload = {
        "files": files,
        "host_id": "personal-host",
        "display_name": "个人 Host",
        "piece_max_tokens": 7,
        "config_overrides": {
            "routes": {
                "host_profile_distill": {"model": "override-profile"},
                "host_style_example_reverse": {"model": "override-examples"},
            }
        },
    }

    before_runs = dir_digest(env["runs_root"])
    dry = client.post("/api/hosts/build", json=payload)
    assert dry.status_code == 200, dry.text
    estimate = dry.json()["estimate"]
    assert estimate["calls"] == 2
    assert estimate["host_id"] == "personal-host"
    assert estimate["display_name"] == "个人 Host"
    assert estimate["version"] == "personal-host@v1"
    assert estimate["piece_max_tokens"] == 7
    assert estimate["models"] == {
        "host_profile_distill": "override-profile",
        "host_style_example_reverse": "override-examples",
    }
    reserved = client.post(
        "/api/hosts/build", json={**payload, "host_id": "starter-zh-cn"}
    )
    assert reserved.status_code == 409
    assert "built-in host id is reserved" in reserved.text
    assert [row["position"] for row in estimate["selected_examples"]] == [
        "opening", "opening", "middle", "middle", "ending", "ending",
    ]
    assert fake.calls == []
    assert dir_digest(env["runs_root"]) == before_runs
    assert not (env["hosts_dir"] / "personal-host.json").exists()

    repeated = client.post("/api/hosts/build", json=payload).json()["estimate"]
    assert repeated["seed"] == estimate["seed"]
    assert repeated["selected_examples"] == estimate["selected_examples"]

    confirmed = client.post(
        "/api/hosts/build", json={**payload, "confirm": True}
    )
    assert confirmed.status_code == 200, confirmed.text
    job = wait_job(client, confirmed.json()["job_id"])
    assert job["state"] == "succeeded", job
    assert job["result"]["host_id"] == "personal-host"
    assert job["result"]["calls"] == 2
    assert job["result"]["sampled_examples"] == 6
    assert len(fake.calls) == 2
    assert [call["config"].route(call["operation_route"]).model for call in fake.calls] == [
        "override-profile", "override-examples",
    ]

    hosts = client.get("/api/hosts").json()
    built = next(row for row in hosts if row["host_id"] == "personal-host")
    assert built["schema_version"] == "tracecast-host/2"
    assert built["output_language"] == "zh-CN"
    run_ids = {row["run_id"] for row in client.get("/api/runs").json()}
    assert confirmed.json()["run_id"] not in run_ids
    full_references = {row["content"] for row in files}
    assert not any(text in json.dumps(job, ensure_ascii=False) for text in full_references)
    assert not any(text in json.dumps(hosts, ensure_ascii=False) for text in full_references)


def test_trace_uses_work_unit_and_returns_readable_output(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    records = store.read_records(LLM_CALL_RECORDS)
    raw = store.read_raw_payload("call-1", "request")
    assert isinstance(raw, dict)
    records[0] = records[0].model_copy(
        update={
            "input_hash": f"sha256:{sha256_json(raw)}",
            "work_unit": LlmWorkUnitRef(
                kind="render_chunk",
                unit_id=env["contracts"][0].chunk_id,
                order_index=0,
                input_refs=["c1", "m1"],
            ),
        }
    )
    records[1] = records[1].model_copy(
        update={
            "work_unit": LlmWorkUnitRef(
                kind="render_chunk",
                unit_id=env["contracts"][1].chunk_id,
                order_index=1,
                input_refs=["c2", "m2"],
            )
        }
    )
    store.write_records(LLM_CALL_RECORDS, records)
    client = make_client(env)
    chunk_id = env["contracts"][0].chunk_id

    index = client.get(
        f"/api/runs/{BASE_RUN}/trace", params={"chunk_id": chunk_id}
    ).json()
    assert index["exact_trace_available"] is True
    by_stage = {stage["stage"]: stage for stage in index["stages"]}
    assert by_stage["rendering"]["units"][0]["unit_id"] == chunk_id
    assert by_stage["packing"]["units"][0]["origin_run_id"] == BASE_RUN

    detail = client.get(
        f"/api/runs/{BASE_RUN}/trace/rendering/{chunk_id}"
    ).json()
    assert "annotated_materials_block" in detail["input_blocks"]
    assert detail["compiled_output"]["text"] == env["texts"][0]
    assert detail["technical"]["matches_recorded_request"] is True
    readable = json.dumps(detail["compiled_output"], ensure_ascii=False)
    assert "metadata" not in readable and "call_id" not in readable


def test_chunk_trace_includes_all_selection_work_units(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    records = store.read_records(LLM_CALL_RECORDS)
    selection_units = {
        "g1": "selection_graph",
        "g1:directives": "selection_directives",
    }
    for index, (unit_id, kind) in enumerate(selection_units.items(), start=1):
        records.append(
            LlmCallRecord(
                call_id=f"call-selection-{index}",
                stage="selection",
                operation_route="selector_path_dsl",
                provider="test",
                model="fake-selector",
                input_hash=f"sha256:selection-{index}",
                provider_status=ProviderStatus.success,
                created_at=datetime.now(tz=UTC),
                work_unit=LlmWorkUnitRef(
                    kind=kind,
                    unit_id=unit_id,
                    input_refs=["c1", "c2"],
                ),
            )
        )
    store.write_records(LLM_CALL_RECORDS, records)
    client = make_client(env)

    index = client.get(
        f"/api/runs/{BASE_RUN}/trace",
        params={"chunk_id": env["contracts"][0].chunk_id},
    ).json()
    selection = next(
        stage for stage in index["stages"] if stage["stage"] == "selection"
    )

    assert {
        unit["unit_id"]: unit["kind"] for unit in selection["units"]
    } == selection_units


def test_sparse_fact_context_trace_is_one_run_wide_work_unit(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    first, second = env["contracts"]
    context_call_id = "call-context"
    first_node_id = first.nodes[0].source_node_id
    second_node_id = second.nodes[0].source_node_id
    store.write_records(
        RENDER_FACT_CONTEXTS,
        [
            RenderFactContext(
                chunk_id=first.chunk_id,
                order_index=first.order_index,
                metadata={"call_id": context_call_id},
            ),
            RenderFactContext(
                chunk_id=second.chunk_id,
                order_index=second.order_index,
                node_hints=[
                    RenderFactContextNodeHint(
                        source_node_id=second_node_id,
                        established="主角登场已经在更早计划安排。",
                    ),
                    RenderFactContextNodeHint(
                        source_node_id=second_node_id,
                        established="把登场包装为首次悬念的动作也已安排。",
                    ),
                ],
                metadata={"call_id": context_call_id},
            ),
        ],
    )
    outline = (
        "ORDERED_CHUNK_PLAN\n"
        "CHUNK C001\n"
        "NODE N001\n"
        "CONTENT_SCHEDULED_HERE 主角登场\n"
        "ACTION_SCHEDULED_HERE 建立起点\n"
        "ENDNODE\n"
        "ENDCHUNK\n"
        "CHUNK C002\n"
        "NODE N001\n"
        "CONTENT_SCHEDULED_HERE 主角再次登场\n"
        "ACTION_SCHEDULED_HERE none\n"
        "ENDNODE\n"
        "ENDCHUNK\n"
        "END_ORDERED_CHUNK_PLAN"
    )
    config = RuntimeConfig.load(env["config_path"], load_env=False)
    context_system, _ = load_system_prompt("render_fact_context_dsl", config)
    request = {
        "messages": [
            {"role": "system", "content": context_system},
            {"role": "user", "content": f"zh-CN\n{outline}"},
        ]
    }
    store.write_raw_payload(context_call_id, "request", request)
    records = store.read_records(LLM_CALL_RECORDS)
    records.append(
        LlmCallRecord(
            call_id=context_call_id,
            stage="rendering",
            operation_route="render_fact_context_dsl",
            provider="test",
            model="fake-context",
            input_hash=f"sha256:{sha256_json(request)}",
            provider_status=ProviderStatus.success,
            created_at=datetime.now(tz=UTC),
            work_unit=LlmWorkUnitRef(
                kind="render_fact_context",
                unit_id="fact-context",
                input_refs=[first_node_id, second_node_id],
            ),
        )
    )
    store.write_records(LLM_CALL_RECORDS, records)
    store.write_records(
        AUDIT_REPORT,
        [
            AuditReportItem(
                issue_id="issue-context-parse",
                stage="rendering",
                kind="fact_context_parse_error",
                level="warning",
                message="one malformed line was dropped",
                usable=True,
                artifact_ref=RENDER_FACT_CONTEXTS.relpath,
                trace_refs=[second_node_id],
                call_id=context_call_id,
            )
        ],
    )
    client = make_client(env)

    first_trace = client.get(
        f"/api/runs/{BASE_RUN}/trace",
        params={"chunk_id": first.chunk_id},
    ).json()
    first_render_units = next(
        stage["units"]
        for stage in first_trace["stages"]
        if stage["stage"] == "rendering"
    )
    assert "fact-context" in {unit["unit_id"] for unit in first_render_units}

    second_trace = client.get(
        f"/api/runs/{BASE_RUN}/trace",
        params={"chunk_id": second.chunk_id},
    ).json()
    second_render_units = next(
        stage["units"]
        for stage in second_trace["stages"]
        if stage["stage"] == "rendering"
    )
    context_unit = next(
        unit for unit in second_render_units
        if unit["unit_id"] == "fact-context"
    )
    assert context_unit["kind"] == "render_fact_context"

    detail = client.get(
        f"/api/runs/{BASE_RUN}/trace/rendering/fact-context"
    ).json()
    assert detail["input_blocks"] == {
        "output_language": "zh-CN",
        "node_outline": outline,
    }
    assert detail["technical"]["input_refs"] == [first_node_id, second_node_id]
    assert detail["technical"]["matches_recorded_request"] is True
    assert detail["compiled_output"] == [
        {
            "order_index": first.order_index,
            "node_hints": [],
            "semantics": "sparse_node_established_v3",
        },
        {
            "order_index": second.order_index,
            "node_hints": [
                    {
                        "source_node_id": second_node_id,
                        "established": "主角登场已经在更早计划安排。",
                    },
                    {
                        "source_node_id": second_node_id,
                        "established": "把登场包装为首次悬念的动作也已安排。",
                    },
            ],
            "semantics": "sparse_node_established_v3",
        },
    ]
    assert [item["call_id"] for item in detail["diagnostics"]] == [
        context_call_id
    ]


def test_trace_rebuilds_blocks_with_origin_run_config_override(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    contract = env["contracts"][0]
    override_path = tmp_path / "prompt" / "render.override.md"
    override_body = "覆盖模板\n" + RENDER_USER_TEMPLATE.split("\n", 1)[1]
    _prompt_file(
        override_path,
        "test_render_override@v1",
        override_body,
    )
    persona = coerce_persona(HOST_PROFILE)
    user_prompt = render_chunk_user_prompt(
        template=override_body,
        contract=contract,
        persona=persona,
        output_language="zh-CN",
    )
    request = {
        "messages": [
            {"role": "system", "content": "渲染系统层。"},
            {"role": "user", "content": user_prompt},
        ]
    }
    derived = RunStore.for_run(env["runs_root"], "proj-config-trace")
    derived.run_dir.mkdir(parents=True)
    (derived.run_dir / "console").mkdir()
    (derived.run_dir / "console" / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "render",
                "base_run_id": BASE_RUN,
                "request": {
                    "config_overrides": {
                        "routes": {
                            "render_chunk": {
                                "user_prompt": str(override_path),
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    derived.write_raw_payload("call-override", "request", request)
    derived.write_records(
        LLM_CALL_RECORDS,
        [
            LlmCallRecord(
                call_id="call-override",
                stage="rendering",
                operation_route="render_chunk",
                provider="test",
                model="fake-render",
                input_hash=f"sha256:{sha256_json(request)}",
                provider_status=ProviderStatus.success,
                created_at=datetime.now(tz=UTC),
                work_unit=LlmWorkUnitRef(
                    kind="render_chunk",
                    unit_id=contract.chunk_id,
                    order_index=contract.order_index,
                    input_refs=["c1", "m1"],
                ),
            )
        ],
    )

    detail = make_client(env).get(
        f"/api/runs/{derived.run_id}/trace/rendering/{contract.chunk_id}"
    )
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["input_blocks"]["annotated_materials_block"]
    assert payload["technical"]["prompt_template_drift"] is False
    assert payload["technical"]["matches_recorded_request"] is True


def test_planning_trace_uses_unit_lineage_snapshot_not_final_node(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    graph = store.read_model(CANDIDATE_GRAPH)
    final_node = graph.nodes[0]
    metadata = dict(final_node.metadata)
    metadata.update(
        {
            "producer_unit_id": "compress:w1:1",
            "compression_lineage_units": ["seed:w1"],
            "producer_lineage": [
                {
                    "unit_id": "seed:w1",
                    "call_id": "call-seed",
                    "summary": "早期 seed 摘要",
                    "narrative_function": "setup",
                    "material_count": 1,
                },
                {
                    "unit_id": "compress:w1:1",
                    "call_id": "call-compress",
                    "summary": final_node.summary,
                    "material_count": 1,
                },
            ],
        }
    )
    nodes = [final_node.model_copy(update={"metadata": metadata}), *graph.nodes[1:]]
    store.write_model(CANDIDATE_GRAPH, graph.model_copy(update={"nodes": nodes}))
    request = {"messages": [{"role": "user", "content": "seed prompt"}]}
    store.write_raw_payload("call-seed", "request", request)
    records = store.read_records(LLM_CALL_RECORDS)
    records.append(
        LlmCallRecord(
            call_id="call-seed",
            stage="planner",
            operation_route="planner_graph_dsl",
            provider="test",
            model="fake",
            input_hash=f"sha256:{sha256_json(request)}",
            provider_status=ProviderStatus.success,
            created_at=datetime.now(tz=UTC),
            work_unit=LlmWorkUnitRef(
                kind="planning_seed",
                unit_id="seed:w1",
                input_refs=["m1"],
            ),
        )
    )
    store.write_records(LLM_CALL_RECORDS, records)
    chunk_id = env["contracts"][0].chunk_id
    client = make_client(env)

    index = client.get(
        f"/api/runs/{BASE_RUN}/trace", params={"chunk_id": chunk_id}
    ).json()
    planning = next(stage for stage in index["stages"] if stage["stage"] == "planning")
    assert {unit["unit_id"] for unit in planning["units"]} == {"seed:w1"}

    detail = client.get(
        f"/api/runs/{BASE_RUN}/trace/planning/seed:w1"
    ).json()
    assert detail["compiled_output"] == [
        {
            "summary": "早期 seed 摘要",
            "narrative_function": "setup",
            "material_count": 1,
        }
    ]
    readable = json.dumps(detail["compiled_output"], ensure_ascii=False)
    assert final_node.summary not in readable
    assert "seed:w1" not in readable and "call-seed" not in readable


def test_chunk_trace_includes_run_level_name_table(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    first_contract = env["contracts"][0]
    store.write_model(
        NAME_RESOLUTION_PLAN,
        NameResolutionPlan(
            plan_id="name-resolution-trace",
            project_id="proj",
            output_language="zh-CN",
            input_hash="sha256:trace",
            contract_ids=[contract.contract_id for contract in env["contracts"]],
            rules=[
                NameResolutionRule(
                    rule_id="rule-wang",
                    aliases=["小王", "王先生"],
                    stable_name="王先生",
                    evidence_chunk_ids=[first_contract.chunk_id],
                    metadata={
                        "source": "model",
                        "model_call_ids": ["call-name-table"],
                    },
                )
            ],
        ),
    )
    records = store.read_records(LLM_CALL_RECORDS)
    for call_id, route, work_unit in (
        (
            "call-name-table",
            "name_table",
            LlmWorkUnitRef(
                kind="name_table",
                unit_id="name-table",
                input_refs=["m1", "m2"],
            ),
        ),
    ):
        request = {"messages": [{"role": "user", "content": route}]}
        store.write_raw_payload(call_id, "request", request)
        records.append(
            LlmCallRecord(
                call_id=call_id,
                stage="name_resolution",
                operation_route=route,
                provider="test",
                model="fake",
                input_hash=f"sha256:{sha256_json(request)}",
                provider_status=ProviderStatus.success,
                created_at=datetime.now(tz=UTC),
                work_unit=work_unit,
            )
        )
    store.write_records(LLM_CALL_RECORDS, records)
    chunk_id = first_contract.chunk_id
    client = make_client(env)

    index = client.get(
        f"/api/runs/{BASE_RUN}/trace", params={"chunk_id": chunk_id}
    ).json()
    name_stage = next(
        stage for stage in index["stages"] if stage["stage"] == "name_resolution"
    )
    assert {unit["unit_id"] for unit in name_stage["units"]} == {"name-table"}

    detail = client.get(
        f"/api/runs/{BASE_RUN}/trace/name_resolution/name-table"
    ).json()
    assert detail["compiled_output"] == [
        {
            "rule_id": "rule-wang",
                "stable_name": "王先生",
                "aliases": ["小王", "王先生"],
                "evidence_chunk_ids": [first_contract.chunk_id],
                "source": "model",
        }
    ]


@pytest.mark.parametrize(
    "stage",
    [
        "planning",
        "selection",
        "node_edit",
        "packing",
        "name_resolution",
        "rendering",
    ],
)
def test_stage_reports_are_derived_without_writing(
    tmp_path: Path, stage: str
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    before = dir_digest(store.run_dir)
    response = make_client(env).get(f"/api/runs/{BASE_RUN}/reports/{stage}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"]
    assert payload["origins"]
    assert dir_digest(store.run_dir) == before


def test_stage_reports_expose_curated_volume_and_outline_fields(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    atoms = store.read_records(MATERIAL_ATOMS)
    graph = store.read_model(CANDIDATE_GRAPH)
    selection_plan = store.read_model(SELECTION_PLAN)

    planning = client.get(f"/api/runs/{BASE_RUN}/reports/planning").json()["summary"]
    expected_material_tokens = sum(estimate_tokens(atom.content) for atom in atoms)
    expected_summary_tokens = sum(estimate_tokens(node.summary) for node in graph.nodes)
    assert planning["material_tokens"] == expected_material_tokens
    assert planning["summary_tokens"] == expected_summary_tokens
    assert planning["summary_retention_ratio"] == round(
        expected_summary_tokens / expected_material_tokens, 3
    )

    selection = client.get(f"/api/runs/{BASE_RUN}/reports/selection").json()
    assert len(selection["samples"]) == len(selection_plan.selected_sequence)
    assert selection["samples"][0]["directive"] == "开场铺垫"

    node_edit = client.get(f"/api/runs/{BASE_RUN}/reports/node_edit").json()["summary"]
    assert node_edit["source_token_total"] == node_edit["output_token_total"]
    assert node_edit["token_retention"] == 1.0

    rendering = client.get(f"/api/runs/{BASE_RUN}/reports/rendering").json()["summary"]
    assert rendering["body_tokens"] == sum(estimate_tokens(text) for text in env["texts"])


def test_name_resolution_failure_report_remains_visible_without_a_plan(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    store.path_for(NAME_RESOLUTION_PLAN.relpath).unlink()
    store.write_records(
        NAME_RESOLUTION_REPORT,
        [
            AuditReportItem(
                issue_id="issue-name-failure",
                stage="name_resolution",
                kind="name_table_invalid_output",
                level="error",
                message="name table output was malformed",
                usable=True,
                artifact_ref="chunk_1",
            )
        ],
    )

    response = make_client(env).get(
        f"/api/runs/{BASE_RUN}/reports/name_resolution"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["summary"] == {"status": "failed", "diagnostic_count": 1}
    assert payload["diagnostics"][0]["kind"] == "name_table_invalid_output"


def test_stage_reports_expose_node_repair_and_sparse_fact_hint_counts(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    plan = store.read_model(NODE_EDIT_PLAN)
    recovered = plan.node_edits[0].model_copy(
        update={"metadata": {"producer_unit_id": "repair:c1"}}
    )
    exhausted = plan.node_edits[1].model_copy(
        update={
            "metadata": {
                "producer_unit_id": "repair:c2",
                "fallback_reason": "repair failed",
            }
        }
    )
    store.write_model(
        NODE_EDIT_PLAN,
        plan.model_copy(update={"node_edits": [recovered, exhausted]}),
    )
    first, second = env["contracts"]
    store.write_records(
        RENDER_FACT_CONTEXTS,
        [
            RenderFactContext(
                chunk_id=first.chunk_id,
                order_index=first.order_index,
                metadata={"call_id": "call-context"},
            ),
            RenderFactContext(
                chunk_id=second.chunk_id,
                order_index=second.order_index,
                node_hints=[
                    RenderFactContextNodeHint(
                        source_node_id=second.nodes[0].source_node_id,
                        established="主角登场已在更早计划安排。",
                    ),
                    RenderFactContextNodeHint(
                        source_node_id=second.nodes[0].source_node_id,
                        established="首次悬念动作也已在更早计划安排。",
                    ),
                ],
                metadata={"call_id": "call-context"},
            ),
        ],
    )
    store.write_records(
        AUDIT_REPORT,
        [
            AuditReportItem(
                issue_id="issue-context-length",
                stage="rendering",
                kind="fact_context_length_warning",
                level="warning",
                message="context exceeds its diagnostic target",
                usable=True,
                artifact_ref=RENDER_FACT_CONTEXTS.relpath,
            )
        ],
    )
    client = make_client(env)

    node_summary = client.get(
        f"/api/runs/{BASE_RUN}/reports/node_edit"
    ).json()["summary"]
    assert node_summary["fallback_nodes"] == 1
    assert node_summary["repair_nodes"] == 2
    assert node_summary["repair_recovered"] == 1
    assert node_summary["repair_exhausted"] == 1

    render_summary = client.get(
        f"/api/runs/{BASE_RUN}/reports/rendering"
    ).json()["summary"]
    assert render_summary["fact_context_cards"] == 2
    assert render_summary["fact_context_used"] == 1
    assert render_summary["fact_context_hints"] == 2
    assert "fact_context_repaired" not in render_summary
    assert "fact_context_repair_exhausted" not in render_summary
    assert render_summary["fact_context_length_warnings"] == 1


def test_validation_and_assembly_reports_are_derived_without_writing(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    store = RunStore.for_run(env["runs_root"], BASE_RUN)
    issue = AuditReportItem(
        issue_id="issue-1",
        stage="rendering",
        kind="tail_mismatch",
        level="warning",
        message="tail differs",
        usable=True,
        artifact_ref=env["contracts"][0].chunk_id,
    )
    store.write_records(AUDIT_REPORT, [issue])
    chunk_ids = [contract.chunk_id for contract in env["contracts"]]
    script = "\n\n".join(env["texts"])
    store.write_text(SCRIPT, script)
    store.write_model(
        ASSEMBLY_REPORT,
        AssemblyReport(
            assembled_chunk_ids=chunk_ids,
            chunk_order=chunk_ids,
            final_script_hash=content_hash(script),
            assembled_with_issues=False,
        ),
    )
    client = make_client(env)
    before = dir_digest(store.run_dir)

    validation = client.get(f"/api/runs/{BASE_RUN}/reports/validation").json()
    assembly = client.get(f"/api/runs/{BASE_RUN}/reports/assembly").json()

    assert validation["summary"]["by_kind"] == {"tail_mismatch": 1}
    assert validation["summary"]["traceability_rate"] == 1.0
    assert assembly["summary"]["assembled_chunks"] == 2
    assert assembly["summary"]["script_chars"] == len(script)
    assert dir_digest(store.run_dir) == before


def test_stage_report_marks_missing_artifact_unavailable(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    response = make_client(env).get(f"/api/runs/{BASE_RUN}/reports/validation")
    assert response.status_code == 200
    assert response.json()["available"] is False


def test_invalid_config_relationship_is_rejected_before_spend(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({})
    response = make_client(env, call=fake).post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "config_overrides": {
                "stage_budgets": {
                    "rendering": {
                        "target_material_tokens": 5000,
                        "max_material_tokens": 1000,
                    }
                }
            },
        },
    )
    assert response.status_code == 400
    assert "target_material_tokens" in response.json()["detail"]
    assert fake.calls == []


# --- fresh ingestion run ------------------------------------------------------


def test_ingestion_run_dry_run_is_all_sources_and_writes_nothing(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    fake = FakeCall({"atom_boundary_dsl": "A | p0001 | narration | "})
    client = make_client(env, call=fake)
    before = dir_digest(env["runs_root"])

    response = client.post("/api/runs/ingestion", json={"project_id": "proj"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["confirm_required"] is True
    assert payload["estimate"]["sources"] == 2
    assert payload["estimate"]["pieces"] == 2
    assert payload["estimate"]["windows"] == 2
    assert payload["estimate"]["calls"] == 2
    assert payload["estimate"]["max_calls_with_repair"] == 4
    assert [row["source_id"] for row in payload["estimate"]["source_stats"]] == ["s1", "s2"]
    assert payload["estimate"]["est_input_tokens"] > 0
    assert fake.calls == []
    assert dir_digest(env["runs_root"]) == before


def test_ingestion_run_confirm_creates_root_artifacts_and_audit(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    fake = FakeCall({"atom_boundary_dsl": "A | p0001 | narration | "})
    client = make_client(env, call=fake)
    base_dir = env["runs_root"] / BASE_RUN
    before = dir_digest(base_dir)

    response = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "run_label": "All Sources", "confirm": True},
    )

    assert response.status_code == 200
    submitted = response.json()
    assert "proj-all-sources-ingestion-" in submitted["run_id"]
    job = wait_job(client, submitted["job_id"])
    assert job["state"] == "succeeded"
    assert job["base_run_id"] is None
    assert job["result"] == {
        "mode": "ingestion",
        "sources": 2,
        "pieces": 2,
        "windows": 2,
        "atoms": 2,
        "calls": 2,
        "report_items": 0,
        "errors": 0,
        "warnings": 0,
        "ok": True,
    }
    assert len(fake.calls) == 2

    created = RunStore(env["runs_root"] / submitted["run_id"])
    for spec in (SOURCE_SNAPSHOTS, SOURCE_PIECES, SOURCE_WINDOWS, MATERIAL_ATOMS):
        assert created.exists(spec)
    assert not created.exists(CANDIDATE_GRAPH)
    assert [row.source_id for row in created.read_records(SOURCE_SNAPSHOTS)] == ["s1", "s2"]
    assert len(created.read_records(MATERIAL_ATOMS)) == 2

    manifest = json.loads((created.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["kind"] == "ingestion"
    assert manifest["base_run_id"] is None
    assert manifest["status"] == "complete"
    assert set(manifest["request"]) == {
        "project_id", "host_id", "output_language", "source_ids", "persona_patch",
        "config_overrides", "run_label", "mode", "name_resolution_mode",
        "target_output_kchars", "selection_human_directive",
    }
    assert manifest["request"]["output_language"] is None
    assert manifest["request"]["name_resolution_mode"] is None
    assert manifest["request"]["target_output_kchars"] is None
    assert manifest["request"]["selection_human_directive"] is None
    assert manifest["effective"]["output_language"] == "zh-CN"
    assert manifest["request"]["source_ids"] is None
    assert manifest["request"]["host_id"] is None
    assert manifest["request"]["persona_patch"] == {}
    assert manifest["request"]["mode"] == "ingestion"
    assert manifest["effective"]["source_ids"] == ["s1", "s2"]
    assert manifest["effective"]["host"] is None
    assert manifest["copied_artifacts"] == []

    audit = client.get(f"/api/runs/{submitted['run_id']}/ingestion/audit").json()
    assert audit["summary"]["sources"] == 2
    assert audit["summary"]["pieces"] == 2
    assert audit["summary"]["atoms"] == 2
    assert dir_digest(base_dir) == before


def test_ingestion_only_persists_concrete_stage_findings(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    fake = FakeCall({"atom_boundary_dsl": "not atom DSL"})
    client = make_client(env, call=fake)

    response = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "confirm": True},
    )
    submitted = response.json()
    job = wait_job(client, submitted["job_id"])

    assert job["state"] == "succeeded"
    assert job["result"]["ok"] is False
    store = RunStore(env["runs_root"] / submitted["run_id"])
    reports = store.read_records(AUDIT_REPORT)
    assert {item.kind for item in reports} == {
        "window_protocol_failed",
        "source_integrity_failed",
    }
    assert {item.stage for item in reports} == {"ingestion"}
    assert all(item.usable is False for item in reports)
    assert not store.exists(MATERIAL_ATOMS)
    parse_failures = [
        item for item in reports if item.kind == "window_protocol_failed"
    ]
    assert all(item.call_id and item.raw_response_path is None for item in parse_failures)


def test_full_run_uses_canonical_pipeline_and_writes_full_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    project_path = env["projects_dir"] / "proj" / "sources.json"
    project_payload = json.loads(project_path.read_text(encoding="utf-8"))
    project_payload["name_resolution_mode"] = "name_table"
    project_path.write_text(json.dumps(project_payload), encoding="utf-8")
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    planner_route = dict(config["routes"]["selector_path_dsl"])
    for route in ("planner_graph_dsl", "planner_node_compress_dsl"):
        config["routes"][route] = dict(planner_route, stage="planning")
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")

    captured: list[dict] = []

    def fake_run_pipeline(project, project_dir, **kwargs):
        captured.append({"project": project, "project_dir": project_dir, **kwargs})
        kwargs["store"].write_text(SCRIPT, "完整脚本")
        return RunResult(
            run_id=kwargs["store"].run_id, runs_root=str(kwargs["store"].run_dir.parent),
            sources=2, atoms=2, nodes=3, selected_nodes=2, chunks=1,
            script_chars=4, audit_items=0, calls=9, upstream_has_issues=False,
        )

    monkeypatch.setattr("tracecast.console.ingestion_run.run_pipeline", fake_run_pipeline)
    client = make_client(env)

    estimate = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "mode": "full", "host_id": "host"},
    ).json()["estimate"]
    assert estimate["mode"] == "full"
    assert estimate["calls"] is None
    assert estimate["ingestion_initial_calls"] == 2
    assert set(estimate["models"]) == {
        "atom_boundary_dsl", "planner_graph_dsl", "planner_node_compress_dsl",
        "selector_path_dsl", "selector_directive_dsl", "node_edit",
        "node_edit_block_repair",
        "name_table",
        "render_fact_context_dsl", "render_chunk",
    }

    response = client.post(
        "/api/runs/ingestion",
        json={
            "project_id": "proj",
            "mode": "full",
            "host_id": "host",
            "confirm": True,
        },
    )
    assert response.status_code == 200
    submitted = response.json()
    assert "proj-full-" in submitted["run_id"]
    job = wait_job(client, submitted["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["mode"] == "full"
    assert job["result"]["script_chars"] == 4
    assert len(captured) == 1
    assert captured[0]["project"].project_id == "proj"
    assert captured[0]["host"] == HOST_PROFILE
    assert captured[0]["store"].run_id == submitted["run_id"]

    created = RunStore(env["runs_root"] / submitted["run_id"])
    assert created.read_text(SCRIPT) == "完整脚本"
    manifest = json.loads((created.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["kind"] == "full"
    assert manifest["request"]["mode"] == "full"
    assert manifest["request"]["host_id"] == "host"
    assert manifest["effective"]["host"] == HOST_PROFILE.snapshot()
    assert manifest["status"] == "complete"
    assert "global_edge_dsl" not in manifest["effective"]["routes"]


def test_full_run_explicit_semantics_override_project_and_quick_target_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    project_path = env["projects_dir"] / "proj" / "sources.json"
    project_payload = json.loads(project_path.read_text(encoding="utf-8"))
    project_payload["name_resolution_mode"] = "name_table"
    project_path.write_text(json.dumps(project_payload), encoding="utf-8")
    english = json.loads(json.dumps(HOST, ensure_ascii=False))
    english.update(
        {
            "host_id": "host-en-us",
            "display_name": "English US Host",
            "version": "host-en-us@v1",
            "output_language": "en-US",
        }
    )
    (env["hosts_dir"] / "host-en-us.json").write_text(
        json.dumps(english, ensure_ascii=False), encoding="utf-8"
    )
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    planner_route = dict(config["routes"]["selector_path_dsl"])
    for route in ("planner_graph_dsl", "planner_node_compress_dsl"):
        config["routes"][route] = dict(planner_route, stage="planning")
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")
    captured: list[dict] = []

    def fake_run_pipeline(project, project_dir, **kwargs):
        captured.append({"project": project, "project_dir": project_dir, **kwargs})
        kwargs["store"].write_text(SCRIPT, "English script")
        return RunResult(
            run_id=kwargs["store"].run_id,
            runs_root=str(kwargs["store"].run_dir.parent),
            sources=2,
            atoms=2,
            nodes=2,
            selected_nodes=1,
            chunks=1,
            script_chars=14,
            audit_items=0,
            calls=8,
            upstream_has_issues=False,
        )

    monkeypatch.setattr("tracecast.console.ingestion_run.run_pipeline", fake_run_pipeline)
    client = make_client(env)
    request = {
        "project_id": "proj",
        "mode": "full",
        "host_id": "host-en-us",
        "output_language": "en-US",
        "name_resolution_mode": "overrides_only",
        "target_output_kchars": 12,
        "selection_human_directive": ["Prefer the independently corroborated route change."],
        "config_overrides": {
            "stage_budgets": {"selection": {"target_output_kchars": 33}}
        },
        "confirm": True,
    }

    submitted = client.post("/api/runs/ingestion", json=request)
    assert submitted.status_code == 200, submitted.text
    job = wait_job(client, submitted.json()["job_id"])
    assert job["state"] == "succeeded"
    [invocation] = captured
    assert invocation["output_language"] == "en-US"
    assert invocation["name_resolution_mode"].value == "overrides_only"
    assert invocation["selection_human_directive"] == [
        "Prefer the independently corroborated route change."
    ]
    assert invocation["config"].stage_budgets["selection"]["target_output_kchars"] == 12
    assert invocation["host"].output_language == "en-US"
    assert invocation["host"].host_thinking.human_directive == []
    assert invocation["control_prompt_decision"].source_language.value == "zh"
    assert invocation["control_prompt_decision"].variant.value == "zh"

    store = RunStore(env["runs_root"] / submitted.json()["run_id"])
    manifest = json.loads((store.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["request"]["output_language"] == "en-US"
    assert manifest["request"]["name_resolution_mode"] == "overrides_only"
    assert manifest["request"]["target_output_kchars"] == 12
    assert manifest["request"]["selection_human_directive"] == request[
        "selection_human_directive"
    ]
    assert manifest["effective"]["output_language"] == "en-US"
    assert manifest["effective"]["name_resolution_mode"] == "overrides_only"
    assert manifest["effective"]["control_prompt_decision"]["source_language"] == "zh"
    assert manifest["effective"]["control_prompt_decision"]["variant"] == "zh"
    assert manifest["effective"]["stage_budgets"]["selection"][
        "target_output_kchars"
    ] == 12
    assert "name_table" not in manifest["effective"]["routes"]


def test_full_run_overrides_only_estimate_excludes_all_name_routes(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    project_path = env["projects_dir"] / "proj" / "sources.json"
    project_payload = json.loads(project_path.read_text(encoding="utf-8"))
    project_payload["name_resolution_mode"] = "overrides_only"
    project_path.write_text(json.dumps(project_payload), encoding="utf-8")
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    planner_route = dict(config["routes"]["selector_path_dsl"])
    for route in ("planner_graph_dsl", "planner_node_compress_dsl"):
        config["routes"][route] = dict(planner_route, stage="planning")
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")

    estimate = make_client(env).post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "mode": "full", "host_id": "host"},
    )

    assert estimate.status_code == 200, estimate.text
    models = estimate.json()["estimate"]["models"]
    assert "name_table" not in models


def test_full_run_requires_a_compatible_host_but_ingestion_only_rejects_one(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    incompatible = json.loads(json.dumps(HOST, ensure_ascii=False))
    incompatible["host_id"] = "english_host"
    incompatible["display_name"] = "English Host"
    incompatible["version"] = "english_host@v1"
    incompatible["output_language"] = "en"
    (env["hosts_dir"] / "english_host.json").write_text(
        json.dumps(incompatible, ensure_ascii=False), encoding="utf-8"
    )
    client = make_client(env)

    missing = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "mode": "full"},
    )
    assert missing.status_code == 400
    assert "host_id is required" in missing.json()["detail"]

    wrong_language = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "mode": "full", "host_id": "english_host"},
    )
    assert wrong_language.status_code == 400
    assert "targets 'en'" in wrong_language.json()["detail"]

    ingestion_with_host = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "mode": "ingestion", "host_id": "host"},
    )
    assert ingestion_with_host.status_code == 400
    assert "only applies to mode=full" in ingestion_with_host.json()["detail"]


def test_ingestion_run_rejects_slice_fields_and_unsafe_project(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    client = make_client(env)

    for field in ("max_sources", "limit_source_chars"):
        response = client.post(
            "/api/runs/ingestion", json={"project_id": "proj", field: 1}
        )
        assert response.status_code == 422
    assert client.post(
        "/api/runs/ingestion", json={"project_id": "../proj"}
    ).status_code == 400


def test_project_sources_listing(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    spec_path = env["projects_dir"] / "proj" / "sources.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["sources"].append(
        {"source_id": "s3", "path": "raw/missing.txt", "profile": "script", "language": "zh"}
    )
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    client = make_client(env)

    sources = client.get("/api/projects/proj/sources").json()
    assert [row["source_id"] for row in sources] == ["s1", "s2", "s3"]
    assert sources[0]["exists"] is True
    assert sources[0]["chars"] == len("第一来源只有一段。")
    assert sources[0]["title"] == "s1"  # falls back to source_id
    assert sources[0]["profile"] == "script"
    assert sources[2]["exists"] is False and sources[2]["chars"] is None
    assert client.get("/api/projects/ghost/sources").status_code == 404


def test_ingestion_run_source_subset(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    fake = FakeCall({"atom_boundary_dsl": "A | p0001 | narration | "})
    client = make_client(env, call=fake)
    before = dir_digest(env["runs_root"])

    dry = client.post("/api/runs/ingestion", json={"project_id": "proj", "source_ids": ["s2"]})
    assert dry.status_code == 200
    estimate = dry.json()["estimate"]
    assert estimate["sources"] == 1
    assert [row["source_id"] for row in estimate["source_stats"]] == ["s2"]
    assert fake.calls == []
    assert dir_digest(env["runs_root"]) == before

    assert client.post(
        "/api/runs/ingestion", json={"project_id": "proj", "source_ids": []}
    ).status_code == 400
    assert client.post(
        "/api/runs/ingestion", json={"project_id": "proj", "source_ids": ["s1", "s1"]}
    ).status_code == 400
    unknown = client.post(
        "/api/runs/ingestion", json={"project_id": "proj", "source_ids": ["ghost"]}
    )
    assert unknown.status_code == 400 and "ghost" in unknown.json()["detail"]

    confirmed = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "source_ids": ["s2"], "confirm": True},
    ).json()
    job = wait_job(client, confirmed["job_id"])
    assert job["state"] == "succeeded"
    created = RunStore(env["runs_root"] / confirmed["run_id"])
    assert [row.source_id for row in created.read_records(SOURCE_SNAPSHOTS)] == ["s2"]
    manifest = json.loads((created.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["request"]["source_ids"] == ["s2"]
    assert manifest["effective"]["source_ids"] == ["s2"]


def test_full_run_legacy_persona_directive_is_selection_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    planner_route = dict(config["routes"]["selector_path_dsl"])
    for route in ("planner_graph_dsl", "planner_node_compress_dsl"):
        config["routes"][route] = dict(planner_route, stage="planning")
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")

    captured: list[dict] = []

    def fake_run_pipeline(project, project_dir, **kwargs):
        captured.append({"project": project, "project_dir": project_dir, **kwargs})
        kwargs["store"].write_text(SCRIPT, "完整脚本")
        return RunResult(
            run_id=kwargs["store"].run_id, runs_root=str(kwargs["store"].run_dir.parent),
            sources=2, atoms=2, nodes=3, selected_nodes=2, chunks=1,
            script_chars=4, audit_items=0, calls=9, upstream_has_issues=False,
        )

    monkeypatch.setattr("tracecast.console.ingestion_run.run_pipeline", fake_run_pipeline)
    client = make_client(env)
    patch = {"viewpoint_profile": {"human_directive": ["多讲配乐幕后"]}}

    res = client.post(
        "/api/runs/ingestion",
        json={
            "project_id": "proj",
            "mode": "full",
            "host_id": "host",
            "persona_patch": patch,
            "confirm": True,
        },
    )
    assert res.status_code == 200
    submitted = res.json()
    job = wait_job(client, submitted["job_id"])
    assert job["state"] == "succeeded"
    assert captured[0]["selection_human_directive"] == ["多讲配乐幕后"]
    effective_host = captured[0]["host"]
    assert effective_host.host_thinking.human_directive == []
    # untouched Host leaves inherit the selected external profile
    assert (
        effective_host.host_thinking.viewpoint
        == HOST_PROFILE.host_thinking.viewpoint
    )

    created = RunStore(env["runs_root"] / submitted["run_id"])
    manifest = json.loads((created.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["request"]["persona_patch"] == patch
    assert manifest["request"]["selection_human_directive"] is None
    assert manifest["effective"]["selection_human_directive"] == ["多讲配乐幕后"]
    assert (
        manifest["effective"]["host"]["profile"]["host_thinking"]["human_directive"]
        == []
    )


def test_persona_patch_rejected_for_ingestion_mode(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    fake = FakeCall({})
    client = make_client(env, call=fake)

    res = client.post(
        "/api/runs/ingestion",
        json={
            "project_id": "proj",
            "persona_patch": {"viewpoint_profile": {"human_directive": ["x"]}},
        },
    )
    assert res.status_code == 400 and "mode=full" in res.json()["detail"]
    assert fake.calls == []
    # an empty patch is a no-op for any mode
    empty = client.post(
        "/api/runs/ingestion", json={"project_id": "proj", "persona_patch": {}}
    )
    assert empty.status_code == 200 and empty.json()["confirm_required"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("output_language", "en-US"),
        ("name_resolution_mode", "overrides_only"),
        ("target_output_kchars", 18),
        ("selection_human_directive", []),
        ("selection_human_directive", ["只选择材料支持的主线"]),
    ],
)
def test_ingestion_only_rejects_every_explicit_full_run_semantic_override(
    tmp_path: Path, field: str, value: object
) -> None:
    env = build_env(tmp_path)
    configure_ingestion_project(env)
    fake = FakeCall({})
    client = make_client(env, call=fake)

    response = client.post(
        "/api/runs/ingestion",
        json={"project_id": "proj", "mode": "ingestion", field: value},
    )

    assert response.status_code == 400
    assert field in response.json()["detail"]
    assert fake.calls == []


def test_static_assets_are_mounted(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)
    root = client.get("/")
    assert root.status_code == 200
    assert root.headers["cache-control"] == "no-store"
    assert root.headers["clear-site-data"] == '"cache"'
    index = client.get("/static/index.html")
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert 'href="#/hosts"' in index.text
    assert 'href="#/setup">快速配置</a>' in index.text
    assert index.text.index('href="#/setup"') < index.text.index('href="#/config"')
    main_response = client.get("/static/js/main.js")
    assert main_response.headers["cache-control"] == "no-store"
    main = main_response.text
    assert 'register(/^\\/hosts$/' in main
    assert 'register(/^\\/setup$/' in main
    assert 'register(/^\\/new\\/project$/' in main
    assert 'location.hash = "#/setup"' in main
    assert "refreshHosts()" in main
    hosts_view = client.get("/static/js/views/hosts.js").text
    assert 'api(`/api/hosts/${encodeURIComponent(selectedHostId)}`)' in hosts_view
    assert 'post("/api/hosts/build", payload(false))' in hosts_view
    assert 'new TextDecoder("utf-8", {fatal: true})' in hosts_view
    assert "function hostIdFromDisplayName" in hosts_view
    assert 'id="builder-name" type="text" required' in hosts_view
    assert 'data-builder-host-id-preview' not in hosts_view
    assert "填写名称后自动生成" not in hosts_view
    assert '<summary>高级设置</summary>' in hosts_view
    assert 'pattern="[a-z0-9][a-z0-9-]{0,63}"' in hosts_view
    assert 'data-builder-reset-id' in hosts_view
    assert 'estimate.piece_max_tokens' in hosts_view
    new_run = client.get("/static/js/views/newRun.js").text
    assert 'host_id: mode === "full"' in new_run
    assert 'q("host-wrap").classList.toggle' in new_run
    ordered_controls = [
        'data-nr="project"',
        'data-nr="output-language"',
        'data-nr="host"',
        'data-nr="sources"',
        'data-nr="mode"',
        'data-nr="strategy-wrap"',
        'data-nr="label"',
        'data-nr="directive-wrap"',
        'data-nr="config"',
        'data-nr="estimate"',
    ]
    assert [new_run.index(marker) for marker in ordered_controls] == sorted(
        new_run.index(marker) for marker in ordered_controls
    )
    assert "目标成稿长度（千字，可选）" in new_run
    assert "路径取舍要求（每行一条）" in new_run
    assert "不能补充事实，也不是正文措辞指令" in new_run
    assert "多讲配乐幕后" not in new_run
    assert 'href="#/new/project">创建材料项目' in new_run
    assert '<select data-nr="output-language">' in new_run
    assert '<input data-nr="output-language"' not in new_run
    assert 'q("output-language").addEventListener("change"' in new_run
    assert 'q("output-language").value.trim() !== projectDefaultLanguage' in new_run
    assert 'q("name-mode").value !== "project_default"' in new_run
    setup_js = client.get("/static/js/views/setup.js").text
    assert '<div class="page-title">快速配置</div>' in setup_js
    assert 'configured ? "CONNECTION" : "FIRST RUN"' in setup_js
    assert "function currentConnection" in setup_js
    assert 'routes.render_chunk' in setup_js
    assert 'routes.selector_path_dsl' in setup_js
    assert "仅保存在本机，不会回显" in setup_js
    assert "保存会用这套单网关配置" not in setup_js
    assert "导入本地文本，确认编码" not in setup_js
    assert "data-starter" not in setup_js
    assert 'type="password" autocomplete="new-password"' in setup_js
    assert "confirm: q(\"confirm\").checked" in setup_js
    assert 'q("key").value = ""' in setup_js
    assert "密钥不会在界面或 API 中回显" in setup_js
    assert 'data-setup="status"' in setup_js
    assert 'state.setup.api_key_configured' in setup_js
    for marker in (
        'data-setup="model" type="text"',
        'data-setup="selection" type="text"',
        'data-setup="path" type="text"',
    ):
        assert marker in setup_js
    project_js = client.get("/static/js/views/newProject.js").text
    assert 'accept=".txt,.md,.markdown,.srt,.vtt' in project_js
    assert 'data-pd="id" type="text" required maxlength="64"' in project_js
    assert 'pattern="[a-z0-9][a-z0-9_-]{0,63}"' in project_js
    assert '目标输出语言<select data-pd="output">' in project_js
    assert '<input data-pd="output"' not in project_js
    for marker in (
        'data-file-language type="text"',
        'data-r="id" type="text"',
        'data-r="language" type="text"',
        'data-r="title" type="text"',
        'data-r="note" type="text"',
    ):
        assert marker in project_js
    assert "candidate_previews" in project_js
    assert "手动模式，零模型调用" in project_js
    assert 'q("file-rows").addEventListener("change", invalidate)' in project_js
    assert "unknown 或 needs_review 不能提交" in project_js
    state_js = client.get("/static/js/state.js").text
    assert 'export function outputLanguageChoices' in state_js
    assert '"zh-CN"' in state_js
    assert '"en-US"' in state_js
    derive = client.get("/static/js/views/run/derive.js").text
    assert 'p.host_id = q("host").value' in derive
    assert "export async function renderPathAdjustment" in derive
    assert "export async function renderStageTests" in derive
    experiments_js = client.get("/static/js/views/run/experiments.js").text
    for label in ("路径调整", "阶段测试", "单段测试", "运行对比"):
        assert label in experiments_js
    assert 'const LEGACY_TOOLS = {rerun: "path"}' in experiments_js
    shell_js = client.get("/static/js/views/run/shell.js").text
    assert '["experiments", "调整", renderExperiments]' in shell_js
    diff_js = client.get("/static/js/views/run/diff.js").text
    assert "对比正文结果" in diff_js
    assert "对比渲染注入块" in diff_js
    assert "Prompt 对比" not in diff_js
    assert "/api/diff/prompt" not in diff_js
    diff_view_js = client.get("/static/js/components/diffView.js").text
    assert "renderPromptDiff" not in diff_view_js
    chunklab_js = client.get(
        "/static/js/views/run/chunklab.js"
    ).text
    assert "真实 7 块快照" in chunklab_js
    assert "真实 6 块快照" not in chunklab_js
    derive_js = client.get("/static/js/views/run/derive.js").text
    assert "只覆盖两个跨 chunk 不变的 host 块" in derive_js
    assert "位置、对应位置的风格示例、事实联系、材料和禁写项" in derive_js
    assert "previous tail" not in derive_js
    labels_js = client.get("/static/js/labels.js").text
    for label in ("SUM 体量", "材料 token 保留", "正文体量", "带问题装配"):
        assert label in labels_js
    report_js = client.get("/static/js/components/kvReport.js").text
    assert "完整路径大纲" in report_js
    assert "recursively" in report_js


def test_config_refresh_purges_stale_global_route_overrides_before_emit(
    tmp_path: Path,
) -> None:
    """Keep the no-build Console's served state module cleanup contract visible.

    The repository has no JavaScript test runner, so this exercises the mounted
    production asset and constrains the control-flow/persistence statements that
    prevent a removed runtime route from leaking into later spend payloads.
    """
    env = build_env(tmp_path)
    client = make_client(env)

    config = client.get("/api/config").json()
    assert "global_edge_dsl" not in config["routes"]
    response = client.get("/static/js/state.js")
    assert response.status_code == 200
    source = response.text

    refresh = source[
        source.index("export async function refreshConfig()"):
        source.index("export const runById")
    ]
    assert refresh.index('state.config = await api("/api/config");') < refresh.index(
        "dropStaleOverrideRoutes()"
    ) < refresh.index('emit("config-changed")')

    stale = source[
        source.index("export function staleOverrideRoutes()"):
        source.index("export function dropStaleOverrideRoutes()")
    ]
    assert "const known = state.config.routes ?? {};" in stale
    assert "!(name in known)" in stale

    drop = source[source.index("export function dropStaleOverrideRoutes()"):]
    assert "const stale = staleOverrideRoutes();" in drop
    assert "if (!stale.length) return [];" in drop
    assert "const routes = {...state.globalOverrides.routes};" in drop
    assert "for (const name of stale) delete routes[name];" in drop
    assert "saveGlobalOverrides({...state.globalOverrides, routes});" in drop
    assert "return stale;" in drop


# --- chunk blocks / chunklab ------------------------------------------------------


def test_chunk_blocks_seven_keys_positions_and_recorded_match(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)
    first, second = env["artifacts"]

    blocks_1 = client.get(f"/api/runs/{BASE_RUN}/chunks/{first.chunk_id}/blocks").json()
    assert len(blocks_1["blocks"]) == 7
    assert blocks_1["portable_block_keys"] == [
        "host_thinking_block",
        "host_expression_block",
    ]
    assert (
        blocks_1["chunk_index"],
        blocks_1["chunk_count"],
        blocks_1["segment_position"],
    ) == (1, 2, "opening")
    assert "segment_position: opening" in blocks_1["blocks"]["chunk_position_block"]
    assert blocks_1["block_source"] == "reconstructed"
    assert blocks_1["reconstructed_blocks"] == blocks_1["blocks"]
    assert blocks_1["edited_keys"] == []
    assert blocks_1["matches_recorded_request"] is True

    blocks_2 = client.get(f"/api/runs/{BASE_RUN}/chunks/{second.chunk_id}/blocks").json()
    assert set(blocks_2["blocks"]) == set(blocks_1["blocks"])
    assert (
        blocks_2["chunk_index"],
        blocks_2["chunk_count"],
        blocks_2["segment_position"],
    ) == (2, 2, "ending")
    assert "segment_position: ending" in blocks_2["blocks"]["chunk_position_block"]
    assert (
        blocks_2["blocks"]["style_examples_block"]
        != blocks_1["blocks"]["style_examples_block"]
    )
    assert blocks_2["matches_recorded_request"] is True
    assert blocks_2["baseline"]["call_id"] == "call-2"


def test_chunklab_dry_run_writes_nothing(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({"render_chunk": "改后的正文。"})
    client = make_client(env, call=fake)
    chunk_id = env["artifacts"][0].chunk_id

    blocks = client.get(f"/api/runs/{BASE_RUN}/chunks/{chunk_id}/blocks").json()["blocks"]
    blocks["annotated_materials_block"] += "\n补充一行指令。"
    res = client.post("/api/chunklab/render", json={
        "base_run_id": BASE_RUN, "chunk_id": chunk_id, "blocks": blocks,
    }).json()
    assert res["confirm_required"] is True
    assert res["estimate"]["calls"] == 1
    assert res["edited_keys"] == ["annotated_materials_block"]
    assert fake.calls == []
    assert [p.name for p in env["runs_root"].iterdir()] == [BASE_RUN]


def test_chunklab_confirm_renders_and_preserves_base(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({"render_chunk": "重写后的正文。第二句也在。"})
    client = make_client(env, call=fake)
    chunk_id = env["artifacts"][0].chunk_id
    base_dir = env["runs_root"] / BASE_RUN
    before = dir_digest(base_dir)

    blocks = client.get(f"/api/runs/{BASE_RUN}/chunks/{chunk_id}/blocks").json()["blocks"]
    blocks["annotated_materials_block"] = "【叙事提示｜只讲雨夜】\n\n雨夜。"
    res = client.post("/api/chunklab/render", json={
        "base_run_id": BASE_RUN, "chunk_id": chunk_id, "blocks": blocks, "confirm": True,
    }).json()
    job = wait_job(client, res["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["text"] == "重写后的正文。第二句也在。"

    # the edited block reached the provider verbatim inside the assembled user prompt
    sent_user = fake.calls[0]["messages"][1]["content"]
    assert "只讲雨夜" in sent_user

    derived = RunStore(env["runs_root"] / res["run_id"])
    chunks = derived.read_records(CHUNK_ARTIFACTS)
    assert len(chunks) == 1 and chunks[0].chunk_id == chunk_id
    assert chunks[0].metadata["edited_blocks"] == ["annotated_materials_block"]
    assert (
        chunks[0].metadata["chunk_index"],
        chunks[0].metadata["chunk_count"],
        chunks[0].metadata["segment_position"],
    ) == (1, 2, "opening")
    assert {
        key: fake.calls[0]["work_unit"].metadata[key]
        for key in ("chunk_index", "chunk_count", "segment_position")
    } == {
        "chunk_index": 1,
        "chunk_count": 2,
        "segment_position": "opening",
    }
    assert len(derived.read_records(RENDER_CONTRACTS)) == 2
    copied_plan = derived.read_model(NAME_RESOLUTION_PLAN)
    assert copied_plan.contract_ids == [
        contract.contract_id for contract in env["contracts"]
    ]
    manifest = json.loads((derived.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["kind"] == "chunklab" and manifest["status"] == "complete"
    assert manifest["base_run_id"] == BASE_RUN
    assert manifest["artifact_sources"][RENDER_CONTRACTS.relpath] == BASE_RUN
    assert manifest["artifact_sources"][CHUNK_ARTIFACTS.relpath] == BASE_RUN
    assert manifest["request"]["injection_blocks"] == blocks
    assert {
        key: manifest["request"][key]
        for key in ("chunk_index", "chunk_count", "segment_position")
    } == {
        "chunk_index": 1,
        "chunk_count": 2,
        "segment_position": "opening",
    }
    assert {
        key: manifest["effective"][key]
        for key in ("chunk_index", "chunk_count", "segment_position")
    } == {
        "chunk_index": 1,
        "chunk_count": 2,
        "segment_position": "opening",
    }
    assert dir_digest(base_dir) == before  # base run untouched

    # A new lab opened from this lab receives the complete prior seven-block
    # snapshot, without trying to split a multiline provider prompt in reverse.
    continued = client.get(
        f"/api/runs/{res['run_id']}/chunks/{chunk_id}/blocks"
    ).json()
    assert continued["block_source"] == "manifest_snapshot"
    assert continued["blocks"] == blocks

    # Stage Trace reads the same snapshot as the exact Chunk Lab input source.
    call_id = chunks[0].metadata["call_id"]
    trace_request = {"messages": fake.calls[0]["messages"]}
    derived.write_raw_payload(
        str(call_id),
        "request",
        trace_request,
    )
    derived.write_records(
        LLM_CALL_RECORDS,
        [
            LlmCallRecord(
                call_id=str(call_id),
                stage="rendering",
                operation_route="render_chunk",
                provider="test",
                model="fake-render",
                input_hash=f"sha256:{sha256_json(trace_request)}",
                provider_status=ProviderStatus.success,
                created_at=datetime.now(tz=UTC),
                work_unit=LlmWorkUnitRef(
                    kind="render_chunk",
                    unit_id=chunk_id,
                    order_index=0,
                    input_refs=["c1", "m1"],
                ),
            )
        ],
    )
    trace = client.get(
        f"/api/runs/{res['run_id']}/trace/rendering/{chunk_id}"
    ).json()
    assert trace["input_blocks"] == blocks

    # A run without the full seven-block snapshot is rejected. The former
    # edited-subset compatibility overlay is intentionally gone.
    snapshot = manifest["request"].pop("injection_blocks")
    (derived.run_dir / "console" / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    incomplete = client.get(
        f"/api/runs/{res['run_id']}/chunks/{chunk_id}/blocks"
    )
    assert incomplete.status_code == 400
    assert "complete injection snapshot" in incomplete.json()["detail"]
    manifest["request"]["injection_blocks"] = snapshot
    (derived.run_dir / "console" / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    # Both the saved injection blocks and their reconstructed baseline remain
    # pinned to the run's Host snapshot even if the external Host later drifts.
    drifted_host = json.loads(json.dumps(HOST, ensure_ascii=False))
    drifted_host["host_thinking"]["viewpoint"] = (
        "这是一份后改的观点，不属于上次实验。"
    )
    (env["hosts_dir"] / "host.json").write_text(
        json.dumps(drifted_host, ensure_ascii=False),
        encoding="utf-8",
    )
    recorded = client.get(
        f"/api/runs/{res['run_id']}/chunks/{chunk_id}/blocks"
    ).json()
    assert recorded["block_source"] == "manifest_snapshot"
    assert recorded["block_source_run_id"] == res["run_id"]
    assert recorded["blocks"]["annotated_materials_block"] == blocks[
        "annotated_materials_block"
    ]
    assert "以结构变化讲故事" in recorded["blocks"]["host_thinking_block"]
    assert "以结构变化讲故事" in recorded["reconstructed_blocks"]["host_thinking_block"]
    assert "后改的观点" not in recorded["reconstructed_blocks"]["host_thinking_block"]
    assert recorded["blocks"]["host_thinking_block"] == recorded[
        "reconstructed_blocks"
    ]["host_thinking_block"]
    next_dry_run = client.post(
        "/api/chunklab/render",
        json={
            "base_run_id": res["run_id"],
            "chunk_id": chunk_id,
            "blocks": recorded["blocks"],
        },
    )
    assert next_dry_run.status_code == 200
    assert next_dry_run.json()["confirm_required"] is True

    diff = client.get("/api/diff/chunks", params={
        "run_a": BASE_RUN, "chunk_a": chunk_id, "run_b": res["run_id"], "chunk_b": chunk_id,
    }).json()
    assert diff["ratio"] < 1
    assert any(op["op"] != "equal" for op in diff["ops"])


def test_chunklab_rejects_bad_block_sets(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)
    chunk_id = env["artifacts"][0].chunk_id
    blocks = client.get(f"/api/runs/{BASE_RUN}/chunks/{chunk_id}/blocks").json()["blocks"]

    missing = dict(blocks)
    missing.pop("forbidden_block")
    res = client.post("/api/chunklab/render", json={
        "base_run_id": BASE_RUN, "chunk_id": chunk_id, "blocks": missing,
    })
    assert res.status_code == 400 and "forbidden_block" in res.json()["detail"]

    extra = dict(blocks, rogue_block="x")
    res = client.post("/api/chunklab/render", json={
        "base_run_id": BASE_RUN, "chunk_id": chunk_id, "blocks": extra,
    })
    assert res.status_code == 400 and "rogue_block" in res.json()["detail"]


# --- derive -------------------------------------------------------------------------


def test_derive_selection_confirm_and_path_diff(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({
        "selector_path_dsl": "P C002 | core | q",
        "selector_directive_dsl": (
            "D P001 | 让反派线改变听众对前一段冲突的理解，并把疑问留给后续。"
        ),
    })
    client = make_client(env, call=fake)

    dry = client.post(
        "/api/derive/selection",
        json={"base_run_id": BASE_RUN, "host_id": "host"},
    ).json()
    assert dry["confirm_required"] is True and dry["estimate"]["calls"] == 2
    assert "max_calls_with_repair" not in dry["estimate"]
    assert fake.calls == []

    res = client.post("/api/derive/selection", json={
        "base_run_id": BASE_RUN,
        "host_id": "host",
        "persona_patch": {"viewpoint_profile": {"prefer": ["反派动机"]}},
        "confirm": True,
    }).json()
    job = wait_job(client, res["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["selected"] == 1

    # persona feedback layer reached the selector prompt
    sent_user = fake.calls[0]["messages"][1]["content"]
    assert "反派动机" in sent_user

    derived = RunStore(env["runs_root"] / res["run_id"])
    assert derived.exists(SELECTION_PLAN)
    assert not derived.exists(CANDIDATE_GRAPH)
    assert not derived.exists(MATERIAL_ATOMS)
    manifest = json.loads((derived.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["request"]["host_id"] == "host"
    assert manifest["effective"]["host"]["profile"]["host_thinking"]["prefer"] == [
        "反派动机"
    ]
    assert manifest["copied_artifacts"] == []
    assert manifest["artifact_sources"] == {CANDIDATE_GRAPH.relpath: BASE_RUN}

    inherited_chunks = client.get(f"/api/runs/{res['run_id']}/chunks")
    assert inherited_chunks.status_code == 200
    chunk_id = inherited_chunks.json()[0]["chunk_id"]
    inherited_blocks = client.get(
        f"/api/runs/{res['run_id']}/chunks/{chunk_id}/blocks"
    )
    assert inherited_blocks.status_code == 200
    assert inherited_blocks.json()["artifact_sources"][
        RENDER_CONTRACTS.relpath
    ] == BASE_RUN
    chunklab_dry = client.post(
        "/api/chunklab/render",
        json={
            "base_run_id": res["run_id"],
            "chunk_id": chunk_id,
            "blocks": inherited_blocks.json()["blocks"],
        },
    )
    assert chunklab_dry.status_code == 200
    assert chunklab_dry.json()["confirm_required"] is True

    diff = client.get("/api/diff/selection", params={
        "run_a": BASE_RUN, "run_b": res["run_id"],
    }).json()
    assert [n["candidate_id"] for n in diff["removed"]] == ["c1"]
    assert diff["removed"][0]["summary"] == "主角登场"
    changed = {c["candidate_id"]: c["fields"] for c in diff["changed"]}
    assert changed["c2"]["role"] == ["bridge", "core"]
    assert changed["c2"]["treatment"] == ["summarize", "quote"]


def test_derive_selection_progress_total_is_two_primary_calls(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    fake = FakeCall(
        {
            "selector_path_dsl": "P C001 | core | p",
            "selector_directive_dsl": "D P001 | -",
        }
    )
    client = make_client(env, call=fake)

    dry = client.post(
        "/api/derive/selection",
        json={"base_run_id": BASE_RUN, "host_id": "host"},
    ).json()
    assert dry["estimate"]["calls"] == 2
    assert "max_calls_with_repair" not in dry["estimate"]

    submitted = client.post(
        "/api/derive/selection",
        json={"base_run_id": BASE_RUN, "host_id": "host", "confirm": True},
    ).json()
    job = wait_job(client, submitted["job_id"])

    assert job["state"] == "succeeded"
    assert job["progress"] == {
        "done": 2,
        "total": 2,
        "message": "selection 2/2",
    }


def test_sparse_selection_advances_through_node_edit_to_render(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({
        "selector_path_dsl": "P C002 | core | q",
        "selector_directive_dsl": (
            "D P001 | 让反派线改变听众对前一段冲突的理解，并把疑问留给后续。"
        ),
        "node_edit": "NODE C001\nK M001 | 保留原始材料\nENDNODE",
    })
    client = make_client(env, call=fake)

    selection_response = client.post("/api/derive/selection", json={
        "base_run_id": BASE_RUN,
        "host_id": "host",
        "confirm": True,
    }).json()
    selection_job = wait_job(client, selection_response["job_id"])
    assert selection_job["state"] == "succeeded"
    selection_run = selection_response["run_id"]

    node_response = client.post("/api/derive/node-edit", json={
        "base_run_id": selection_run,
        "confirm": True,
    }).json()
    node_job = wait_job(client, node_response["job_id"])
    assert node_job["state"] == "succeeded"

    node_store = RunStore(env["runs_root"] / node_response["run_id"])
    assert node_store.exists(NODE_EDIT_PLAN)
    assert node_store.exists(EDITED_MATERIALS)
    assert not node_store.exists(CANDIDATE_GRAPH)
    assert not node_store.exists(SELECTION_PLAN)
    assert not node_store.exists(MATERIAL_ATOMS)
    manifest = json.loads((node_store.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["copied_artifacts"] == []
    assert manifest["artifact_sources"] == {
        CANDIDATE_GRAPH.relpath: BASE_RUN,
        MATERIAL_ATOMS.relpath: BASE_RUN,
        SELECTION_PLAN.relpath: selection_run,
        AUDIT_REPORT.relpath: selection_run,
    }
    detail = client.get(f"/api/runs/{node_response['run_id']}").json()
    graph_slot = next(
        slot for slot in detail["slots"] if slot["relpath"] == CANDIDATE_GRAPH.relpath
    )
    assert graph_slot["exists_here"] is False
    assert graph_slot["available"] is True
    assert graph_slot["origin_run_id"] == BASE_RUN
    inherited = client.get(
        f"/api/runs/{node_response['run_id']}/artifact",
        params={"relpath": CANDIDATE_GRAPH.relpath},
    ).json()
    assert inherited["origin_run_id"] == BASE_RUN
    assert inherited["data"]["graph_id"] == "g1"

    render_dry_run = client.post("/api/derive/render", json={
        "base_run_id": node_response["run_id"],
    })
    assert render_dry_run.status_code == 200
    # One render body + one complete selected-material Name Table.
    assert render_dry_run.json()["estimate"]["calls"] == 2


def test_derive_render_dry_run_counts_chunks(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({})
    client = make_client(env, call=fake)
    dry = client.post("/api/derive/render", json={"base_run_id": BASE_RUN}).json()
    assert dry["confirm_required"] is True
    # Two bodies + one fact-context + one complete selected-material Name Table.
    assert dry["estimate"]["calls"] == 4
    assert dry["estimate"]["max_calls_with_repair"] == 4
    assert dry["estimate"]["est_input_tokens"] > 0
    assert any(
        "does not issue semantic repair calls" in note
        for note in dry["estimate"]["notes"]
    )
    assert fake.calls == []


def test_render_derive_mode_priority_lineage_and_overrides_only_route_exclusion(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    project_path = env["projects_dir"] / "proj" / "sources.json"
    project_payload = json.loads(project_path.read_text(encoding="utf-8"))
    project_payload["name_resolution_mode"] = "overrides_only"
    project_path.write_text(json.dumps(project_payload), encoding="utf-8")
    decision = resolve_control_prompt_locale(
        [
            ControlPromptSample(
                text="这是一段明确的中文来源材料。" * 4,
                language_tag="zh-CN",
            ),
            ControlPromptSample(
                text="This is a clearly English source passage. " * 4,
                language_tag="en-US",
            ),
        ]
    )
    assert decision.source_language.value == "mixed"
    assert decision.variant.value == "en"
    base = RunStore.for_run(env["runs_root"], BASE_RUN)
    manifest_path = base.run_dir / "console" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "tracecast-console-manifest/1",
                "kind": "full",
                "base_run_id": None,
                "status": "complete",
                "request": {"project_id": "proj"},
                "effective": {
                    "output_language": "zh-CN",
                    "name_resolution_mode": "name_table",
                    "control_prompt_decision": decision.model_dump(mode="json"),
                },
            }
        ),
        encoding="utf-8",
    )
    prompt_dir = env["config_path"].parent / "lineage-prompts"
    prompt_dir.mkdir()
    render_system = _prompt_file(
        prompt_dir / "render.system.en.md",
        "test_render_sys.en@v1",
        "Render the body without adding facts.",
    )
    render_user = prompt_dir / "render.user.en.md"
    render_user.write_text(
        RENDER_USER_TEMPLATE.replace(
            "test_render_user@v1", "test_render_user.en@v1"
        ),
        encoding="utf-8",
    )
    context_system = _prompt_file(
        prompt_dir / "context.system.en.md",
        "test_context_sys.en@v1",
        "Find sparse planned repetition.",
    )
    context_user = _prompt_file(
        prompt_dir / "context.user.en.md",
        "test_context_user.en@v1",
        "{{output_language}}\n{{node_outline}}",
    )
    config = json.loads(env["config_path"].read_text(encoding="utf-8"))
    config["routes"]["render_chunk"].update(
        {
            "system_prompt": str(render_system).replace(".en.md", ".{locale}.md"),
            "user_prompt": str(render_user).replace(".en.md", ".{locale}.md"),
        }
    )
    config["routes"]["render_fact_context_dsl"].update(
        {
            "system_prompt": str(context_system).replace(".en.md", ".{locale}.md"),
            "user_prompt": str(context_user).replace(".en.md", ".{locale}.md"),
        }
    )
    env["config_path"].write_text(json.dumps(config), encoding="utf-8")
    fake = FakeCall(
        {
            "render_fact_context_dsl": (
                "HINT C002N001 | Earlier planning already schedules the opening."
            ),
            "render_chunk": "离线正文。第二句完整。",
        }
    )
    client = make_client(env, call=fake)

    inherited = client.post("/api/derive/render", json={"base_run_id": BASE_RUN})
    assert inherited.status_code == 200, inherited.text
    inherited_estimate = inherited.json()["estimate"]
    assert inherited_estimate["calls"] == 4
    assert "name_table" in inherited_estimate["est_input_tokens_by_stage"]
    assert "name_table" in inherited_estimate["models_by_stage"]

    explicit = client.post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "name_resolution_mode": "overrides_only",
        },
    )
    assert explicit.status_code == 200, explicit.text
    estimate = explicit.json()["estimate"]
    assert estimate["calls"] == 3
    assert not any(key.startswith("name_") for key in estimate["est_input_tokens_by_stage"])
    assert not any(key.startswith("name_") for key in estimate["models_by_stage"])
    assert estimate["configured_uncached_input_cost_by_stage"] == {}
    assert fake.calls == []

    submitted = client.post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "name_resolution_mode": "overrides_only",
            "confirm": True,
        },
    )
    assert submitted.status_code == 200, submitted.text
    job = wait_job(client, submitted.json()["job_id"])
    assert job["state"] == "succeeded", job
    assert Counter(call["operation_route"] for call in fake.calls) == Counter(
        {"render_fact_context_dsl": 1, "render_chunk": 2}
    )
    assert all(".en@v1" in call["prompt_version"] for call in fake.calls)
    derived = RunStore(env["runs_root"] / submitted.json()["run_id"])
    manifest = json.loads((derived.run_dir / "console/manifest.json").read_text("utf-8"))
    assert manifest["request"]["output_language"] is None
    assert manifest["request"]["name_resolution_mode"] == "overrides_only"
    assert manifest["effective"]["output_language"] == "zh-CN"
    assert manifest["effective"]["name_resolution_mode"] == "overrides_only"
    assert manifest["effective"]["control_prompt_decision"] == decision.model_dump(
        mode="json"
    )
    assert set(manifest["effective"]["routes"]) == {
        "render_fact_context_dsl",
        "render_chunk",
    }


def test_legacy_relation_scan_lineage_migrates_to_zero_name_calls(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    base = RunStore.for_run(env["runs_root"], BASE_RUN)
    manifest_path = base.run_dir / "console" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "tracecast-console-manifest/1",
                "kind": "full",
                "base_run_id": None,
                "status": "complete",
                "request": {"project_id": "proj"},
                "effective": {
                    "output_language": "zh-CN",
                    "name_resolution_mode": "relation_scan",
                },
            }
        ),
        encoding="utf-8",
    )
    fake = FakeCall(
        {
            "render_fact_context_dsl": (
                "HINT C002N001 | Earlier planning already schedules the opening."
            ),
            "render_chunk": "离线正文。第二句完整。",
        }
    )
    context = ConsoleContext(
        runs_root=env["runs_root"],
        projects_dir=env["projects_dir"],
        hosts_dir=env["hosts_dir"],
        config_path=env["config_path"],
        profiles_path=env["profiles_path"],
        call=fake,
    )
    assert resolve_lineage_effective(context, BASE_RUN)[
        "name_resolution_mode"
    ] == "overrides_only"
    client = make_client(env, call=fake)

    dry = client.post("/api/derive/render", json={"base_run_id": BASE_RUN})
    assert dry.status_code == 200, dry.text
    assert dry.json()["estimate"]["calls"] == 3
    assert "name_table" not in dry.json()["estimate"]["models_by_stage"]

    submitted = client.post(
        "/api/derive/render",
        json={"base_run_id": BASE_RUN, "confirm": True},
    )
    assert submitted.status_code == 200, submitted.text
    job = wait_job(client, submitted.json()["job_id"])
    assert job["state"] == "succeeded", job
    assert Counter(call["operation_route"] for call in fake.calls) == Counter(
        {"render_fact_context_dsl": 1, "render_chunk": 2}
    )
    derived = RunStore(env["runs_root"] / submitted.json()["run_id"])
    manifest = json.loads(
        (derived.run_dir / "console/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["effective"]["name_resolution_mode"] == "overrides_only"


def test_derive_render_applies_portable_blocks_to_every_chunk(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({
        "render_fact_context_dsl": (
            "HINT C002N001 | 主角登场已在更早计划安排。"
        ),
        "render_chunk": "声音实验后的正文。末句完整。",
    })
    client = make_client(env, call=fake)
    overrides = {
        "host_thinking_block": "从结构反差切入，不抢跑事实。",
        "host_expression_block": "短句、自然停顿、避免总结腔。",
    }

    dry = client.post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "render_block_overrides": overrides,
        },
    )
    assert dry.status_code == 200
    assert any(
        "host_thinking_block" in note
        for note in dry.json()["estimate"]["notes"]
    )
    assert fake.calls == []

    submitted = client.post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "render_block_overrides": overrides,
            "confirm": True,
        },
    ).json()
    job = wait_job(client, submitted["job_id"])
    assert job["state"] == "succeeded"
    # One full-material Name Table + one FactContext + two render bodies.
    assert job["progress"]["total"] == 4

    render_calls = [
        call for call in fake.calls if call["operation_route"] == "render_chunk"
    ]
    assert len(render_calls) == 2
    render_calls_by_index = {
        call["work_unit"].metadata["chunk_index"]: call for call in render_calls
    }
    for call in render_calls_by_index.values():
        user_prompt = call["messages"][1]["content"]
        assert all(value in user_prompt for value in overrides.values())
    assert "segment_position: opening" in (
        render_calls_by_index[1]["messages"][1]["content"]
    )
    assert "segment_position: ending" in (
        render_calls_by_index[2]["messages"][1]["content"]
    )
    assert (
        render_calls_by_index[1]["messages"][1]["content"]
        != render_calls_by_index[2]["messages"][1]["content"]
    )
    assert "主角在第一章的雨夜登场。" in render_calls[0]["messages"][1]["content"]
    assert "反派身份在第三章被揭晓。" not in render_calls[0]["messages"][1]["content"]
    assert "反派身份在第三章被揭晓。" in render_calls[1]["messages"][1]["content"]

    derived = RunStore(env["runs_root"] / submitted["run_id"])
    manifest = json.loads(
        (derived.run_dir / "console/manifest.json").read_text("utf-8")
    )
    assert manifest["request"]["render_block_overrides"] == overrides
    assert manifest["effective"]["render_block_overrides"] == overrides
    assert set(manifest["effective"]["routes"]) == {
        "name_table",
        "render_fact_context_dsl",
        "render_chunk",
    }

    first_chunk = derived.read_records(CHUNK_ARTIFACTS)[0]
    assert (
        first_chunk.metadata["chunk_index"],
        first_chunk.metadata["chunk_count"],
        first_chunk.metadata["segment_position"],
    ) == (1, 2, "opening")
    loaded = client.get(
        f"/api/runs/{submitted['run_id']}/chunks/{first_chunk.chunk_id}/blocks"
    ).json()
    assert loaded["block_source"] == "render_block_overrides"
    assert loaded["block_source_run_id"] == submitted["run_id"]
    assert {
        key: loaded["blocks"][key] for key in loaded["portable_block_keys"]
    } == overrides


def test_derive_render_rejects_chunk_local_block_override(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({})
    response = make_client(env, call=fake).post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "render_block_overrides": {
                "annotated_materials_block": "不能把单 chunk 材料复制到全量。"
            },
        },
    )
    assert response.status_code == 400
    assert "annotated_materials_block" in response.json()["detail"]
    assert fake.calls == []


def test_derive_render_explicitly_rejects_style_example_override(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({})
    response = make_client(env, call=fake).post(
        "/api/derive/render",
        json={
            "base_run_id": BASE_RUN,
            "render_block_overrides": {
                "style_examples_block": "单 chunk 选择的示例不能移植到整批。"
            },
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "only allow host_thinking_block, host_expression_block" in detail
    assert "unsupported: style_examples_block" in detail
    assert fake.calls == []


def test_derive_render_confirm_runs_global_context_then_parallel_bodies(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({
        "render_fact_context_dsl": (
            "HINT C002N001 | 主角登场已在更早计划安排。"
        ),
        "render_chunk": "整段重渲的正文。句子完整。",
    })
    client = make_client(env, call=fake)
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN, "run_label": "SwapTest", "confirm": True,
    }).json()
    job = wait_job(client, res["job_id"])
    assert job["state"] == "succeeded"
    assert job["result"]["chunks"] == 2
    assert "swaptest-render-" in res["run_id"]

    derived = RunStore(env["runs_root"] / res["run_id"])
    assert derived.exists(CHUNK_ARTIFACTS)
    assert (derived.run_dir / "assembly/script.md").exists()
    # FactContext runs first, then one complete selected-material Name Table,
    # then both render bodies. The fake seam keeps this entirely offline.
    routes = [call["operation_route"] for call in fake.calls]
    assert routes[0] == "render_fact_context_dsl"
    assert Counter(routes) == Counter(
        {
            "render_fact_context_dsl": 1,
            "name_table": 1,
            "render_chunk": 2,
        }
    )
    [table_call] = [
        call for call in fake.calls if call["operation_route"] == "name_table"
    ]
    assert table_call["work_unit"].kind == "name_table"


def test_override_validation_before_spend(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    fake = FakeCall({"render_chunk": "x"})
    client = make_client(env, call=fake)

    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {
            "routes": {
                "render_chunk": {
                    "transport_max_attempts": 5,
                    "transport_backoff_base_seconds": 2.0,
                    "transport_backoff_max_seconds": 20.0,
                    "transport_jitter_ratio": 0.1,
                }
            }
        },
    })
    assert res.status_code == 200
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {
            "routes": {"render_chunk": {"transport_jitter_ratio": 1.1}}
        },
    })
    assert res.status_code == 422
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {"routes": {"render_chunk": {"user_prompt": "nope/missing.md"}}},
        "confirm": True,
    })
    assert res.status_code == 400
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {"routes": {"ghost_route": {"model": "x"}}},
    })
    assert res.status_code == 400
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {"routes": {"render_chunk": {"provider": "other"}}},
    })
    assert res.status_code == 422
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {"routes": {"render_chunk": {"temperature": 3}}},
    })
    assert res.status_code == 400
    res = client.post("/api/derive/render", json={
        "base_run_id": BASE_RUN,
        "config_overrides": {
            "routes": {"render_fact_context_dsl": {"temperature": 0.2}}
        },
    })
    assert res.status_code == 400
    assert res.json()["detail"] == (
        "routes.render_fact_context_dsl.temperature must be 0"
    )
    assert fake.calls == []
    assert [p.name for p in env["runs_root"].iterdir()] == [BASE_RUN]


# --- jobs ----------------------------------------------------------------------------


def test_job_registry_single_flight_and_cancel() -> None:
    registry = JobRegistry()
    gate = threading.Event()

    def blocked(job) -> dict:
        assert gate.wait(timeout=10)
        job.check_cancelled()
        return {"ok": True}

    job = registry.submit(kind="test", run_id="r1", base_run_id="b", target=blocked)
    with pytest.raises(Conflict):
        registry.submit(kind="test", run_id="r2", base_run_id="b", target=blocked)

    registry.request_cancel(job.job_id)
    gate.set()
    deadline = time.time() + 10
    while registry.get(job.job_id).state == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert registry.get(job.job_id).state == "cancelled"

    # registry is free again after the terminal state
    job2 = registry.submit(kind="test", run_id="r3", base_run_id="b", target=lambda j: {"ok": 1})
    deadline = time.time() + 10
    while registry.get(job2.job_id).state in ("pending", "running") and time.time() < deadline:
        time.sleep(0.02)
    assert registry.get(job2.job_id).state == "succeeded"


def test_reload_endpoint_clears_component_caches(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    client = make_client(env)
    cleared = client.post("/api/reload").json()["cleared"]
    assert "load_rendering_protocol_catalog" in cleared
