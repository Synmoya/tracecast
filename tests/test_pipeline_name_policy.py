"""Project-level output-language and optional name-override contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracecast.domain import NameOverride
from tracecast.name_resolution import NameResolutionMode
from tracecast.project import load_project, override_script_mismatch_reports


def _write_project(
    tmp_path: Path,
    *,
    output_language: object = "zh-CN",
    extra: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    spec: dict[str, object] = {
        "project_id": "demo",
        "output_language": output_language,
        "sources": [],
        **(extra or {}),
    }
    (project / "sources.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8"
    )
    profiles = tmp_path / "profiles.json"
    profiles.write_text('{"profiles":{}}', encoding="utf-8")
    return project, profiles


def test_project_requires_explicit_non_empty_output_language(tmp_path: Path) -> None:
    project, profiles = _write_project(tmp_path, output_language="")
    with pytest.raises(ValueError, match="output_language"):
        load_project(project, profiles)

    spec_path = project / "sources.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec.pop("output_language")
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="output_language"):
        load_project(project, profiles)


def test_project_loads_output_language_without_requiring_name_overrides(
    tmp_path: Path,
) -> None:
    project, profiles = _write_project(tmp_path, output_language="en")

    loaded = load_project(project, profiles)

    assert loaded.output_language == "en"
    assert loaded.name_overrides == []
    assert not hasattr(loaded, "persona")


def test_legacy_relation_scan_project_migrates_to_overrides_only_on_read(
    tmp_path: Path,
) -> None:
    project, profiles = _write_project(
        tmp_path,
        extra={"name_resolution_mode": "relation_scan"},
    )

    loaded = load_project(project, profiles)

    assert loaded.name_resolution_mode is NameResolutionMode.overrides_only


def test_project_loads_optional_inline_name_overrides(tmp_path: Path) -> None:
    project, profiles = _write_project(
        tmp_path,
        extra={
            "name_overrides": [
                {
                    "aliases": ["Terra", "Tina"],
                    "target_surface": "蒂娜",
                    "output_language": "zh-CN",
                }
            ]
        },
    )

    [override] = load_project(project, profiles).name_overrides

    assert override.aliases == ["Terra", "Tina"]
    assert override.target_surface == "蒂娜"
    assert override.output_language == "zh-CN"


def test_project_loads_override_file_entries(tmp_path: Path) -> None:
    project, profiles = _write_project(
        tmp_path, extra={"name_overrides_path": "names.json"}
    )
    (project / "names.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "aliases": ["未知名"],
                        "target_surface": "Vaelor",
                        "output_language": "en",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    [override] = load_project(project, profiles).name_overrides

    assert override.target_surface == "Vaelor"


def test_project_rejects_conflicting_targets_for_same_language_alias(
    tmp_path: Path,
) -> None:
    project, profiles = _write_project(
        tmp_path,
        extra={
            "name_overrides": [
                {
                    "aliases": ["Terra"],
                    "target_surface": "蒂娜",
                    "output_language": "zh-CN",
                },
                {
                    "aliases": ["Terra"],
                    "target_surface": "泰拉",
                    "output_language": "zh-CN",
                },
            ]
        },
    )

    with pytest.raises(ValueError, match="conflict"):
        load_project(project, profiles)


def test_override_script_mismatch_reports_flags_non_cjk_target_for_cjk_output_language() -> None:
    override = NameOverride(aliases=["Terra"], target_surface="Terra", output_language="zh-CN")

    [report] = override_script_mismatch_reports([override])

    assert report.kind == "name_override_script_mismatch"
    assert report.level == "warning"
    assert report.usable is True
    assert "Terra" in report.message


def test_override_script_mismatch_reports_empty_for_plausible_or_non_cjk_overrides() -> None:
    plausible = NameOverride(aliases=["Terra"], target_surface="泰拉", output_language="zh-CN")
    non_cjk_target = NameOverride(
        aliases=["未知名"], target_surface="Vaelor", output_language="en"
    )

    assert override_script_mismatch_reports([plausible, non_cjk_target]) == []
