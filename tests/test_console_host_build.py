"""Independent failure-path regression tests for the Console Host Builder."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tracecast.console import host_build as host_build_service
from tracecast.console.context import ConsoleContext
from tracecast.console.errors import BadRequest, Conflict
from tracecast.console.host_build import UploadedPreferenceFile
from tracecast.console.run_artifacts import list_runs
from tracecast.domain import LlmCallRecord, ProviderStatus
from tracecast.host_builder.pipeline import HOST_EXAMPLES_ROUTE, HOST_PROFILE_ROUTE
from tracecast.hosts import load_host
from tracecast.storage import LLM_CALL_RECORDS, RunStore, sha256_json

REPO_ROOT = Path(__file__).resolve().parents[1]


def _profile_response() -> str:
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


def _examples_response(
    aliases: tuple[str, ...] = tuple(f"E{index:03d}" for index in range(1, 7)),
) -> str:
    rows: list[str] = []
    for alias in aliases:
        rows.extend(
            (
                f"EXAMPLE {alias}",
                "SOURCE",
                f"{alias} 的朴素事实骨架。",
                "ENDSOURCE",
                "ENDEXAMPLE",
            )
        )
    return "\n".join(rows)


class _FakeCall:
    """Offline provider seam that also persists the standard audit ledger."""

    def __init__(self, *, profile_response: str | None = None) -> None:
        self.profile_response = profile_response or _profile_response()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[str, LlmCallRecord]:
        self.calls.append(kwargs)
        route = kwargs["operation_route"]
        if route == HOST_PROFILE_ROUTE:
            response = self.profile_response
        elif route == HOST_EXAMPLES_ROUTE:
            user_prompt = kwargs["messages"][1]["content"]
            aliases = tuple(
                re.findall(r"^EXAMPLE_INPUT (E\d{3})\b", user_prompt, re.MULTILINE)
            )
            response = _examples_response(aliases)
        else:  # pragma: no cover - protects the fake itself.
            raise AssertionError(f"unexpected route: {route}")

        store: RunStore = kwargs["store"]
        call_id = f"console-host-fake-{len(self.calls)}"
        request = {"messages": kwargs["messages"]}
        request_path = store.write_raw_payload(call_id, "request", request)
        response_path = store.write_raw_payload(call_id, "response", response)
        route_config = kwargs["config"].route(route)
        record = LlmCallRecord(
            call_id=call_id,
            stage=route_config.stage,
            operation_route=route,
            provider="offline-test",
            model=route_config.model,
            prompt_version=kwargs.get("prompt_version"),
            input_hash=f"sha256:{sha256_json(request)}",
            raw_request_path=request_path,
            raw_response_path=response_path,
            provider_status=ProviderStatus.success,
            created_at=datetime.now(tz=UTC),
            work_unit=kwargs.get("work_unit"),
        )
        store.append_record(LLM_CALL_RECORDS, record)
        return response, record


class _BlockingFirstCall(_FakeCall):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **kwargs: Any) -> tuple[str, LlmCallRecord]:
        if kwargs["operation_route"] == HOST_PROFILE_ROUTE:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise AssertionError("test did not release the first provider call")
        return super().__call__(**kwargs)


def _files() -> tuple[UploadedPreferenceFile, ...]:
    return tuple(
        UploadedPreferenceFile(
            name=f"偏好稿-{index}.txt",
            content=(
                f"{char * 5}，开场有具体动作。"
                f"{char * 7}，中段出现转折！"
                f"{char * 9}，结尾留下判断？"
            ),
        )
        for index, char in enumerate("甲乙丙丁", start=1)
    )


def _single_file() -> tuple[UploadedPreferenceFile, ...]:
    return (
        UploadedPreferenceFile(
            name="单篇偏好稿.txt",
            content="甲甲甲甲。乙乙乙乙！丙丙丙丙？丁丁丁丁。戊戊戊戊！",
        ),
    )


def _context(tmp_path: Path, call: Any) -> ConsoleContext:
    runs_root = tmp_path / "runs"
    hosts_dir = tmp_path / "hosts"
    runs_root.mkdir()
    hosts_dir.mkdir()
    return ConsoleContext(
        runs_root=runs_root,
        projects_dir=tmp_path / "projects",
        hosts_dir=hosts_dir,
        config_path=REPO_ROOT / "configs" / "runtime.example.json",
        profiles_path=tmp_path / "unused-profiles.json",
        call=call,
    )


def _wait_for_job(ctx: ConsoleContext, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = ctx.registry.get(job_id).snapshot()
        if snapshot["state"] not in ("pending", "running"):
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def _track_staging_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = host_build_service.tempfile.TemporaryDirectory
    paths: list[Path] = []

    def recording_directory(*args: Any, **kwargs: Any) -> Any:
        directory = original(*args, **kwargs)
        paths.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        host_build_service.tempfile,
        "TemporaryDirectory",
        recording_directory,
    )
    return paths


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.mark.parametrize(
    ("files", "message"),
    (
        ((), "select at least one"),
        ((UploadedPreferenceFile(name="note.md", content="正文"),), "must be .txt"),
        ((UploadedPreferenceFile(name="empty.txt", content="  \n"),), "is empty"),
        (
            (
                UploadedPreferenceFile(name="Voice.txt", content="甲。乙。丙。"),
                UploadedPreferenceFile(name="voice.TXT", content="丁。戊。己。"),
            ),
            "duplicate reference file name",
        ),
        (
            (UploadedPreferenceFile(name="../escape.txt", content="甲。乙。丙。"),),
            "invalid reference file name",
        ),
    ),
)
def test_invalid_uploads_are_rejected_before_staging_or_provider_call(
    tmp_path: Path,
    files: tuple[UploadedPreferenceFile, ...],
    message: str,
) -> None:
    fake = _FakeCall()
    ctx = _context(tmp_path, fake)

    with pytest.raises(BadRequest, match=message):
        host_build_service.build_host(
            ctx,
            files=files,
            host_id="invalid-upload",
            piece_max_tokens=7,
        )

    assert fake.calls == []
    assert list(ctx.runs_root.iterdir()) == []
    assert list(ctx.hosts_dir.iterdir()) == []


def test_existing_host_is_rejected_before_prepare_and_never_overwritten(
    tmp_path: Path,
) -> None:
    fake = _FakeCall()
    ctx = _context(tmp_path, fake)
    target = ctx.hosts_dir / "existing.json"
    original = b"pre-existing catalog bytes\n"
    target.write_bytes(original)

    with pytest.raises(Conflict, match="host already exists"):
        host_build_service.build_host(
            ctx,
            files=_files(),
            host_id="existing",
            piece_max_tokens=7,
            confirm=True,
        )

    assert target.read_bytes() == original
    assert fake.calls == []
    assert list(ctx.runs_root.iterdir()) == []


def test_success_removes_prepare_and_job_staging_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCall()
    ctx = _context(tmp_path, fake)
    staging_dirs = _track_staging_dirs(monkeypatch)

    submitted = host_build_service.build_host(
        ctx,
        files=_files(),
        host_id="cleanup-success",
        piece_max_tokens=7,
        confirm=True,
    )
    job = _wait_for_job(ctx, submitted["job_id"])

    assert job["state"] == "succeeded", job
    assert len(fake.calls) == 2
    assert len(staging_dirs) == 2
    assert all(not path.exists() for path in staging_dirs)
    assert (ctx.hosts_dir / "cleanup-success.json").is_file()


def test_single_file_dry_run_and_confirm_build_variable_v2_pool_with_two_calls(
    tmp_path: Path,
) -> None:
    fake = _FakeCall()
    ctx = _context(tmp_path, fake)
    files = _single_file()

    dry_run = host_build_service.build_host(
        ctx,
        files=files,
        host_id="single-file-host",
        piece_max_tokens=7,
        seed=37,
    )

    assert dry_run["confirm_required"] is True
    assert dry_run["estimate"]["calls"] == 2
    preview = dry_run["estimate"]["selected_examples"]
    assert Counter(row["position"] for row in preview) == Counter(
        {"opening": 1, "middle": 2, "ending": 1}
    )
    assert 3 <= len(preview) <= 6
    assert fake.calls == []
    assert list(ctx.runs_root.iterdir()) == []

    submitted = host_build_service.build_host(
        ctx,
        files=files,
        host_id="single-file-host",
        piece_max_tokens=7,
        seed=37,
        confirm=True,
    )
    job = _wait_for_job(ctx, submitted["job_id"])

    assert job["state"] == "succeeded", job
    assert job["result"]["calls"] == 2
    assert job["result"]["sampled_examples"] == len(preview)
    assert [call["operation_route"] for call in fake.calls] == [
        HOST_PROFILE_ROUTE,
        HOST_EXAMPLES_ROUTE,
    ]
    host = load_host(ctx.hosts_dir, "single-file-host")
    assert host.schema_version == "tracecast-host/2"
    assert {
        position: len(rows)
        for position, rows in host.style_examples_by_position.items()
    } == {"opening": 1, "middle": 2, "ending": 1}
    run_dir = ctx.runs_root / submitted["run_id"]
    assert len(RunStore(run_dir).read_records(LLM_CALL_RECORDS)) == 2
    assert submitted["run_id"] not in {
        row["run_id"] for row in list_runs(ctx.runs_root)
    }


def test_profile_failure_cleans_staging_and_leaves_catalog_and_full_inputs_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCall(profile_response="OUTPUT_LANGUAGE zh-CN")
    ctx = _context(tmp_path, fake)
    marker = ctx.hosts_dir / ".catalog-marker"
    marker.write_text("unchanged", encoding="utf-8")
    before_catalog = _directory_digest(ctx.hosts_dir)
    staging_dirs = _track_staging_dirs(monkeypatch)
    files = _files()

    submitted = host_build_service.build_host(
        ctx,
        files=files,
        host_id="parse-failure",
        piece_max_tokens=7,
        confirm=True,
    )
    job = _wait_for_job(ctx, submitted["job_id"])

    assert job["state"] == "failed", job
    assert len(fake.calls) == 1
    assert len(staging_dirs) == 2
    assert all(not path.exists() for path in staging_dirs)
    assert _directory_digest(ctx.hosts_dir) == before_catalog
    assert not (ctx.hosts_dir / "parse-failure.json").exists()

    run_dir = ctx.runs_root / submitted["run_id"]
    assert (run_dir / "host_builder" / "report.md").is_file()
    assert submitted["run_id"] not in {
        row["run_id"] for row in list_runs(ctx.runs_root)
    }
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in run_dir.rglob("*")
        if path.is_file()
    )
    assert all(row.content not in persisted for row in files)


def test_cancel_after_first_call_stops_second_call_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _BlockingFirstCall()
    ctx = _context(tmp_path, fake)
    staging_dirs = _track_staging_dirs(monkeypatch)
    before_catalog = _directory_digest(ctx.hosts_dir)

    submitted = host_build_service.build_host(
        ctx,
        files=_files(),
        host_id="cancelled-build",
        piece_max_tokens=7,
        confirm=True,
    )
    assert fake.entered.wait(timeout=5), "first provider call did not start"
    ctx.registry.request_cancel(submitted["job_id"])
    fake.release.set()
    job = _wait_for_job(ctx, submitted["job_id"])

    assert job["state"] == "cancelled", job
    assert len(fake.calls) == 1
    assert len(staging_dirs) == 2
    assert all(not path.exists() for path in staging_dirs)
    assert _directory_digest(ctx.hosts_dir) == before_catalog
    assert not (ctx.hosts_dir / "cancelled-build.json").exists()


def test_run_shell_guards_missing_base_identity_before_reading_operation() -> None:
    source = (
        REPO_ROOT / "src/tracecast/console/static/js/views/run/shell.js"
    ).read_text(encoding="utf-8")

    assert "baseDisplay && baseDisplay.title === display.project" in source
    assert "baseDisplay?.title === display.project" not in source
