"""Focused ingestion orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tracecast.domain import (
    ContentKind,
    LlmCallRecord,
    ProviderStatus,
    SourceSnapshot,
    TrustTier,
    estimate_tokens,
)
from tracecast.ingestion import (
    IngestionBudgets,
    IngestResult,
    SourceProfile,
    ingest_source,
    split_source,
)
from tracecast.llm import LlmTransportError
from tracecast.storage import RunStore, RuntimeConfig, content_hash


def _write_prompt(path: Path, version: str, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<!-- version: {version} -->\n{body}", encoding="utf-8")
    return str(path)


def _record(index: int, prompt_version: str | None) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=f"call_{index}",
        stage="atomizer",
        operation_route="atom_boundary_dsl",
        provider="fake",
        model="fake",
        input_hash=f"hash_{index}",
        provider_status=ProviderStatus.success,
        created_at=datetime.now(UTC),
        prompt_version=prompt_version,
    )


def _runtime_config(tmp_path: Path) -> RuntimeConfig:
    prompt_dir = tmp_path / "prompt" / "ingestion"
    system_prompt = _write_prompt(prompt_dir / "atom.system.md", "sys@test", "system")
    user_prompt = _write_prompt(
        prompt_dir / "atom.user.md",
        "user@test",
        "aliases: {{alias_range}}\n"
        "kinds:\n{{allowed_content_kinds}}\n"
        "{{content_kind_descriptions}}\n"
        "tags:\n{{allowed_tags}}\n"
        "{{tag_descriptions}}\n"
        "pieces:\n{{piece_lines}}",
    )
    repair_prompt = _write_prompt(
        prompt_dir / "atom.repair.md",
        "repair@test",
        "repair {{alias_range}}\n{{violations}}",
    )
    return RuntimeConfig.model_validate(
        {
            "providers": {"fake": {"base_url": "https://fake.invalid", "keys": []}},
            "routes": {
                "atom_boundary_dsl": {
                    "stage": "atomizer",
                    "provider": "fake",
                    "model": "fake",
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "repair_prompt": repair_prompt,
                    "max_concurrency": 1,
                }
            },
            "stage_budgets": {
                "ingestion": {
                    "source_piece_max_tokens": 3,
                    "atom_window_max_input_tokens": 1000,
                    "pieces_per_call_soft_limit": 8,
                }
            },
        }
    )


def _snapshot(raw_text: str) -> SourceSnapshot:
    return SourceSnapshot(
        source_id="src",
        source_type="script",
        source_class="transcript",
        trust_tier=TrustTier.canonical,
        content_hash=content_hash(raw_text),
    )


def test_ingest_source_repairs_one_connected_partition_gap(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\ndddddddddd"
    messages_seen: list[list[dict[str, Any]]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        messages_seen.append(kwargs["messages"])
        index = len(messages_seen)
        if index == 1:
            text = (
                "A | p0001 | opinion | \n"
                "A | p0003-p0004 | opinion | \n"
            )
        else:
            text = "A | p0001 | opinion | \n"
        return text, _record(index, kwargs.get("prompt_version"))

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "block-repair"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert result.ok
    assert len(result.call_records) == 2
    assert result.call_records[0].prompt_version == (
        "sys@test+user@test+ingestion_content_kinds@v2+ingestion_tags@v1"
    )
    assert result.call_records[1].prompt_version == (
        "sys@test+user@test+ingestion_content_kinds@v2+ingestion_tags@v1+repair@test"
    )
    assert [atom.provenance.piece_refs for atom in result.atoms] == [
        ["src:000000"],
        ["src:000001"],
        ["src:000002", "src:000003"],
    ]
    repaired = next(
        item for item in result.reports if item.kind == "window_partition_repaired"
    )
    assert repaired.usable is True
    assert repaired.metadata["reason_code"] == "connected_partition_repaired"
    repair_projection = messages_seen[1][1]["content"]
    assert "aaaaaaaaaa" not in repair_projection
    assert "bbbbbbbbbb" in repair_projection
    assert "cccccccccc" not in repair_projection
    assert "dddddddddd" not in repair_projection


@pytest.mark.parametrize(
    "bad_row",
    [
        "A | p0002-p0003 | opinion | | attr:Legacy Author",
        "A | p0002-p0003 | narration | ",
    ],
)
def test_protocol_or_cell_failure_does_not_authorize_boundary_repair(
    tmp_path: Path, bad_row: str
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\ndddddddddd"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "A | p0001 | opinion | \n"
            f"{bad_row}\n"
            "A | p0004 | opinion | \n",
            _record(len(calls), kwargs.get("prompt_version")),
        )

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "protocol-hard-fail"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert len(calls) == 1
    assert result.atoms == []
    failure = next(item for item in result.items if item.kind == "window_protocol_failed")
    assert "after 1 model call(s)" in failure.message
    assert "cannot authorize new atom boundaries" in failure.message
    assert failure.piece_refs == tuple(piece.piece_id for piece in result.pieces)
    assert not result.ok
    audit_failure = next(
        item for item in result.reports if item.kind == "window_protocol_failed"
    )
    assert audit_failure.usable is False


def test_empty_response_is_protocol_hard_failure_without_partition_repair(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return "", _record(len(calls), kwargs.get("prompt_version"))

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "empty-response-hard-fail"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert len(calls) == 1
    assert result.atoms == []
    assert not result.ok
    failure = next(item for item in result.items if item.kind == "window_protocol_failed")
    assert "no atom rows" in failure.message
    assert "cannot authorize new atom boundaries" in failure.message
    assert failure.metadata["reason_code"] == "no_atom_rows"
    assert next(
        item for item in result.reports if item.kind == "window_protocol_failed"
    ).usable is False


def test_empty_source_is_blocking_precondition_without_model_call(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        raise AssertionError("empty source must fail before a model call")

    result = ingest_source(
        snapshot=_snapshot(" \n\t"),
        raw_text=" \n\t",
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "empty-source"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert calls == []
    assert result.pieces == []
    assert result.windows == []
    assert result.atoms == []
    assert not result.ok
    [failure] = result.reports
    assert failure.kind == "source_precondition_failed"
    assert failure.metadata["reason_code"] == "empty_source"
    assert failure.usable is False


def test_declared_sha_mismatch_is_blocking_precondition_before_model_call(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "actual source text"
    snapshot = _snapshot("different source text")
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        raise AssertionError("hash mismatch must fail before a model call")

    result = ingest_source(
        snapshot=snapshot,
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "hash-mismatch"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert calls == []
    assert result.pieces == []
    assert result.windows == []
    assert result.atoms == []
    assert not result.ok
    [failure] = result.reports
    assert failure.kind == "source_precondition_failed"
    assert failure.metadata["reason_code"] == "content_hash_mismatch"
    assert failure.usable is False


def test_unknown_tag_is_dropped_without_boundary_repair(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "A | p0001-p0002 | opinion | invented_tag\n",
            _record(len(calls), kwargs.get("prompt_version")),
        )

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "unknown-tag"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert result.ok
    assert len(calls) == 1
    assert len(result.atoms) == 1
    assert result.atoms[0].tags == []
    assert [item.kind for item in result.items] == ["unknown_tag"]
    assert result.reports[0].usable is True


def test_disconnected_partition_failures_hard_fail_without_widening(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\ndddddddddd\neeeeeeeeee"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        return (
            "A | p0001 | opinion | \n"
            "A | p0003 | opinion | \n"
            "A | p0005 | opinion | \n",
            _record(len(calls), kwargs.get("prompt_version")),
        )

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "disconnected-hard-fail"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert len(calls) == 1
    assert result.atoms == []
    failure = next(item for item in result.items if item.kind == "window_partition_failed")
    assert "partition failure spans are disconnected" in failure.message
    assert "bbbbbbbbbb" in calls[0]["messages"][1]["content"]
    assert "dddddddddd" in calls[0]["messages"][1]["content"]


def test_connected_partition_failure_gets_only_one_repair_attempt(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\ndddddddddd"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                "A | p0001 | opinion | \nA | p0003-p0004 | opinion | \n",
                _record(1, kwargs.get("prompt_version")),
            )
        if len(calls) == 2:
            return "", _record(2, kwargs.get("prompt_version"))
        raise AssertionError("Ingestion must not recursively repair a repair response")

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "one-repair-attempt"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert len(calls) == 2
    assert [call["work_unit"].kind for call in calls] == [
        "ingestion_window",
        "ingestion_repair",
    ]
    assert result.atoms == []
    assert not result.ok
    failure = next(item for item in result.items if item.kind == "window_partition_failed")
    assert "block repair failed" in failure.message
    assert next(
        item for item in result.reports if item.kind == "window_partition_failed"
    ).usable is False


def test_repair_transport_failure_reports_the_discarded_full_window(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb\ncccccccccc\ndddddddddd"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                "A | p0001 | opinion | \nA | p0003-p0004 | opinion | \n",
                _record(1, kwargs.get("prompt_version")),
            )
        failed = _record(2, kwargs.get("prompt_version")).model_copy(
            update={"provider_status": ProviderStatus.error}
        )
        raise LlmTransportError("repair offline", record=failed, attempts=1)

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "repair-transport"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )

    assert len(calls) == 2
    repair_unit = calls[1]["work_unit"]
    assert repair_unit.input_refs == ["src:000001"]
    failure = next(item for item in result.items if item.kind == "window_transport_failed")
    assert failure.call_id == "call_2"
    assert failure.piece_refs == tuple(piece.piece_id for piece in result.pieces)
    assert "localized partition repair" in failure.message
    assert not result.ok
    aggregate = IngestResult(sources=[result])
    assert not aggregate.ok
    assert next(
        item for item in aggregate.reports if item.kind == "window_transport_failed"
    ).usable is False


def test_initial_transport_failure_makes_source_and_stage_unusable(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    raw_text = "aaaaaaaaaa\nbbbbbbbbbb"
    calls: list[dict[str, Any]] = []

    def fake_call(**kwargs: Any) -> tuple[str, LlmCallRecord]:
        calls.append(kwargs)
        failed = _record(1, kwargs.get("prompt_version")).model_copy(
            update={"provider_status": ProviderStatus.error}
        )
        raise LlmTransportError("window offline", record=failed, attempts=1)

    result = ingest_source(
        snapshot=_snapshot(raw_text),
        raw_text=raw_text,
        profile=SourceProfile.build(allowed_content_kinds=[ContentKind.opinion]),
        source_language="en",
        config=config,
        store=RunStore.for_run(tmp_path / "runs", "window-transport"),
        budgets=IngestionBudgets.from_config(config),
        call=fake_call,
    )
    aggregate = IngestResult(sources=[result])

    assert len(calls) == 1
    assert result.atoms == []
    assert not result.ok
    assert not aggregate.ok
    failure = next(
        item for item in aggregate.reports if item.kind == "window_transport_failed"
    )
    assert failure.usable is False
    assert failure.trace_refs == [
        "src",
        *(piece.piece_id for piece in result.pieces),
    ]


def test_repository_repair_prompt_is_partition_only() -> None:
    prompt_path = (
        Path(__file__).resolve().parents[1]
        / "prompt"
        / "ingestion"
        / "atom_boundary_dsl.repair.zh.md"
    )
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "<!-- version: atom_boundary_dsl_repair@v3 -->" in prompt
    assert "每条 A 行字段都符合协议" in prompt
    assert "块外已通过的 A 行保持锁定" in prompt


def test_split_source_uses_token_ceiling_and_never_cuts_normal_english_words() -> None:
    raw_text = (
        "Long ago, magic vanished from the world. "
        "A thousand years later, steam engines returned."
    )

    pieces = split_source("src", raw_text, max_tokens=8)

    assert "".join(piece.text for piece in pieces) == raw_text
    assert len(pieces) > 1
    assert all(estimate_tokens(piece.text) <= 8 for piece in pieces)
    for left, right in zip(pieces, pieces[1:], strict=False):
        assert not (left.text[-1].isalnum() and right.text[0].isalnum())


def test_split_source_treats_ascii_period_as_a_sentence_boundary() -> None:
    raw_text = "First sentence. Second sentence continues for a while."

    pieces = split_source("src", raw_text, max_tokens=4)

    assert pieces[0].text == "First sentence."
    assert "".join(piece.text for piece in pieces) == raw_text
