"""Independent checks for the real excerpt loaded by the normal Console."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from tracecast.storage import RuntimeConfig, sha256_json
from tracecast.storage import config as storage_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = REPO_ROOT / "examples" / "console-case"
SPEC = importlib.util.spec_from_file_location(
    "prepare_console_case", REPO_ROOT / "tools" / "prepare_console_case.py",
)
assert SPEC is not None and SPEC.loader is not None
prepare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = prepare
SPEC.loader.exec_module(prepare)

RUN_ID = "ff6-opening-excerpt-20260804"
CHUNK_ID = "chunk_ca8d217cd31ae60d"
CALL_ID = "call_07fadc9482cd4672aa74992c6b6cf173"
TEXT_HASH = "b6ddcfeb00f114d7d40741add67ae63a7ee63bac4071626bbcaf5f09a388feff"
REQUEST_HASH = "0fe95b4704b7cfa7796d0d9a1368b7b894cfd8cac816d2b5d3e5430a23e5109d"
# Independently compared with the saved historical records. CI needs only this
# checkout; no old private repository, provider or hand-written replacement text.
ORIGINAL_FILE_HASHES = {
    "ingestion/material_atoms.jsonl":
        "8d965f6e47134ecc02cfe922af21bc771a8edfeee429f13cafb2d7939a490b89",
    "ingestion/source_pieces.jsonl":
        "bf3ef932598907788f4e8937aa50980ad54741f0f72275fe92f9967651212c4b",
    "ingestion/source_snapshots.jsonl":
        "554ddebed6a85d67434b734b23739c4992d43693282d61aa97257d92b0c7c860",
    "packing/render_contracts.jsonl":
        "f66c01963aeff4a289d9442ff723851da85578c5539c9dd433517104a49e23d9",
    "rendering/chunk_artifacts.jsonl":
        "7e3b1e681e4960097bbf143c213f0e18ecd9b216b179a37c9b43eb26d8baa41a",
    f"llm/raw/{CALL_ID}.request.json":
        "f8b08cea946ce74b5df09ae555541cdcce920530431cf069b0ec6ee9979f205a",
    f"llm/raw/{CALL_ID}.response.json":
        "9ec32236011f30925cf87842aef3cb0197c78559d40e4bab67f44ffd3503c17d",
}
EXPECTED_RUN_FILES = {
    *ORIGINAL_FILE_HASHES, "console/manifest.json", "packing/chunk_plan.json",
    "llm/call_records.jsonl",
}


def _digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("browsing the case must not invoke a provider or network")


def test_case_preserves_the_historical_body_and_actual_provider_payloads() -> None:
    actual = _digest(CASE_DIR / "run")
    assert set(actual) == EXPECTED_RUN_FILES
    assert {name: actual[name] for name in ORIGINAL_FILE_HASHES} == ORIGINAL_FILE_HASHES
    case = prepare.load_case()
    assert case.local_run_id == RUN_ID and case.chunk_id == CHUNK_ID and case.call_id == CALL_ID
    [chunk] = _rows(CASE_DIR / "run/rendering/chunk_artifacts.jsonl")
    [record] = _rows(CASE_DIR / "run/llm/call_records.jsonl")
    request = _json(CASE_DIR / f"run/llm/raw/{CALL_ID}.request.json")
    response = _json(CASE_DIR / f"run/llm/raw/{CALL_ID}.response.json")
    assert len(chunk["text"]) == 1572
    assert hashlib.sha256(chunk["text"].encode()).hexdigest() == TEXT_HASH
    assert chunk["output_hash"] == TEXT_HASH == case.hashes["chunk_text"]
    assert record["output_hash"] == f"sha256:{TEXT_HASH}"
    assert response["choices"][0]["message"]["content"] == chunk["text"]
    assert sha256_json(request) == REQUEST_HASH
    assert record["input_hash"] == case.hashes["request_envelope"] == f"sha256:{REQUEST_HASH}"
    assert request["messages"] == request["request_body"]["messages"]
    assert record["call_id"] == chunk["metadata"]["call_id"] == CALL_ID
    assert record["prompt_version"] == chunk["metadata"]["prompt_version"]
    assert record["prompt_version"].startswith("render_chunk@v16+")
    # A chunk input hash describes its projection, not the provider envelope.
    assert chunk["input_hash"] == (
        "bcb6b219bbd4beeec1cab5f4f98d130a24c59be4dc6225e79974dc208126ecbb"
    )


def test_case_contains_only_selected_materials_and_their_piece_dependencies() -> None:
    atoms = _rows(CASE_DIR / "run/ingestion/material_atoms.jsonl")
    pieces = _rows(CASE_DIR / "run/ingestion/source_pieces.jsonl")
    [snapshot] = _rows(CASE_DIR / "run/ingestion/source_snapshots.jsonl")
    [contract] = _rows(CASE_DIR / "run/packing/render_contracts.jsonl")
    [record] = _rows(CASE_DIR / "run/llm/call_records.jsonl")
    request = _json(CASE_DIR / f"run/llm/raw/{CALL_ID}.request.json")
    user = request["messages"][1]["content"]
    assert len(atoms) == 10 and len(pieces) == 13
    assert len(contract["materials"]) == 10 and len(contract["nodes"]) == 5
    by_atom = {atom["id"]: atom for atom in atoms}
    by_piece = {piece["piece_id"]: piece for piece in pieces}
    assert set(by_atom) == {item["source_material_id"] for item in contract["materials"]}
    assert set(by_piece) == {ref for atom in atoms for ref in atom["provenance"]["piece_refs"]}
    for material in contract["materials"]:
        atom = by_atom[material["source_material_id"]]
        assert material["operation"] == "keep"
        assert material["content"] == atom["content"]
        assert atom["content"] in user
        assert atom["provenance"]["source_ref"] == snapshot["source_id"]
        assert "".join(by_piece[ref]["text"] for ref in atom["provenance"]["piece_refs"]) == (
            atom["content"]
        )
    assert set(record["work_unit"]["input_refs"]) == {
        *by_atom, *(node["source_node_id"] for node in contract["nodes"]),
    }
    assert snapshot["url"] is None  # Later attribution does not rewrite history.
    assert not (CASE_DIR / "run" / snapshot["path"]).exists()
    plan = _json(CASE_DIR / "run/packing/chunk_plan.json")
    assert len(plan["chunks"]) == 1
    assert plan["chunks"][0]["chunk_id"] == CHUNK_ID
    assert plan["chunks"][0]["material_ids"] == list(by_atom)
    assert plan["chunks"][0]["node_ids"] == [
        node["source_node_id"] for node in contract["nodes"]
    ]


def test_case_declares_excerpt_and_removes_cost_and_key_labels() -> None:
    case = prepare.load_case()
    assert case.scope == "historical_excerpt"
    assert case.chunk_index == 1 and case.original_chunk_count == 50
    assert case.complete_run_included is False and case.full_sources_included is False
    assert case.saved_request_authoritative is True
    manifest = _json(CASE_DIR / "run/console/manifest.json")
    assert manifest["kind"] == manifest["status"] == "historical_excerpt"
    assert manifest["base_run_id"] is None and manifest["job_id"] is None
    assert manifest["excerpt"]["annotation_only"] is True
    assert "1/50" in manifest["error"]
    plan = _json(CASE_DIR / "run/packing/chunk_plan.json")
    assert plan["metadata"]["complete_plan_included"] is False
    assert plan["metadata"]["original_chunk_count"] == 50
    assert not (CASE_DIR / "run/assembly").exists()
    assert not (CASE_DIR / "run/validation").exists()
    [record] = _rows(CASE_DIR / "run/llm/call_records.jsonl")
    assert all(field not in record for field in ("cost", "cost_kind", "key_id"))
    assert not any(key.startswith("cost") for key in record["metadata"])
    # This success is the historical provider response, not an assembled-run status.
    assert record["provider_status"] == "success"
    assert "LlmCallRecord.cost" in case.redactions
    assert "LlmCallRecord.key_id" in case.redactions


def test_prepare_creates_an_isolated_reusable_workspace_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "create_connection", _unexpected)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _unexpected)
    monkeypatch.setattr(storage_config, "load_dotenv_file", _unexpected)
    before = _digest(CASE_DIR)
    first, second = tmp_path / "first", tmp_path / "second"
    prepare.prepare_workspace(first)
    prepare.prepare_workspace(second)
    assert _digest(first) == _digest(second)
    assert _digest(CASE_DIR) == before
    assert _digest(first / "runs" / RUN_ID) == _digest(CASE_DIR / "run")
    assert list((first / "projects").iterdir()) == list((first / "hosts").iterdir()) == []
    assert not list(first.rglob(".env*"))
    config = RuntimeConfig.load(first / "configs/runtime.json", load_env=False)
    assert config.providers == {} and config.routes == {}
    snapshot = _digest(first)
    with pytest.raises(FileExistsError):
        prepare.prepare_workspace(first)
    assert _digest(first) == snapshot


@pytest.mark.parametrize("change", ["content", "extra", "duplicate", "traversal", "missing"])
def test_bad_case_is_rejected_before_creating_output(tmp_path: Path, change: str) -> None:
    source = tmp_path / "case"
    shutil.copytree(CASE_DIR, source)
    case = _json(source / "case.json")
    if change == "content":
        (source / f"run/llm/raw/{CALL_ID}.response.json").write_text("{}", encoding="utf-8")
    elif change == "extra":
        (source / "run/full-source.txt").write_text("not approved", encoding="utf-8")
    elif change == "missing":
        (source / "run/packing/chunk_plan.json").unlink()
    else:
        if change == "duplicate":
            case["files"].append(case["files"][0])
        else:
            case["files"][0]["path"] = "../outside.json"
        (source / "case.json").write_text(json.dumps(case), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises((OSError, ValueError)):
        prepare.prepare_workspace(output, case_root=source)
    assert not output.exists()


def test_case_directory_link_cannot_read_an_external_tree(tmp_path: Path) -> None:
    source = tmp_path / "case"
    shutil.copytree(CASE_DIR, source)
    outside = tmp_path / "outside"
    (source / "run").rename(outside)
    try:
        (source / "run").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("directory symlinks are unavailable on this host")
        # Junctions exercise the same ancestor escape on Windows without
        # requiring the privilege needed to create a symbolic link.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(source / "run"), str(outside)],
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
    with pytest.raises(ValueError, match="escapes"):
        prepare.prepare_workspace(tmp_path / "output", case_root=source)
    assert not (tmp_path / "output").exists()


def test_prepare_cli_works_from_a_clean_directory(tmp_path: Path) -> None:
    output = tmp_path / "workspace with spaces"
    command = [
        sys.executable, str(REPO_ROOT / "tools/prepare_console_case.py"),
        "--output", str(output), "--port", "8770",
    ]
    env = {
        name: value for name, value in os.environ.items()
        if not name.endswith(("API_KEY", "API_TOKEN"))
    }
    env["PYTHONUTF8"] = "1"
    # Match pytest's source import path without relying on a globally installed package.
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "127.0.0.1:8770" in result.stdout
    assert f"/#/run/{RUN_ID}/diagnostics/trace" in result.stdout
    assert "--config" in result.stdout and str(output) in result.stdout
    assert not (output / ".env").exists()
    before = _digest(output)
    repeated = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    assert repeated.returncode == 1
    assert _digest(output) == before


@pytest.fixture
def case_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from tracecast.console import create_app

    workspace = tmp_path / "workspace"
    prepare.prepare_workspace(workspace)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(socket, "create_connection", _unexpected)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _unexpected)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _unexpected)
    real_load = storage_config.load_dotenv_file

    def isolated_env(path: Path, **kwargs: Any) -> None:
        assert Path(path).resolve() == workspace / ".env"
        assert not Path(path).exists()
        real_load(path, **kwargs)

    monkeypatch.setattr(storage_config, "load_dotenv_file", isolated_env)
    for name in tuple(os.environ):
        if name.endswith(("API_KEY", "API_TOKEN")):
            monkeypatch.delenv(name, raising=False)
    app = create_app(
        runs_root=workspace / "runs", projects_dir=workspace / "projects",
        hosts_dir=workspace / "hosts", config_path=workspace / "configs/runtime.json",
        profiles_path=workspace / "configs/source_profile_templates.json", call=_unexpected,
    )
    with TestClient(app) as client:
        yield client, workspace


def test_normal_console_browses_case_without_network_credentials_or_writes(case_client) -> None:
    client, workspace = case_client
    before = _digest(workspace)
    for path in (
        "/", "/static/index.html", "/static/js/main.js", "/api/setup", "/api/projects",
        "/api/runs", f"/api/runs/{RUN_ID}", f"/api/runs/{RUN_ID}/chunks",
        f"/api/runs/{RUN_ID}/trace?chunk_id={CHUNK_ID}",
        f"/api/runs/{RUN_ID}/trace/rendering/{CHUNK_ID}",
        f"/api/runs/{RUN_ID}/trace/packing/{CHUNK_ID}",
        f"/api/runs/{RUN_ID}/llm/calls",
        f"/api/runs/{RUN_ID}/llm/calls/{CALL_ID}/raw?role=request",
        f"/api/runs/{RUN_ID}/llm/calls/{CALL_ID}/raw?role=response",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
    setup = client.get("/api/setup").json()
    assert setup["runtime"]["configured"] is True
    assert setup["api_key_configured"] is False
    assert "demo_mode" not in setup
    assert client.get("/api/projects").json() == []
    inventory = client.get(f"/api/runs/{RUN_ID}").json()
    assert inventory["console_manifest"]["status"] == "historical_excerpt"
    slots = {item["relpath"]: item for item in inventory["slots"]}
    assert slots["assembly/script.md"]["available"] is False
    assert slots["assembly/assembly_report.json"]["available"] is False
    rendered = client.get(f"/api/runs/{RUN_ID}/trace/rendering/{CHUNK_ID}").json()
    assert rendered["technical"]["recorded_request_matches_hash"] is True
    assert rendered["technical"]["prompt_template_drift"] is True
    assert rendered["technical"]["matches_recorded_request"] is False
    original_request = _json(CASE_DIR / f"run/llm/raw/{CALL_ID}.request.json")
    assert rendered["input_blocks"]["user_message_1"] == original_request["messages"][1]["content"]
    assert hashlib.sha256(rendered["compiled_output"]["text"].encode()).hexdigest() == TEXT_HASH
    packed = client.get(f"/api/runs/{RUN_ID}/trace/packing/{CHUNK_ID}").json()
    assert packed["compiled_output"]["material_count"] == 10
    assert packed["compiled_output"]["node_count"] == 5
    atoms = _rows(CASE_DIR / "run/ingestion/material_atoms.jsonl")
    assert all(atom["content"] in packed["input_blocks"]["materials"] for atom in atoms)
    [record] = client.get(f"/api/runs/{RUN_ID}/llm/calls").json()
    assert "cost" not in record and "key_id" not in record
    assert _digest(workspace) == before


def test_excerpt_cannot_be_rerun_as_a_complete_project(case_client) -> None:
    client, workspace = case_client
    before = _digest(workspace)
    for endpoint in ("/api/derive/selection", "/api/derive/node-edit", "/api/derive/render"):
        response = client.post(endpoint, json={"base_run_id": RUN_ID, "confirm": True})
        assert 400 <= response.status_code < 500, (endpoint, response.text)
    assert _digest(workspace) == before
    assert client.get("/api/jobs").json() == []


def test_case_reads_reject_unknown_run_and_outside_artifact(case_client) -> None:
    client, _workspace = case_client
    assert client.get("/api/runs/not-in-this-workspace").status_code == 404
    response = client.get(
        f"/api/runs/{RUN_ID}/artifact", params={"relpath": "../../../.env"},
    )
    assert response.status_code == 404
    assert client.get(f"/api/runs/{RUN_ID}/llm/calls/{CALL_ID}/raw?role=other").status_code == 400
