"""Independent spec-conformance tests for ``tracecast.storage``.

Assertions cover JSON/JSONL IO, hashing, typed run-artifact slots, stable-id
persistence, and the provider-secret boundary. Secret values are resolved lazily,
masked in ``SecretStr``, and never become config model fields.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tracecast.domain import (
    CandidateGraph,
    CandidateNode,
    ContentKind,
    LlmCallRecord,
    MaterialAtom,
    Provenance,
    ProviderStatus,
    TrustTier,
    atom_id,
    candidate_id,
    graph_id,
    new_call_id,
    piece_id,
    window_id,
)
from tracecast.storage import (
    ARTIFACT_SPECS,
    ASSEMBLY_REPORT,
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    LLM_CALL_RECORDS,
    MATERIAL_ATOMS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    SCRIPT,
    SELECTION_PLAN,
    SOURCE_PIECES,
    ArtifactFormat,
    RunStore,
    RuntimeConfig,
    canonical_json,
    content_hash,
    read_json,
    read_json_model,
    read_json_models,
    read_jsonl_models,
    sha256_bytes,
    sha256_json,
    sha256_text,
    write_json,
    write_json_model,
    write_jsonl,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_CONFIG = REPO_ROOT / "configs" / "runtime.example.json"

# A unique fake secret that must never escape the security boundary.
SENTINEL_SECRET = "sk-tracecast-SENTINEL-must-not-leak-7b3f9c"


# --- builders (valid instances built via the domain id helpers) -------------


def make_provenance(refs: list[str]) -> Provenance:
    return Provenance(
        source_ref="src-1",
        source_type="script_transcript",
        source_class="script_transcript",
        source_content_hash=content_hash("原始来源全文"),
        piece_refs=refs,
    )


def make_atom(content: str = "中文证据原文", order: int = 0) -> MaterialAtom:
    refs = [piece_id("src-1", order)]
    return MaterialAtom(
        id=atom_id("src-1", refs),
        content=content,
        language="zh",
        content_kind=ContentKind.assertion,
        source_order=order,
        trust_tier=TrustTier.canonical,
        provenance=make_provenance(refs),
    )


def make_call_record(stage: str = "rendering") -> LlmCallRecord:
    return LlmCallRecord(
        call_id=new_call_id(),
        stage=stage,
        operation_route="render_chunk",
        provider="gateway",
        model="your-standard-model",
        input_hash=content_hash({"prompt": "渲染中文提示"}),
        provider_status=ProviderStatus.success,
        created_at=datetime(2026, 6, 29, 12, 0, tzinfo=UTC),
    )


def make_graph(project_id: str = "proj-1") -> CandidateGraph:
    atom = make_atom()
    node = CandidateNode(
        candidate_id=candidate_id(project_id, [atom.id]),
        source_window=window_id("src-1", [piece_id("src-1", 0)]),
        summary="候选节点中文摘要",
        material_ids=[atom.id],
    )
    return CandidateGraph(
        graph_id=graph_id(project_id, [node.candidate_id]),
        project_id=project_id,
        nodes=[node],
    )


# === 1. JSON / JSONL Chinese is stored human-readable (ensure_ascii=False) ===


def test_write_json_keeps_chinese_human_readable(tmp_path: Path) -> None:
    path = tmp_path / "obj.json"
    write_json(path, {"标题": "中文标题", "body": "证据内容"})
    raw = path.read_text(encoding="utf-8")
    assert "中文标题" in raw
    assert "证据内容" in raw
    assert "\\u" not in raw  # no \uXXXX escapes
    assert read_json(path) == {"标题": "中文标题", "body": "证据内容"}


def test_write_jsonl_keeps_chinese_human_readable(tmp_path: Path) -> None:
    path = tmp_path / "atoms.jsonl"
    write_jsonl(path, [make_atom(content="第一条中文证据")])
    raw = path.read_text(encoding="utf-8")
    assert "第一条中文证据" in raw
    assert "\\u" not in raw


def test_json_model_file_keeps_chinese_human_readable(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    write_json_model(path, make_graph())
    raw = path.read_text(encoding="utf-8")
    assert "候选节点中文摘要" in raw
    assert "\\u" not in raw


# === 2. Real domain models round-trip to an equal model =====================


def test_json_model_roundtrip_equal(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    graph = make_graph()
    write_json_model(path, graph)
    assert read_json_model(path, CandidateGraph) == graph


def test_jsonl_ledger_roundtrip_equal_and_ordered(tmp_path: Path) -> None:
    path = tmp_path / "atoms.jsonl"
    atoms = [
        make_atom(content="第一", order=0),
        make_atom(content="第二", order=1),
        make_atom(content="第三", order=2),
    ]
    write_jsonl(path, atoms)
    assert read_jsonl_models(path, MaterialAtom) == atoms  # equal AND order-preserving


def test_read_json_models_rejects_non_array(tmp_path: Path) -> None:
    # Type discipline: a single object is not a model array -> no silent misuse.
    path = tmp_path / "single.json"
    write_json_model(path, make_atom())
    with pytest.raises(ValueError):
        read_json_models(path, MaterialAtom)


# === 3. Hashing: deterministic canonicalization + stable content hashes ======


def test_sha256_text_matches_hashlib() -> None:
    text = "tracecast-中文-payload"
    assert sha256_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_sha256_bytes_matches_hashlib() -> None:
    data = "中文".encode()
    assert sha256_bytes(data) == hashlib.sha256(data).hexdigest()


def test_canonical_json_is_key_order_independent() -> None:
    a = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    b = {"a": 2, "nested": {"x": 2, "y": 1}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert sha256_json(a) == sha256_json(b)


def test_content_hash_is_stable_and_changes_with_content() -> None:
    assert content_hash("中文A") == content_hash("中文A")  # stable across calls
    assert content_hash("中文A") != content_hash("中文B")  # changes with content


def test_content_hash_dispatch_matches_primitives() -> None:
    assert content_hash(b"raw-bytes") == sha256_bytes(b"raw-bytes")
    assert content_hash("中文串") == sha256_text("中文串")
    atom = make_atom()
    assert content_hash(atom) == sha256_json(atom)  # models hash their canonical JSON


# === 4. RunStore layout + write/read round-trip + single-use reservation =====


# Each registered artifact slot must be addressed under its owning stage directory.
OWNER_STAGE = {
    SOURCE_PIECES: "ingestion",
    CANDIDATE_GRAPH: "planning",
    SELECTION_PLAN: "selection",
    NODE_EDIT_PLAN: "node_edit",
    CHUNK_PLAN: "packing",
    RENDER_CONTRACTS: "packing",
    NAME_RESOLUTION_PLAN: "name_resolution",
    NAME_RESOLUTION_REPORT: "name_resolution",
    CHUNK_ARTIFACTS: "rendering",
    AUDIT_REPORT: "validation",
    ASSEMBLY_REPORT: "assembly",
    LLM_CALL_RECORDS: "llm",
    SCRIPT: "assembly",
}

# Single artifact -> one JSON model; collection / append-only ledger -> JSONL.
EXPECTED_FORMAT = {
    CANDIDATE_GRAPH: ArtifactFormat.json_model,
    ASSEMBLY_REPORT: ArtifactFormat.json_model,
    MATERIAL_ATOMS: ArtifactFormat.jsonl,
    NAME_RESOLUTION_PLAN: ArtifactFormat.json_model,
    NAME_RESOLUTION_REPORT: ArtifactFormat.jsonl,
    LLM_CALL_RECORDS: ArtifactFormat.jsonl,
}


def test_run_store_layout_is_runs_run_id_stage_file(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-42")
    assert store.run_dir == tmp_path / "runs" / "run-42"
    assert store.run_id == "run-42"
    for spec, stage in OWNER_STAGE.items():
        assert spec.stage == stage
        assert spec.relpath == f"{spec.stage}/{spec.filename}"
        assert store.path_for(spec.relpath) == store.run_dir / spec.stage / spec.filename
    for spec, fmt in EXPECTED_FORMAT.items():
        assert spec.format is fmt
    # Every registered slot resolves inside the run dir.
    for spec in ARTIFACT_SPECS:
        assert store.path_for(spec.relpath).is_relative_to(store.run_dir)


def test_run_store_json_model_roundtrip(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    graph = make_graph()
    assert store.exists(CANDIDATE_GRAPH) is False  # absent before the first write
    store.write_model(CANDIDATE_GRAPH, graph)
    assert store.exists(CANDIDATE_GRAPH) is True
    assert store.read_model(CANDIDATE_GRAPH) == graph


def test_create_for_run_reserves_a_single_use_run_id(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    store = RunStore.create_for_run(runs_root, "run-1")

    assert store.run_dir.is_dir()
    with pytest.raises(FileExistsError):
        RunStore.create_for_run(runs_root, "run-1")


def test_run_store_jsonl_append_preserves_order(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    records = [make_call_record(), make_call_record(), make_call_record()]
    for record in records:
        store.append_record(LLM_CALL_RECORDS, record)
    read_back = store.read_records(LLM_CALL_RECORDS)
    assert read_back == records
    assert [r.call_id for r in read_back] == [r.call_id for r in records]


def test_run_store_missing_artifact_raises(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    assert store.exists(MATERIAL_ATOMS) is False
    with pytest.raises(FileNotFoundError):
        store.read_records(MATERIAL_ATOMS)
    with pytest.raises(FileNotFoundError):
        store.read_text(SCRIPT)


def test_run_store_raw_payload_roundtrip_under_llm_dir(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    call_id = "call_abc123"
    req_path = store.write_raw_payload(call_id, "request", {"messages": ["你好"]})
    resp_path = store.write_raw_payload(call_id, "response", "模型输出正文")
    assert req_path.startswith("llm/")
    assert call_id in req_path and "request" in req_path
    assert resp_path.startswith("llm/")
    assert store.read_raw_payload(call_id, "request") == {"messages": ["你好"]}
    assert store.read_raw_payload(call_id, "response") == "模型输出正文"
    # raw JSON payload also stays human-readable.
    raw = (store.run_dir / req_path).read_text(encoding="utf-8")
    assert "你好" in raw and "\\u" not in raw


# === 5. RunStore safety: path escape + format/type mismatch must RAISE =======


@pytest.mark.parametrize("bad_run_id", ["..", "a/b", "/abs", ".", "nested\\seg"])
def test_run_id_path_escape_raises(tmp_path: Path, bad_run_id: str) -> None:
    with pytest.raises(ValueError):
        RunStore.for_run(tmp_path / "runs", bad_run_id)


@pytest.mark.parametrize("bad_rel", ["../escape", "/etc/passwd", "a/../../b"])
def test_path_for_escape_raises(tmp_path: Path, bad_rel: str) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    with pytest.raises(ValueError):
        store.path_for(bad_rel)


def test_raw_payload_rejects_bad_role_and_unsafe_call_id(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    with pytest.raises(ValueError):
        store.write_raw_payload("call_x", "banana", {"a": 1})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.write_raw_payload("..", "request", {"a": 1})


def test_run_store_format_mismatch_raises(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    store.write_model(CANDIDATE_GRAPH, make_graph())
    # reading a json_model slot as a jsonl ledger (and vice versa) must fail.
    with pytest.raises(ValueError):
        store.read_records(CANDIDATE_GRAPH)
    with pytest.raises(ValueError):
        store.write_records(CANDIDATE_GRAPH, [make_graph()])
    with pytest.raises(ValueError):
        store.read_model(MATERIAL_ATOMS)
    with pytest.raises(ValueError):
        store.write_model(MATERIAL_ATOMS, make_atom())


def test_run_store_wrong_model_type_raises(tmp_path: Path) -> None:
    store = RunStore.for_run(tmp_path / "runs", "run-1")
    with pytest.raises(TypeError):
        store.write_model(CANDIDATE_GRAPH, make_atom())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.write_records(MATERIAL_ATOMS, [make_call_record()])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        store.append_record(MATERIAL_ATOMS, make_call_record())  # type: ignore[arg-type]


# === 6. SECURITY: provider secrets never serialize; missing var must raise ===


def test_provider_secret_never_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    # Put the sentinel only in the process env; load config WITHOUT the real .env.
    monkeypatch.setenv("TRACECAST_LLM_API_KEY", SENTINEL_SECRET)
    cfg = RuntimeConfig.load(RUNTIME_CONFIG, load_env=False)
    key = cfg.provider("gateway").key("default")

    secret = key.resolve_secret()  # resolved lazily from os.environ
    assert secret.get_secret_value() == SENTINEL_SECRET  # reveal works at the boundary

    # The secret value is never a model field -> it cannot appear in any dump/repr,
    # and SecretStr masks itself in str()/repr().
    leak_surfaces = [
        cfg.model_dump_json(),
        repr(cfg),
        cfg.provider("gateway").model_dump_json(),
        key.model_dump_json(),
        repr(key),
        str(secret),
        repr(secret),
    ]
    for surface in leak_surfaces:
        assert SENTINEL_SECRET not in surface
    # Only the env-var NAME is persisted, never the value.
    assert "TRACECAST_LLM_API_KEY" in key.model_dump_json()


def test_missing_or_empty_secret_raises_never_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACECAST_LLM_API_KEY", raising=False)
    cfg = RuntimeConfig.load(RUNTIME_CONFIG, load_env=False)
    key = cfg.provider("gateway").key("default")
    with pytest.raises(KeyError):
        key.resolve_secret(env={})  # missing -> raises, no silent empty secret
    with pytest.raises(ValueError):
        key.resolve_secret(env={key.api_key_env: ""})  # empty -> raises


def test_dotenv_preserves_literal_dollar_brace_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opaque = "sk-test-${SHOULD_NOT_EXPAND}-tail"
    env_file = tmp_path / ".env"
    env_file.write_text(f"TRACECAST_LLM_API_KEY='{opaque}'\n", encoding="utf-8")
    monkeypatch.delenv("TRACECAST_LLM_API_KEY", raising=False)
    monkeypatch.setenv("SHOULD_NOT_EXPAND", "corrupted")

    cfg = RuntimeConfig.load(
        RUNTIME_CONFIG,
        load_env=True,
        env_file=env_file,
    )

    assert cfg.provider("gateway").key("default").resolve_secret().get_secret_value() == opaque


# === 7. Config parses runtime.json faithfully ===============================


def test_runtime_config_parses_faithfully() -> None:
    cfg = RuntimeConfig.load(RUNTIME_CONFIG, load_env=False)
    route = cfg.route("selector_path_dsl")
    assert route.model == "your-selection-model"
    assert route.thinking is None
    assert route.transport_max_attempts == 3
    assert route.transport_backoff_base_seconds == 1.0
    assert route.transport_backoff_max_seconds == 30.0
    assert route.transport_jitter_ratio == 0.25
    assert cfg.stage_budgets["rendering"]["target_material_tokens"] == 3000
    assert cfg.stage_budgets["rendering"]["max_material_tokens"] == 4500
    provider = cfg.provider("gateway")
    assert provider.base_url == "https://gateway.example.com/v1"
    assert provider.chat_completions_path == "/chat/completions"
    assert provider.max_output_tokens_field == "max_tokens"
    assert provider.key("default").api_key_env == "TRACECAST_LLM_API_KEY"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("transport_max_attempts", 0),
        ("transport_backoff_base_seconds", 0),
        ("transport_backoff_max_seconds", 0),
        ("transport_jitter_ratio", -0.01),
        ("transport_jitter_ratio", 1.01),
    ],
)
def test_runtime_config_rejects_invalid_transport_policy(
    field: str,
    value: int | float,
) -> None:
    cfg = RuntimeConfig.load(RUNTIME_CONFIG, load_env=False)
    data = cfg.model_dump()
    data["routes"]["render_chunk"][field] = value

    with pytest.raises(ValueError):
        RuntimeConfig.model_validate(data)


def test_runtime_config_rejects_transport_backoff_base_above_cap() -> None:
    cfg = RuntimeConfig.load(RUNTIME_CONFIG, load_env=False)
    data = cfg.model_dump()
    data["routes"]["render_chunk"].update(
        {
            "transport_backoff_base_seconds": 31.0,
            "transport_backoff_max_seconds": 30.0,
        }
    )

    with pytest.raises(
        ValueError,
        match="transport_backoff_base_seconds must be <= transport_backoff_max_seconds",
    ):
        RuntimeConfig.model_validate(data)
