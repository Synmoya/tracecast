"""Tests for sampled DSL source intake."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tracecast import cli
from tracecast.domain import LlmCallRecord, ProviderStatus
from tracecast.intake import IntakeParseError, build_source_probe, parse_probe_response, run_intake
from tracecast.storage import RuntimeConfig, read_json


def _record(index: int, prompt_version: str | None) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=f"intake_call_{index}",
        stage="intake",
        operation_route="source_profile_probe",
        provider="fake",
        model="fake",
        key_id="fake",
        input_hash=f"hash_{index}",
        provider_status=ProviderStatus.success,
        created_at=datetime.now(UTC),
        prompt_version=prompt_version,
    )


def _write_prompt(path: Path, version: str, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<!-- version: {version} -->\n{body}", encoding="utf-8")
    return str(path)


def _config(tmp_path: Path) -> RuntimeConfig:
    for locale in ("zh", "en"):
        _write_prompt(
            tmp_path
            / "prompt"
            / "intake"
            / f"source_profile_probe.system.{locale}.md",
            f"source_profile_probe.{locale}@test",
            f"system-{locale}",
        )
        _write_prompt(
            tmp_path
            / "prompt"
            / "intake"
            / f"source_profile_probe.user.{locale}.md",
            f"source_profile_probe_user.{locale}@test",
            "profiles\n{{profile_catalog}}\n\nsources\n{{source_probe_blocks}}",
        )
    system = str(
        tmp_path / "prompt" / "intake" / "source_profile_probe.system.{locale}.md"
    )
    user = str(
        tmp_path / "prompt" / "intake" / "source_profile_probe.user.{locale}.md"
    )
    return RuntimeConfig.model_validate(
        {
            "providers": {"fake": {"base_url": "https://fake.invalid", "keys": []}},
            "routes": {
                "source_profile_probe": {
                    "stage": "intake",
                    "provider": "fake",
                    "model": "fake",
                    "system_prompt": system,
                    "user_prompt": user,
                    "max_concurrency": 1,
                }
            },
            "stage_budgets": {
                "ingestion": {
                    "source_piece_max_tokens": 40,
                    "atom_window_max_input_tokens": 1000,
                    "pieces_per_call_soft_limit": 4,
                }
            },
        }
    )


def _profiles(path: Path) -> None:
    path.write_text(
        """{
  "profiles": {
    "script_transcript": {
      "description": "Script-like source.",
      "default_source_class": "script_transcript",
      "default_trust_tier": "canonical",
      "allowed_content_kinds": ["dialogue", "narration", "assertion"],
      "allowed_tags": ["speaker_turn"]
    },
    "review": {
      "description": "Attributed review.",
      "default_source_class": "review",
      "default_trust_tier": "low",
      "allowed_content_kinds": ["opinion"],
      "allowed_tags": ["critique"]
    }
  }
}
""",
        encoding="utf-8",
    )


def test_source_probe_samples_head_middle_tail_and_signal_with_budget() -> None:
    raw = "\n\n".join(
        [
            "OPENING LINE",
            "plain early one",
            "plain early two",
            "TERRA: Who am I?",
            "plain middle one",
            "plain middle two",
            "plain late one",
            "This review says I think it is excellent.",
            "plain late two",
            "ENDING LINE",
        ]
    )

    probe = build_source_probe(
        source_alias="S001",
        path="raw/example.txt",
        raw_text=raw,
        source_id_guess="example",
        source_piece_max_tokens=6,
        sample_token_budget=80,
    )

    labels = [sample.label for sample in probe.samples]
    assert any(label.startswith("head") for label in labels)
    assert any(label.startswith("middle") for label in labels)
    assert any(label.startswith("tail") for label in labels)
    assert "signal:speaker_colon" in labels
    assert len({sample.piece_id for sample in probe.samples}) == len(probe.samples)


def test_parse_probe_response_rejects_missing_endsrc_and_bad_ready_state() -> None:
    with pytest.raises(IntakeParseError, match="missing ENDSRC"):
        parse_probe_response(
            "SRC S001 | profile=review | language=en | status=ready\nTITLE Review",
            known_profiles={"review"},
            expected_aliases={"S001"},
        )

    with pytest.raises(IntakeParseError, match="status=ready"):
        parse_probe_response(
            "SRC S001 | profile=unknown | language=en | status=ready\n"
            "TITLE x\nNOTE y\nREASON z\nENDSRC",
            known_profiles={"review"},
            expected_aliases={"S001"},
        )


def test_run_intake_uses_sampled_dsl_and_writes_proposal_without_sources_json(
    tmp_path: Path,
) -> None:
    project = tmp_path / "projects" / "demo"
    raw_dir = project / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "demo_script.txt").write_text("TERRA: Who am I?\nLOCKE: Easy.", encoding="utf-8")
    (raw_dir / "demo_review.txt").write_text(
        "This review says I think the game is thoughtful.", encoding="utf-8"
    )
    profiles = tmp_path / "profiles.json"
    _profiles(profiles)
    config = _config(tmp_path)
    seen_messages: list[list[dict[str, Any]]] = []
    seen_prompt_versions: list[str | None] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        seen_messages.append(kwargs["messages"])
        seen_prompt_versions.append(kwargs.get("prompt_version"))
        text = (
            "SRC S001 | profile=script_transcript | language=en | status=ready\n"
            "TITLE Demo Script\n"
            "NOTE Script-like dialogue source.\n"
            "REASON speaker dialogue pattern dominates.\n"
            "ENDSRC\n"
            "SRC S002 | profile=review | language=en | status=ready\n"
            "TITLE Demo Review\n"
            "NOTE Review source with opinion wording.\n"
            "REASON review terms and first-person judgement dominate.\n"
            "ENDSRC\n"
        )
        return text, _record(len(seen_messages), kwargs.get("prompt_version"))

    result = run_intake(
        project_dir=project,
        profiles_path=profiles,
        output_language="zh-CN",
        config=config,
        runs_root=tmp_path / "runs",
        run_id="demo-intake",
        call=fake_call,
    )

    assert result.calls == 1
    assert result.control_prompt_decision.source_language.value == "unknown"
    assert result.control_prompt_decision.variant.value == "en"
    assert len(seen_prompt_versions) == 1
    assert seen_prompt_versions[0] is not None
    assert seen_prompt_versions[0].startswith(
        "source_profile_probe.en@test+source_profile_probe_user.en@test+"
    )
    assert not (project / "sources.json").exists()
    proposal = read_json(project / "sources.proposed.json")
    assert proposal["output_language"] == "zh-CN"
    assert [row["profile"] for row in proposal["sources"]] == [
        "script_transcript",
        "review",
    ]
    assert proposal["sources"][0]["source_type"] == "local_text"
    assert proposal["sources"][0]["source_class"] == "script_transcript"
    assert proposal["sources"][0]["trust_tier"] == "canonical"
    assert proposal["sources"][0]["content_hash"].startswith("sha256:")
    user_prompt = seen_messages[0][1]["content"]
    assert "SRC S001" in user_prompt
    assert "SAMPLE" in user_prompt
    assert "PROFILE script_transcript" in user_prompt
    report = (project / "intake_report.md").read_text(encoding="utf-8")
    assert "source language: `unknown`; control prompt variant: `en`" in report


def test_run_intake_preserves_existing_sources_and_write_sources_is_explicit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "projects" / "demo"
    raw_dir = project / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "existing.txt").write_text("TERRA: Who am I?", encoding="utf-8")
    (project / "sources.json").write_text(
        """{
  "project_id": "demo",
  "output_language": "zh-CN",
  "sources": [
    {
      "source_id": "manual_existing",
      "path": "raw/existing.txt",
      "profile": "review",
      "language": "en",
      "note": "manual row"
    }
  ]
}
""",
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles.json"
    _profiles(profiles)

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        text = (
            "SRC S001 | profile=script_transcript | language=en | status=ready\n"
            "TITLE Existing Script\n"
            "NOTE Probe says script.\n"
            "REASON speaker dialogue pattern dominates.\n"
            "ENDSRC\n"
        )
        return text, _record(1, kwargs.get("prompt_version"))

    result = run_intake(
        project_dir=project,
        profiles_path=profiles,
        output_language="zh-CN",
        config=_config(tmp_path),
        runs_root=tmp_path / "runs",
        run_id="demo-intake",
        write_sources=True,
        call=fake_call,
    )

    assert result.wrote_sources
    sources = read_json(project / "sources.json")
    assert sources["sources"][0]["source_id"] == "manual_existing"
    assert sources["sources"][0]["profile"] == "review"
    assert "persona" not in sources
    assert "probe suggested 'script_transcript'" in (project / "intake_report.md").read_text(
        encoding="utf-8"
    )


def test_cli_intake_forwards_write_sources_only_when_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    config = tmp_path / "runtime.json"
    config.write_text('{"routes": {}, "providers": {}, "stage_budgets": {}}', encoding="utf-8")
    calls: list[tuple[bool, str]] = []

    def fake_run_intake(**kwargs: Any) -> Any:
        calls.append((bool(kwargs["write_sources"]), kwargs["output_language"]))
        return type(
            "Result",
            (),
            {
                "run_id": "run",
                "proposal_path": "proposal",
                "report_path": "report",
                "raw_sources": 0,
                "proposed_sources": 0,
                "needs_review": 0,
                "calls": 0,
                "wrote_sources": kwargs["write_sources"],
            },
        )()

    monkeypatch.setattr(cli, "run_intake", fake_run_intake)

    assert (
        cli.main(
            [
                "intake",
                "--project",
                "demo",
                "--projects-dir",
                str(tmp_path / "projects"),
                "--config",
                str(config),
                "--output-language",
                "zh-CN",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "intake",
                "--project",
                "demo",
                "--projects-dir",
                str(tmp_path / "projects"),
                "--config",
                str(config),
                "--output-language",
                "zh-CN",
                "--write-sources",
            ]
        )
        == 0
    )

    assert calls == [(False, "zh-CN"), (True, "zh-CN")]
    assert "proposal" in capsys.readouterr().out


def test_run_intake_rejects_missing_output_language_before_creating_a_run(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="output_language"):
        run_intake(
            project_dir=tmp_path / "projects" / "demo",
            profiles_path=tmp_path / "missing-profiles.json",
            output_language=" ",
            config=_config(tmp_path),
            runs_root=tmp_path / "runs",
        )

    assert not (tmp_path / "runs").exists()
