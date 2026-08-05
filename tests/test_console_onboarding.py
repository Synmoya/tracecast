"""Independent offline regression for first-run Console onboarding."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tracecast.console.app import create_app  # noqa: E402
from tracecast.console.setup import authorize_setup_write  # noqa: E402
from tracecast.domain import LlmCallRecord, ProviderStatus  # noqa: E402
from tracecast.storage import (  # noqa: E402
    LLM_CALL_RECORDS,
    RunStore,
    content_hash,
    sha256_json,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = REPO_ROOT / "configs" / "source_profile_templates.example.json"
SENTINEL = "sk-onboarding-${LITERAL}-quote'\\tail"


def _client(tmp_path: Path, *, call: Any | None = None) -> tuple[TestClient, dict[str, Path]]:
    paths = {
        "runs": tmp_path / "runs",
        "projects": tmp_path / "projects",
        "hosts": tmp_path / "hosts",
        "config": tmp_path / "configs" / "runtime.json",
        "env": tmp_path / ".env",
    }
    app = create_app(
        runs_root=paths["runs"],
        projects_dir=paths["projects"],
        hosts_dir=paths["hosts"],
        config_path=paths["config"],
        profiles_path=PROFILES,
        env_path=paths["env"],
        call=call if call is not None else _UnexpectedCall(),
    )
    return TestClient(app), paths


class _UnexpectedCall:
    def __call__(self, **_kwargs: Any) -> tuple[str, LlmCallRecord]:
        raise AssertionError("this test path must make zero model calls")


class _IntakeCall:
    def __init__(self, response: str | None = None, *, fail: bool = False) -> None:
        self.response = response or (
            "SRC S001 | profile=reference_article | language=en | status=ready\n"
            "TITLE Reviewed source\n"
            "NOTE Ready for operator review.\n"
            "REASON Reference-style prose.\n"
            "ENDSRC"
        )
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[str, LlmCallRecord]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("simulated Intake failure")
        store: RunStore = kwargs["store"]
        call_id = f"intake-fake-{len(self.calls)}"
        request = {"messages": kwargs["messages"]}
        request_path = store.write_raw_payload(call_id, "request", request)
        response_path = store.write_raw_payload(call_id, "response", self.response)
        route = kwargs["config"].route(kwargs["operation_route"])
        record = LlmCallRecord(
            call_id=call_id,
            stage=route.stage,
            operation_route=kwargs["operation_route"],
            provider="offline-test",
            model=route.model,
            input_hash=f"sha256:{sha256_json(request)}",
            raw_request_path=request_path,
            raw_response_path=response_path,
            provider_status=ProviderStatus.success,
            prompt_version=kwargs.get("prompt_version"),
            created_at="2026-08-03T00:00:00Z",
            work_unit=kwargs.get("work_unit"),
        )
        store.append_record(LLM_CALL_RECORDS, record)
        return self.response, record


def _runtime_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "gateway_url": "https://gateway.example.test/v1",
        "api_key": SENTINEL,
        "standard_model": "standard-model",
        "selection_model": "selection-model",
        "completion_path": "/chat/completions",
        "max_output_tokens_field": "max_completion_tokens",
        "extra_body": {"reasoning_effort": "medium"},
        "confirm": True,
    }
    payload.update(overrides)
    return payload


def _setup(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(**overrides),
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload(
    name: str,
    raw: bytes,
    *,
    encoding: str | None = None,
    profile: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "data_base64": base64.b64encode(raw).decode("ascii"),
    }
    if encoding is not None:
        result["encoding"] = encoding
    if profile is not None:
        result["profile"] = profile
    if language is not None:
        result["language"] = language
    return result


def _draft_payload(
    project_id: str,
    files: list[dict[str, Any]],
    *,
    mode: str = "manual",
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "output_language": "en-US",
        "name_resolution_mode": "overrides_only",
        "mode": mode,
        "files": files,
    }


def _wait_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["state"] in {"succeeded", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_missing_runtime_still_serves_console_and_structured_setup_required(
    tmp_path: Path,
) -> None:
    client, _paths = _client(tmp_path)

    assert client.get("/").status_code == 200
    status = client.get("/api/setup")
    assert status.status_code == 200
    assert status.json()["runtime"] == {
        "state": "missing",
        "configured": False,
        "path": str(tmp_path / "configs" / "runtime.json"),
        "error": None,
    }
    assert status.json()["api_key_configured"] is False
    assert "starter_hosts" not in status.json()
    builtin_hosts = client.get("/api/hosts").json()
    assert [row["host_id"] for row in builtin_hosts] == [
        "starter-zh-cn", "starter-en-us",
    ]
    assert all(row["read_only"] for row in builtin_hosts)

    blocked = client.get("/api/config")
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "setup_required"
    assert SENTINEL not in blocked.text


def test_runtime_setup_requires_confirmation_same_origin_and_loopback() -> None:
    with pytest.raises(Exception, match="loopback"):
        authorize_setup_write(
            client_host="192.0.2.5",
            origin="http://127.0.0.1:8765",
            request_url="http://127.0.0.1:8765/api/setup/runtime",
        )
    with pytest.raises(Exception, match="origin"):
        authorize_setup_write(
            client_host="127.0.0.1",
            origin="http://evil.example",
            request_url="http://127.0.0.1:8765/api/setup/runtime",
        )
    with pytest.raises(Exception, match="origin"):
        authorize_setup_write(
            client_host="127.0.0.1",
            origin=None,
            request_url="http://127.0.0.1:8765/api/setup/runtime",
        )


def test_runtime_setup_is_secret_safe_preserves_env_and_maps_only_selection_routes(
    tmp_path: Path,
) -> None:
    client, paths = _client(tmp_path)
    paths["env"].write_bytes(b"OTHER=value\r\n# keep me\r\n")

    no_confirm = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(confirm=False),
        headers={"Origin": "http://testserver"},
    )
    assert no_confirm.status_code == 400
    assert not paths["config"].exists()
    assert paths["env"].read_bytes() == b"OTHER=value\r\n# keep me\r\n"

    cross_origin = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(),
        headers={"Origin": "https://cross-origin.example"},
    )
    assert cross_origin.status_code == 400
    assert not paths["config"].exists()

    response = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(),
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    assert response.json()["update_mode"] == "created"
    assert SENTINEL not in response.text
    runtime_text = paths["config"].read_text(encoding="utf-8")
    assert SENTINEL not in runtime_text
    assert "TRACECAST_LLM_API_KEY" in runtime_text
    env_bytes = paths["env"].read_bytes()
    assert env_bytes.startswith(b"OTHER=value\r\n# keep me\r\n")
    from dotenv import dotenv_values

    assert dotenv_values(paths["env"], interpolate=False)["TRACECAST_LLM_API_KEY"] == SENTINEL

    runtime = json.loads(runtime_text)
    assert "name_table" in runtime["routes"]
    assert runtime["providers"]["gateway"]["max_output_tokens_field"] == (
        "max_completion_tokens"
    )
    assert runtime["providers"]["gateway"]["chat_completions_path"] == (
        "/chat/completions"
    )
    selection = {"selector_path_dsl", "selector_directive_dsl"}
    assert all(
        route["model"] == ("selection-model" if name in selection else "standard-model")
        for name, route in runtime["routes"].items()
    )
    assert all(
        route["extra_body"] == {"reasoning_effort": "medium"}
        for route in runtime["routes"].values()
    )
    assert all(
        {
            "transport_max_attempts": route["transport_max_attempts"],
            "transport_backoff_base_seconds": route[
                "transport_backoff_base_seconds"
            ],
            "transport_backoff_max_seconds": route[
                "transport_backoff_max_seconds"
            ],
            "transport_jitter_ratio": route["transport_jitter_ratio"],
        }
        == {
            "transport_max_attempts": 3,
            "transport_backoff_base_seconds": 1.0,
            "transport_backoff_max_seconds": 30.0,
            "transport_jitter_ratio": 0.25,
        }
        for route in runtime["routes"].values()
    )
    config_response = client.get("/api/config")
    assert config_response.status_code == 200
    assert SENTINEL not in config_response.text
    assert "key_ids" not in config_response.text


def test_quick_setup_updates_single_provider_without_rebuilding_advanced_runtime(
    tmp_path: Path,
) -> None:
    client, paths = _client(tmp_path)
    _setup(client)
    runtime = json.loads(paths["config"].read_text(encoding="utf-8"))
    runtime["stage_budgets"]["planning"]["max_final_node_count"] = 73
    runtime["routes"]["render_chunk"]["timeout_seconds"] = 321
    runtime["routes"]["render_chunk"]["extra_body"] = {"preserve_me": True}
    runtime["routes"]["render_chunk"].update(
        {
            "transport_max_attempts": 5,
            "transport_backoff_base_seconds": 2.0,
            "transport_backoff_max_seconds": 19.0,
            "transport_jitter_ratio": 0.1,
        }
    )
    runtime["operator_note"] = "keep advanced fields"
    paths["config"].write_text(json.dumps(runtime), encoding="utf-8")

    response = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(
            gateway_url="https://new-gateway.example/v1",
            api_key="replacement-key",
            standard_model="new-standard",
            selection_model="new-selection",
            extra_body=None,
        ),
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["update_mode"] == "updated"
    updated = json.loads(paths["config"].read_text(encoding="utf-8"))
    assert updated["operator_note"] == "keep advanced fields"
    assert updated["stage_budgets"]["planning"]["max_final_node_count"] == 73
    assert updated["routes"]["render_chunk"]["timeout_seconds"] == 321
    assert updated["routes"]["render_chunk"]["extra_body"] == {"preserve_me": True}
    assert {
        key: updated["routes"]["render_chunk"][key]
        for key in (
            "transport_max_attempts",
            "transport_backoff_base_seconds",
            "transport_backoff_max_seconds",
            "transport_jitter_ratio",
        )
    } == {
        "transport_max_attempts": 5,
        "transport_backoff_base_seconds": 2.0,
        "transport_backoff_max_seconds": 19.0,
        "transport_jitter_ratio": 0.1,
    }
    assert updated["providers"]["gateway"]["base_url"] == (
        "https://new-gateway.example/v1"
    )
    assert updated["providers"]["gateway"]["keys"] == [
        {"key_id": "quick_default", "api_key_env": "TRACECAST_LLM_API_KEY"}
    ]
    selection_routes = {"selector_path_dsl", "selector_directive_dsl"}
    assert all(
        route["model"] == ("new-selection" if name in selection_routes else "new-standard")
        for name, route in updated["routes"].items()
    )


def test_quick_setup_refuses_multi_provider_runtime_without_writes(tmp_path: Path) -> None:
    client, paths = _client(tmp_path)
    _setup(client)
    runtime = json.loads(paths["config"].read_text(encoding="utf-8"))
    runtime["providers"]["backup"] = dict(runtime["providers"]["gateway"])
    paths["config"].write_text(json.dumps(runtime), encoding="utf-8")
    before_runtime = paths["config"].read_bytes()
    before_env = paths["env"].read_bytes()

    response = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(api_key="must-not-be-written"),
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 409
    assert paths["config"].read_bytes() == before_runtime
    assert paths["env"].read_bytes() == before_env
    assert b"must-not-be-written" not in before_runtime + before_env


def test_invalid_runtime_update_does_not_modify_existing_env_or_runtime(
    tmp_path: Path,
) -> None:
    client, paths = _client(tmp_path)
    _setup(client)
    before_env = paths["env"].read_bytes()
    before_runtime = paths["config"].read_bytes()

    rejected = client.put(
        "/api/setup/runtime",
        json=_runtime_payload(api_key="replacement", extra_body={"messages": []}),
        headers={"Origin": "http://testserver"},
    )

    assert rejected.status_code == 400
    assert paths["env"].read_bytes() == before_env
    assert paths["config"].read_bytes() == before_runtime
    assert b"replacement" not in before_env + before_runtime


def test_starter_hosts_are_builtin_read_only_and_ignore_stale_installed_copy(
    tmp_path: Path,
) -> None:
    client, paths = _client(tmp_path)
    target = paths["hosts"] / "starter-zh-cn.json"
    target.parent.mkdir(parents=True)
    stale = json.loads(
        (REPO_ROOT / "src/tracecast/console/starters/starter-zh-cn.json").read_text(
            encoding="utf-8"
        )
    )
    stale["display_name"] = "旧安装副本"
    target.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    hosts = client.get("/api/hosts").json()
    builtin = next(row for row in hosts if row["host_id"] == "starter-zh-cn")
    assert builtin["display_name"] != "旧安装副本"
    assert builtin["catalog_origin"] == "builtin"
    assert builtin["read_only"] is True
    assert target.exists()
    assert client.post(
        "/api/setup/starter-hosts/starter-zh-cn/install",
        headers={"Origin": "http://testserver"},
    ).status_code == 404


@pytest.mark.parametrize(
    ("name", "raw", "expected_encoding", "method"),
    [
        ("utf8.txt", b"plain UTF-8\r\ntext", "UTF-8", "strict_utf8"),
        ("utf8-bom.md", b"\xef\xbb\xbfBOM text", "UTF-8", "bom"),
        ("utf16le.srt", b"\xff\xfe" + "字幕甲\r\n字幕乙".encode("utf-16-le"), "UTF-16 LE", "bom"),
        ("utf16be.vtt", b"\xfe\xff" + "字幕甲\n字幕乙".encode("utf-16-be"), "UTF-16 BE", "bom"),
        ("utf32le.txt", b"\xff\xfe\x00\x00" + "宽字符文本".encode("utf-32-le"), "UTF-32 LE", "bom"),
        ("utf32be.txt", b"\x00\x00\xfe\xff" + "宽字符文本".encode("utf-32-be"), "UTF-32 BE", "bom"),
        ("gb18030.markdown", "中文编码回退文本".encode("gb18030"), "GB18030", "gb18030_fallback"),
    ],
)
def test_project_draft_automatic_encoding_matrix(
    tmp_path: Path,
    name: str,
    raw: bytes,
    expected_encoding: str,
    method: str,
) -> None:
    client, _paths = _client(tmp_path)
    response = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            "encoding-project",
            [_upload(name, raw, profile="reference_article", language="en-US")],
        ),
    )

    assert response.status_code == 200, response.text
    [file] = response.json()["files"]
    assert file["status"] == "ready"
    assert file["encoding"] == expected_encoding
    assert file["detection_method"] == method
    assert file["original_sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    ("encoding", "codec", "text"),
    [
        ("utf-16-le", "utf-16-le", "manual little endian"),
        ("utf-16-be", "utf-16-be", "manual big endian"),
        ("shift-jis", "shift_jis", "日本語の資料です"),
        ("cp949", "cp949", "한국어 자료입니다"),
        ("windows-1252", "cp1252", "Caf\u00e9 costs \u20ac5"),
    ],
)
def test_project_draft_explicit_encoding_matrix(
    tmp_path: Path, encoding: str, codec: str, text: str
) -> None:
    client, _paths = _client(tmp_path)
    response = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            "manual-encoding",
            [
                _upload(
                    "source.txt",
                    text.encode(codec),
                    encoding=encoding,
                    profile="reference_article",
                    language="en-US",
                )
            ],
        ),
    )

    assert response.status_code == 200, response.text
    [file] = response.json()["files"]
    assert file["preview"] == text
    assert file["encoding"] == encoding
    assert file["detection_method"] == "manual"


def test_ambiguous_encoding_returns_each_candidate_preview(tmp_path: Path) -> None:
    client, _paths = _client(tmp_path)
    raw = b"Price:\x80"
    response = client.post(
        "/api/project-drafts",
        json=_draft_payload("ambiguous", [_upload("source.txt", raw)]),
    )

    assert response.status_code == 200, response.text
    [file] = response.json()["files"]
    assert file["status"] == "needs_encoding"
    assert file["detection_method"] == "needs_manual_encoding"
    assert file["candidate_previews"]
    assert file["candidate_previews"]["windows-1252"] == "Price:\u20ac"


@pytest.mark.parametrize(
    ("encoding", "codec", "text", "preview_key"),
    [
        (
            "shift-jis",
            "shift_jis",
            "\u3053\u308c\u306f\u65e5\u672c\u8a9e\u306e\u8cc7\u6599\u3067\u3059\u3002\u6b21\u306e\u884c\u3082\u8aad\u307f\u307e\u3059\u3002",
            "shift-jis",
        ),
        (
            "cp949",
            "cp949",
            (
                "\uc774\uac83\uc740 \ud55c\uad6d\uc5b4 \uc790\ub8cc\uc785\ub2c8\ub2e4. "
                "\ub2e4\uc74c \ubb38\uc7a5\ub3c4 \uc77d\uc2b5\ub2c8\ub2e4."
            ),
            "cp949",
        ),
    ],
)
def test_competing_cjk_decoder_requires_choice_then_commits_correct_utf8(
    tmp_path: Path,
    encoding: str,
    codec: str,
    text: str,
    preview_key: str,
) -> None:
    client, paths = _client(tmp_path)
    raw = text.encode(codec)
    project_id = f"ambiguous-{encoding}"
    undecided = client.post(
        "/api/project-drafts",
        json=_draft_payload(project_id, [_upload("source.txt", raw)]),
    )
    assert undecided.status_code == 200, undecided.text
    [pending] = undecided.json()["files"]
    assert pending["status"] == "needs_encoding"
    assert pending["detection_method"] == "needs_manual_encoding"
    assert pending["candidate_previews"][preview_key] == text

    selected = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            project_id,
            [
                _upload(
                    "source.txt",
                    raw,
                    encoding=encoding,
                    profile="reference_article",
                    language="ja" if encoding == "shift-jis" else "ko",
                )
            ],
        ),
    )
    assert selected.status_code == 200, selected.text
    [ready] = selected.json()["files"]
    assert ready["status"] == "ready"
    assert ready["preview"] == text
    draft_id = selected.json()["draft_id"]
    assert client.post(f"/api/project-drafts/{draft_id}/estimate").json()["calls"] == 0
    committed = client.post(
        f"/api/project-drafts/{draft_id}/commit", json={"sources": None}
    )
    assert committed.status_code == 200, committed.text
    assert (paths["projects"] / project_id / "raw" / "source.txt").read_bytes() == (
        text.encode("utf-8")
    )
    original = paths["projects"] / project_id / "imports" / "original" / "source.txt"
    assert original.read_bytes() == raw


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ([_upload("source.pdf", b"text")], "unsupported source extension"),
        ([_upload("../source.txt", b"text")], "unsafe source filename"),
        ([_upload("CON.txt", b"text")], "reserved source filename"),
        ([_upload("source.txt", b"")], "empty"),
        ([_upload("source.txt", b"\x00\x01\x02")], "binary"),
        ([_upload("A.txt", b"a"), _upload("a.TXT", b"b")], "duplicate target filename"),
    ],
)
def test_project_draft_rejects_unsafe_or_non_text_inputs(
    tmp_path: Path, files: list[dict[str, Any]], message: str
) -> None:
    client, paths = _client(tmp_path)
    response = client.post(
        "/api/project-drafts",
        json=_draft_payload("rejected-project", files),
    )

    assert response.status_code == 400
    assert message in response.text
    assert not (paths["projects"] / "rejected-project").exists()


def test_manual_project_flow_is_zero_call_and_preserves_bytes_hashes_and_original(
    tmp_path: Path,
) -> None:
    client, paths = _client(tmp_path)
    text = "第一行\r\n第二行\r第三行\n"
    raw = b"\xff\xfe" + text.encode("utf-16-le")
    created = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            "manual-project",
            [
                _upload(
                    "source.txt",
                    raw,
                    profile="reference_article",
                    language="zh-CN",
                )
            ],
        ),
    )
    assert created.status_code == 200, created.text
    draft_id = created.json()["draft_id"]

    estimate = client.post(f"/api/project-drafts/{draft_id}/estimate")
    assert estimate.status_code == 200
    assert estimate.json()["calls"] == 0
    assert estimate.json()["rate_status"] == "no_calls"
    confirmed = client.post(f"/api/project-drafts/{draft_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["calls"] == 0
    committed = client.post(
        f"/api/project-drafts/{draft_id}/commit", json={"sources": None}
    )
    assert committed.status_code == 200, committed.text

    project = paths["projects"] / "manual-project"
    assert (project / "raw" / "source.txt").read_bytes() == text.encode("utf-8")
    assert (project / "imports" / "original" / "source.txt").read_bytes() == raw
    payload = json.loads((project / "sources.json").read_text(encoding="utf-8"))
    [source] = payload["sources"]
    assert source["content_hash"] == content_hash(text)
    metadata = source["metadata"]["import"]
    assert metadata == {
        "original_filename": "source.txt",
        "original_byte_sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": "UTF-16 LE",
        "encoding_detection": "bom",
        "original_preserved": True,
    }

    duplicate = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            "manual-project",
            [_upload("other.txt", b"other", profile="reference_article", language="en-US")],
        ),
    )
    assert duplicate.status_code == 409
    assert (project / "raw" / "source.txt").read_bytes() == text.encode("utf-8")


def test_auto_project_flow_requires_review_before_exclusive_commit(tmp_path: Path) -> None:
    fake = _IntakeCall()
    client, paths = _client(tmp_path, call=fake)
    _setup(client)
    created = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            "auto-project",
            [_upload("source.txt", b"An English reference article with enough prose.")],
            mode="auto",
        ),
    )
    assert created.status_code == 200
    draft_id = created.json()["draft_id"]

    estimate = client.post(f"/api/project-drafts/{draft_id}/estimate")
    assert estimate.status_code == 200
    assert estimate.json()["calls"] == 1
    assert estimate.json()["rate_status"] == "unconfigured"
    assert estimate.json()["configured_cost"] is None
    early_commit = client.post(
        f"/api/project-drafts/{draft_id}/commit", json={"sources": None}
    )
    assert early_commit.status_code == 400
    assert not (paths["projects"] / "auto-project").exists()

    submitted = client.post(f"/api/project-drafts/{draft_id}/confirm")
    assert submitted.status_code == 200, submitted.text
    job = _wait_job(client, submitted.json()["job_id"])
    assert job["state"] == "succeeded", job
    [suggested] = job["result"]["proposal"]["sources"]
    assert suggested["profile"] == "reference_article"
    assert suggested["language"] == "en"
    assert len(fake.calls) == 1

    reviewed = dict(suggested)
    reviewed["language"] = "en-US"
    reviewed["status"] = "ready"
    committed = client.post(
        f"/api/project-drafts/{draft_id}/commit", json={"sources": [reviewed]}
    )
    assert committed.status_code == 200, committed.text
    assert (paths["projects"] / "auto-project" / "sources.json").is_file()


def test_failed_auto_intake_and_invalid_review_leave_no_partial_project(
    tmp_path: Path,
) -> None:
    fake = _IntakeCall(fail=True)
    client, paths = _client(tmp_path, call=fake)
    _setup(client)
    created = client.post(
        "/api/project-drafts",
        json=_draft_payload(
            "failed-project",
            [_upload("source.txt", b"English source text for a failed Intake job.")],
            mode="auto",
        ),
    )
    draft_id = created.json()["draft_id"]
    submitted = client.post(f"/api/project-drafts/{draft_id}/confirm")
    assert submitted.status_code == 200
    job = _wait_job(client, submitted.json()["job_id"])
    assert job["state"] == "failed"
    assert not (paths["projects"] / "failed-project").exists()

    invalid = client.post(
        f"/api/project-drafts/{draft_id}/commit",
        json={
            "sources": [
                {
                    "source_id": "source",
                    "path": "raw/source.txt",
                    "profile": "unknown",
                    "language": "unknown",
                    "status": "needs_review",
                }
            ]
        },
    )
    assert invalid.status_code == 400
    assert not (paths["projects"] / "failed-project").exists()
