"""Offline Host Builder integration: two fake calls, artifacts, and CLI seam."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import tracecast.cli as cli
from tracecast.domain import LlmCallRecord, ProviderStatus
from tracecast.host_builder import HostBuildManifest, HostBuildParseError, run_host_build
from tracecast.host_builder.pipeline import (
    HOST_BUILD_MANIFEST_PATH,
    HOST_BUILD_PROPOSAL_PATH,
    HOST_BUILD_REPORT_PATH,
    HOST_EXAMPLES_ROUTE,
    HOST_PROFILE_ROUTE,
)
from tracecast.hosts import HostProfile, load_host
from tracecast.storage import LLM_CALL_RECORDS, RunStore, RuntimeConfig, sha256_json

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_corpus(tmp_path: Path) -> list[Path]:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    paths = []
    for index, char in enumerate("甲乙丙丁", start=1):
        path = corpus / f"{index}.txt"
        path.write_text(f"{char * 4}。{char * 5}！{char * 6}？", encoding="utf-8")
        paths.append(path)
    return paths


def _write_latin_corpus(tmp_path: Path) -> list[Path]:
    corpus = tmp_path / "latin-corpus"
    corpus.mkdir()
    paths = []
    for index in range(1, 5):
        path = corpus / f"{index}.txt"
        path.write_text(
            f"Concrete opening {index}. Detailed middle {index}. Concise ending {index}.",
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def _profile_response(output_language: str = "zh-CN") -> str:
    return "\n".join(
        (
            f"OUTPUT_LANGUAGE {output_language}",
            "VIEWPOINT 关注人物的选择、行动与后果。",
            "PREFER 具体行动与可验证转折",
            "AVOID 空泛总结",
            "THINKING_RULE 区分材料事实与主持判断。",
            "EXPRESSION 自然、克制、清楚的中文播客口语。",
            "EXPRESSION_RULE 长短句交替，判断紧随证据。",
        )
    )


def _examples_response(
    *,
    aliases: tuple[str, ...] = tuple(f"E{i:03d}" for i in range(1, 7)),
) -> str:
    rows = []
    for alias in aliases:
        rows.extend(
            (
                f"EXAMPLE {alias}",
                "SOURCE",
                f"{alias} 的朴素事实与信息骨架。",
                "ENDSOURCE",
                "ENDEXAMPLE",
            )
        )
    return "\n".join(rows)


class HostBuildFakeCall:
    """Fake provider preserving raw payloads and the standard call ledger."""

    def __init__(
        self,
        *,
        profile_response: str | None = None,
        examples_response: str | None = None,
        race_target: Path | None = None,
    ) -> None:
        self.profile_response = profile_response or _profile_response()
        self.examples_response = examples_response or _examples_response()
        self.race_target = race_target
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> tuple[str, LlmCallRecord]:
        self.calls.append(kwargs)
        route = kwargs["operation_route"]
        if route == HOST_PROFILE_ROUTE:
            response = self.profile_response
        elif route == HOST_EXAMPLES_ROUTE:
            response = self.examples_response
            if self.race_target is not None:
                self.race_target.parent.mkdir(parents=True, exist_ok=True)
                self.race_target.write_text("race winner", encoding="utf-8")
        else:  # pragma: no cover - guards the fake itself.
            raise AssertionError(f"unexpected Host Builder route: {route}")

        store: RunStore = kwargs["store"]
        call_id = f"host-build-fake-{len(self.calls)}"
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


def _config() -> RuntimeConfig:
    return RuntimeConfig.load(REPO_ROOT / "configs/runtime.example.json", load_env=False)


def _manifest(run_dir: Path) -> HostBuildManifest:
    payload = json.loads((run_dir / HOST_BUILD_MANIFEST_PATH).read_text(encoding="utf-8"))
    return HostBuildManifest.model_validate(payload)


def test_run_host_build_success_uses_two_calls_and_installs_literal_v2_host(
    tmp_path: Path,
) -> None:
    inputs = _write_corpus(tmp_path)
    hosts_dir = tmp_path / "hosts"
    runs_root = tmp_path / "runs"
    fake = HostBuildFakeCall()

    result = run_host_build(
        inputs=[tmp_path / "corpus", inputs[0]],
        host_id="sample-host",
        display_name="Sample Host",
        version="sample-host@custom",
        piece_max_tokens=7,
        seed=41,
        config=_config(),
        hosts_dir=hosts_dir,
        runs_root=runs_root,
        run_id="host-build-success",
        call=fake,
    )

    assert [item["operation_route"] for item in fake.calls] == [
        HOST_PROFILE_ROUTE,
        HOST_EXAMPLES_ROUTE,
    ]
    assert result.calls == 2
    assert result.sampled_examples == 6
    installed = load_host(hosts_dir, "sample-host")
    assert installed == result.host
    assert installed.schema_version == "tracecast-host/2"
    assert installed.display_name == "Sample Host"
    assert installed.version == "sample-host@custom"
    assert installed.output_language == "zh-CN"
    assert installed.host_thinking.human_directive == []
    assert set(installed.style_examples_by_position) == {"opening", "middle", "ending"}
    assert all(len(rows) == 2 for rows in installed.style_examples_by_position.values())

    run_dir = runs_root / "host-build-success"
    manifest = _manifest(run_dir)
    assert manifest.status == "complete"
    assert manifest.seed == 41
    assert manifest.dominant_script == "cjk"
    assert manifest.control_prompt_decision.source_language.value == "unknown"
    assert manifest.control_prompt_decision.variant.value == "en"
    assert manifest.output_language == "zh-CN"
    assert manifest.call_ids == ["host-build-fake-1", "host-build-fake-2"]
    assert manifest.candidates
    assert len(manifest.selected_examples) == 6
    assert {row.candidate_id for row in manifest.selected_examples} <= {
        row.candidate_id for row in manifest.candidates
    }
    assert {row.position for row in manifest.candidates} == {
        "opening",
        "middle",
        "ending",
    }
    assert manifest.installed_path == str((hosts_dir / "sample-host.json").resolve())
    assert (run_dir / HOST_BUILD_PROPOSAL_PATH).is_file()
    assert (run_dir / HOST_BUILD_REPORT_PATH).is_file()
    proposal = HostProfile.model_validate_json(
        (run_dir / HOST_BUILD_PROPOSAL_PATH).read_text(encoding="utf-8")
    )
    assert proposal == installed

    renders = [
        example.render
        for position in ("opening", "middle", "ending")
        for example in installed.style_examples_by_position[position]
    ]
    input_by_path = {str(path.resolve()): path.read_text(encoding="utf-8") for path in inputs}
    for selected, render in zip(manifest.selected_examples, renders, strict=True):
        raw = input_by_path[selected.input_path]
        assert render == raw[selected.span_start : selected.span_end]
        assert render == render.strip()
        assert selected.render_hash
    for candidate in manifest.candidates:
        raw = input_by_path[candidate.input_path]
        assert raw[candidate.span_start : candidate.span_end].strip()
        assert candidate.render_hash

    profile_prompt = fake.calls[0]["messages"][1]["content"]
    reverse_prompt = fake.calls[1]["messages"][1]["content"]
    assert profile_prompt.count("SAMPLE P") > 6
    assert reverse_prompt.count("EXAMPLE_INPUT E") == 6
    assert all(render in reverse_prompt for render in renders)
    ledger = RunStore(run_dir).read_records(LLM_CALL_RECORDS)
    assert [record.operation_route for record in ledger] == [
        HOST_PROFILE_ROUTE,
        HOST_EXAMPLES_ROUTE,
    ]
    assert [record.work_unit.kind for record in ledger] == [
        "host_profile_distill",
        "host_style_example_reverse",
    ]
    assert ledger[0].prompt_version == (
        "host_profile_distill.en.v2+host_profile_distill_user.en.v2"
    )
    assert all(".neutral." not in record.prompt_version for record in ledger)
    assert all(record.raw_request_path and record.raw_response_path for record in ledger)


def test_run_host_build_compiles_repeated_expression_without_repair(
    tmp_path: Path,
) -> None:
    inputs = _write_corpus(tmp_path)
    fake = HostBuildFakeCall(
        profile_response="\n".join(
            (
                "OUTPUT_LANGUAGE zh-CN",
                "VIEWPOINT 从个人疑问切入，再展开机制与叙事。",
                "PREFER 用生活化类比解释复杂机制",
                "EXPRESSION 用口语化表达传递情绪。",
                "EXPRESSION 用简短过渡句衔接段落。",
                "EXPRESSION 用固定告别语收束。",
                "EXPRESSION_RULE 先给生活化类比，再回到材料本身。",
            )
        )
    )

    result = run_host_build(
        inputs=inputs,
        host_id="repeated-expression",
        piece_max_tokens=7,
        config=_config(),
        hosts_dir=tmp_path / "hosts",
        runs_root=tmp_path / "runs",
        run_id="repeated-expression-run",
        call=fake,
    )

    assert [item["operation_route"] for item in fake.calls] == [
        HOST_PROFILE_ROUTE,
        HOST_EXAMPLES_ROUTE,
    ]
    assert result.calls == 2
    assert result.host.host_expression.description == (
        "用口语化表达传递情绪。 用简短过渡句衔接段落。 用固定告别语收束。"
    )
    assert _manifest(tmp_path / "runs" / "repeated-expression-run").status == "complete"


def test_existing_host_is_rejected_before_run_or_fake_call(tmp_path: Path) -> None:
    inputs = _write_corpus(tmp_path)
    hosts_dir = tmp_path / "hosts"
    hosts_dir.mkdir()
    target = hosts_dir / "taken.json"
    target.write_text("existing", encoding="utf-8")
    fake = HostBuildFakeCall()

    with pytest.raises(FileExistsError, match="host already exists"):
        run_host_build(
            inputs=inputs,
            host_id="taken",
            piece_max_tokens=7,
            config=_config(),
            hosts_dir=hosts_dir,
            runs_root=tmp_path / "runs",
            run_id="must-not-exist",
            call=fake,
        )

    assert fake.calls == []
    assert target.read_text(encoding="utf-8") == "existing"
    assert not (tmp_path / "runs" / "must-not-exist").exists()


def test_insufficient_candidates_fail_before_run_or_fake_call(tmp_path: Path) -> None:
    inputs = [
        (tmp_path / "one.txt"),
        (tmp_path / "two.txt"),
    ]
    inputs[0].write_text("只有一段。", encoding="utf-8")
    inputs[1].write_text("另外一段。", encoding="utf-8")
    fake = HostBuildFakeCall()

    with pytest.raises(ValueError, match="not enough unique middle candidates"):
        run_host_build(
            inputs=inputs,
            host_id="short",
            config=_config(),
            hosts_dir=tmp_path / "hosts",
            runs_root=tmp_path / "runs",
            run_id="short-run",
            call=fake,
        )

    assert fake.calls == []
    assert not (tmp_path / "runs" / "short-run").exists()


def test_language_conflict_stops_after_profile_call_and_preserves_failure_report(
    tmp_path: Path,
) -> None:
    inputs = _write_corpus(tmp_path)
    fake = HostBuildFakeCall(profile_response=_profile_response("en"))

    with pytest.raises(ValueError, match="corpus is 'cjk'"):
        run_host_build(
            inputs=inputs,
            host_id="wrong-language",
            piece_max_tokens=7,
            config=_config(),
            hosts_dir=tmp_path / "hosts",
            runs_root=tmp_path / "runs",
            run_id="wrong-language-run",
            call=fake,
        )

    assert [item["operation_route"] for item in fake.calls] == [HOST_PROFILE_ROUTE]
    assert not (tmp_path / "hosts" / "wrong-language.json").exists()
    run_dir = tmp_path / "runs" / "wrong-language-run"
    manifest = _manifest(run_dir)
    assert manifest.status == "failed"
    assert manifest.call_ids == ["host-build-fake-1"]
    assert any("corpus is 'cjk'" in item for item in manifest.diagnostics)
    assert (run_dir / HOST_BUILD_REPORT_PATH).is_file()
    assert not (run_dir / HOST_BUILD_PROPOSAL_PATH).exists()


def test_latin_corpus_accepts_en_profile_and_installs_host(tmp_path: Path) -> None:
    inputs = _write_latin_corpus(tmp_path)
    fake = HostBuildFakeCall(profile_response=_profile_response("en-US"))

    result = run_host_build(
        inputs=inputs,
        host_id="english-host",
        piece_max_tokens=5,
        config=_config(),
        hosts_dir=tmp_path / "hosts",
        runs_root=tmp_path / "runs",
        run_id="english-host-run",
        call=fake,
    )

    assert result.host.output_language == "en-US"
    assert result.host.schema_version == "tracecast-host/2"
    assert [item["operation_route"] for item in fake.calls] == [
        HOST_PROFILE_ROUTE,
        HOST_EXAMPLES_ROUTE,
    ]
    manifest = _manifest(tmp_path / "runs" / "english-host-run")
    assert manifest.status == "complete"
    assert manifest.dominant_script == "latin"
    assert manifest.output_language == "en-US"


def test_latin_corpus_rejects_chinese_profile_before_example_call(tmp_path: Path) -> None:
    inputs = _write_latin_corpus(tmp_path)
    fake = HostBuildFakeCall(profile_response=_profile_response("zh-CN"))

    with pytest.raises(ValueError, match="corpus is 'latin'"):
        run_host_build(
            inputs=inputs,
            host_id="latin-with-chinese-profile",
            piece_max_tokens=5,
            config=_config(),
            hosts_dir=tmp_path / "hosts",
            runs_root=tmp_path / "runs",
            run_id="latin-language-conflict",
            call=fake,
        )

    assert [item["operation_route"] for item in fake.calls] == [HOST_PROFILE_ROUTE]
    assert not (tmp_path / "hosts" / "latin-with-chinese-profile.json").exists()
    assert _manifest(tmp_path / "runs" / "latin-language-conflict").status == "failed"


@pytest.mark.parametrize("output_language", ["zh-CN extra", "und", "unknown"])
def test_invalid_bcp47_profile_language_stops_before_example_call(
    tmp_path: Path,
    output_language: str,
) -> None:
    inputs = _write_corpus(tmp_path)
    fake = HostBuildFakeCall(profile_response=_profile_response(output_language))

    with pytest.raises(ValueError, match="BCP-47"):
        run_host_build(
            inputs=inputs,
            host_id="invalid-language-tag",
            piece_max_tokens=7,
            config=_config(),
            hosts_dir=tmp_path / "hosts",
            runs_root=tmp_path / "runs",
            run_id="invalid-language-tag-run",
            call=fake,
        )

    assert [item["operation_route"] for item in fake.calls] == [HOST_PROFILE_ROUTE]
    assert not (tmp_path / "hosts" / "invalid-language-tag.json").exists()
    assert _manifest(tmp_path / "runs" / "invalid-language-tag-run").status == "failed"


def test_invalid_second_dsl_preserves_two_call_ledger_without_catalog_host(
    tmp_path: Path,
) -> None:
    inputs = _write_corpus(tmp_path)
    fake = HostBuildFakeCall(examples_response=_examples_response(aliases=("E001",)))

    with pytest.raises(HostBuildParseError, match="coverage mismatch"):
        run_host_build(
            inputs=inputs,
            host_id="invalid-examples",
            piece_max_tokens=7,
            config=_config(),
            hosts_dir=tmp_path / "hosts",
            runs_root=tmp_path / "runs",
            run_id="invalid-examples-run",
            call=fake,
        )

    run_dir = tmp_path / "runs" / "invalid-examples-run"
    manifest = _manifest(run_dir)
    assert manifest.status == "failed"
    assert manifest.call_ids == ["host-build-fake-1", "host-build-fake-2"]
    assert len(RunStore(run_dir).read_records(LLM_CALL_RECORDS)) == 2
    assert not (tmp_path / "hosts" / "invalid-examples.json").exists()
    assert not (run_dir / HOST_BUILD_PROPOSAL_PATH).exists()


def test_install_race_keeps_proposal_and_never_overwrites_winner(tmp_path: Path) -> None:
    inputs = _write_corpus(tmp_path)
    target = tmp_path / "hosts" / "raced.json"
    fake = HostBuildFakeCall(race_target=target)

    with pytest.raises(FileExistsError):
        run_host_build(
            inputs=inputs,
            host_id="raced",
            piece_max_tokens=7,
            config=_config(),
            hosts_dir=target.parent,
            runs_root=tmp_path / "runs",
            run_id="raced-run",
            call=fake,
        )

    assert target.read_text(encoding="utf-8") == "race winner"
    run_dir = tmp_path / "runs" / "raced-run"
    assert (run_dir / HOST_BUILD_PROPOSAL_PATH).is_file()
    assert _manifest(run_dir).status == "failed"


def test_cli_host_build_forwards_public_options_and_prints_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "schema_version": "tracecast-host/2",
        "host_id": "cli-host",
        "display_name": "CLI Host",
        "version": "cli-host@v2",
        "output_language": "zh-CN",
        "host_thinking": {"viewpoint": "角度"},
        "host_expression": {"description": "表达"},
        "style_examples_by_position": {
            position: [
                {"source": f"{position}{i} 源", "render": f"{position}{i} 文"}
                for i in (1, 2)
            ]
            for position in ("opening", "middle", "ending")
        },
    }
    host = HostProfile.model_validate(payload)
    captured: dict[str, Any] = {}

    def fake_run_host_build(**kwargs: Any):
        captured.update(kwargs)
        return type(
            "Result",
            (),
            {
                "run_id": "cli-run",
                "run_dir": str(tmp_path / "runs" / "cli-run"),
                "host_path": str(tmp_path / "hosts" / "cli-host.json"),
                "host": host,
                "sampled_examples": 6,
                "calls": 2,
            },
        )()

    monkeypatch.setattr(cli, "run_host_build", fake_run_host_build)
    exit_code = cli.main(
        [
            "host-build",
            "one.txt",
            "two.txt",
            "--host-id",
            "cli-host",
            "--display-name",
            "CLI Host",
            "--version",
            "cli-host@v2",
            "--piece-max-tokens",
            "91",
            "--seed",
            "13",
            "--config",
            str(REPO_ROOT / "configs/runtime.example.json"),
            "--hosts-dir",
            str(tmp_path / "hosts"),
            "--runs-root",
            str(tmp_path / "runs"),
            "--run-id",
            "cli-run",
        ]
    )

    assert exit_code == 0
    assert captured["inputs"] == ["one.txt", "two.txt"]
    assert captured["host_id"] == "cli-host"
    assert captured["display_name"] == "CLI Host"
    assert captured["version"] == "cli-host@v2"
    assert captured["piece_max_tokens"] == 91
    assert captured["seed"] == 13
    assert captured["run_id"] == "cli-run"
    output = capsys.readouterr().out
    assert "cli-run" in output
    assert "zh-CN" in output
    assert "style examples    6" in output
    assert "llm calls         2" in output
