"""Reusable Host profile loading, validation, and projection regressions."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracecast.hosts import HostProfile, list_hosts, load_host, require_host_language


def _payload(host_id: str = "host_a", *, output_language: str = "zh-CN") -> dict:
    examples = {
        position: [
            {
                "source": f"{position} source {index}",
                "render": f"{position} render {index}",
                "lesson": f"{position} lesson {index}",
            }
            for index in (1, 2)
        ]
        for position in ("opening", "middle", "ending", "single")
    }
    return {
        "schema_version": "tracecast-host/1",
        "host_id": host_id,
        "display_name": f"Host {host_id}",
        "version": f"{host_id}@v1",
        "output_language": output_language,
        "host_thinking": {
            "viewpoint": f"{host_id} 的观察角度",
            "prefer": ["人物选择"],
            "avoid": ["材料外推断"],
            "human_directive": ["先讲清因果"],
            "rules": ["区分材料与判断"],
        },
        "host_expression": {
            "description": f"{host_id} 的表达方式",
            "rules": ["自然口语"],
        },
        "style_examples_by_position": examples,
    }


def _v2_payload(host_id: str = "host_v2", *, output_language: str = "zh-CN") -> dict:
    payload = _payload(host_id, output_language=output_language)
    payload["schema_version"] = "tracecast-host/2"
    del payload["style_examples_by_position"]["single"]
    for examples in payload["style_examples_by_position"].values():
        for example in examples:
            del example["lesson"]
    return payload


def _write_host(root: Path, payload: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{payload['host_id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_one_host_file_loads_as_one_atomic_profile_and_two_stage_projections(
    tmp_path: Path,
) -> None:
    hosts_dir = tmp_path / "hosts"
    _write_host(hosts_dir, _payload())

    host = load_host(hosts_dir, "host_a")
    selection = host.selection_persona()
    snapshot = host.snapshot()

    assert host.host_id == "host_a"
    assert selection["persona_id"] == "host_a"
    assert selection["viewpoint"] == "host_a 的观察角度"
    assert selection["viewpoint_profile"] == {
        "prefer": ["人物选择"],
        "avoid": ["材料外推断"],
        "human_directive": ["先讲清因果"],
    }
    assert selection["thinking_rules"] == ["区分材料与判断"]
    assert selection["expression"] == "host_a 的表达方式"
    assert "style_examples_by_position" not in selection
    assert snapshot["host_id"] == "host_a"
    assert snapshot["profile"] == host.model_dump(mode="json")
    assert snapshot["content_hash"] == host.snapshot()["content_hash"]


def test_host_catalog_is_filename_sorted_and_missing_directory_is_empty(
    tmp_path: Path,
) -> None:
    hosts_dir = tmp_path / "hosts"
    _write_host(hosts_dir, _payload("z_host"))
    _write_host(hosts_dir, _payload("a_host"))

    assert [host.host_id for host in list_hosts(hosts_dir)] == ["a_host", "z_host"]
    assert list_hosts(tmp_path / "missing") == []


@pytest.mark.parametrize("host_id", ["", ".", "..", "../host", "group/host", r"group\host"])
def test_load_host_rejects_unsafe_ids(tmp_path: Path, host_id: str) -> None:
    with pytest.raises(ValueError, match="unsafe host id"):
        load_host(tmp_path, host_id)


def test_load_host_rejects_filename_identity_mismatch(tmp_path: Path) -> None:
    hosts_dir = tmp_path / "hosts"
    payload = _payload("declared")
    hosts_dir.mkdir()
    (hosts_dir / "requested.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="declares host_id.*expected"):
        load_host(hosts_dir, "requested")


@pytest.mark.parametrize("case", ["missing_position", "extra_position", "one_example"])
def test_v1_host_profile_requires_exactly_two_examples_at_all_four_positions(
    case: str,
) -> None:
    payload = deepcopy(_payload())
    examples = payload["style_examples_by_position"]
    if case == "missing_position":
        del examples["middle"]
    elif case == "extra_position":
        examples["epilogue"] = examples["ending"]
    else:
        examples["middle"] = examples["middle"][:1]

    with pytest.raises(ValidationError):
        HostProfile.model_validate(payload)


def test_v2_host_profile_accepts_non_empty_variable_sized_position_pools() -> None:
    payload = _v2_payload()
    examples = payload["style_examples_by_position"]
    examples["opening"] = examples["opening"][:1]
    examples["middle"].extend(deepcopy(examples["middle"][0]) for _ in range(2))
    examples["ending"].extend(deepcopy(examples["ending"][0]) for _ in range(4))

    host = HostProfile.model_validate(payload)

    assert host.schema_version == "tracecast-host/2"
    assert set(host.style_examples_by_position) == {"opening", "middle", "ending"}
    assert {
        position: len(rows)
        for position, rows in host.style_examples_by_position.items()
    } == {"opening": 1, "middle": 4, "ending": 6}
    assert host.snapshot()["profile"] == host.model_dump(mode="json")


@pytest.mark.parametrize("case", ["missing_position", "single_position", "empty_pool"])
def test_v2_host_profile_rejects_missing_extra_or_empty_positions(case: str) -> None:
    payload = _v2_payload()
    examples = payload["style_examples_by_position"]
    if case == "missing_position":
        del examples["middle"]
    elif case == "single_position":
        examples["single"] = deepcopy(examples["opening"])
    else:
        examples["ending"] = []

    with pytest.raises(ValidationError):
        HostProfile.model_validate(payload)


def test_v1_owns_the_style_example_lesson_and_v2_rejects_it() -> None:
    """The lesson field belongs to hand-authored v1 snapshots only.

    A compiled v2 example cannot ground a per-example lesson, so carrying one
    would put an unverifiable directive into every render prompt.
    """

    v1 = HostProfile.model_validate(_payload())
    assert all(
        example.lesson is not None
        for examples in v1.style_examples_by_position.values()
        for example in examples
    )
    v2 = HostProfile.model_validate(_v2_payload())
    assert all(
        example.lesson is None
        for examples in v2.style_examples_by_position.values()
        for example in examples
    )

    stale = _v2_payload()
    stale["style_examples_by_position"]["ending"][0]["lesson"] = "结尾学习点"
    with pytest.raises(ValidationError, match="rebuild the host"):
        HostProfile.model_validate(stale)

    incomplete = deepcopy(_payload())
    del incomplete["style_examples_by_position"]["middle"][1]["lesson"]
    with pytest.raises(ValidationError, match="must carry a non-empty lesson"):
        HostProfile.model_validate(incomplete)


def test_host_profile_rejects_unknown_schema_versions() -> None:
    payload = _v2_payload()
    payload["schema_version"] = "tracecast-host/3"

    with pytest.raises(ValidationError):
        HostProfile.model_validate(payload)


def test_host_language_must_match_project_output_language() -> None:
    host = HostProfile.model_validate(_payload(output_language="zh-CN"))

    require_host_language(host, "zh-CN")
    with pytest.raises(ValueError, match="targets 'zh-CN'.*project targets 'en'"):
        require_host_language(host, "en")
