"""Publication checks cover the bounded case, JSONL and reader-facing media."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "publication_check", REPO_ROOT / "tools" / "check_publication.py",
)
assert SPEC is not None and SPEC.loader is not None
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)

CASE_PREFIX = "examples/console-case/"
CALL_ID = "call_07fadc9482cd4672aa74992c6b6cf173"
RECORD_COUNTS = {
    "run/ingestion/material_atoms.jsonl": 10,
    "run/ingestion/source_pieces.jsonl": 13,
    "run/ingestion/source_snapshots.jsonl": 1,
    "run/packing/render_contracts.jsonl": 1,
    "run/rendering/chunk_artifacts.jsonl": 1,
    "run/llm/call_records.jsonl": 1,
}
CASE_FILES = {
    "README.md", "case.json", "run/console/manifest.json", "run/packing/chunk_plan.json",
    *RECORD_COUNTS,
    f"run/llm/raw/{CALL_ID}.request.json", f"run/llm/raw/{CALL_ID}.response.json",
}


@pytest.fixture
def public_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(publication, "ROOT", tmp_path)
    (tmp_path / "docs/user/assets").mkdir(parents=True)
    (tmp_path / "README.md").write_text("# Tracecast\n", encoding="utf-8")
    _write_case(tmp_path)
    return tmp_path


def _write_case(root: Path) -> Path:
    case = root / CASE_PREFIX
    for name in CASE_FILES:
        path = case / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" * RECORD_COUNTS.get(name, 1), encoding="utf-8")
    return case


def test_case_allowlist_allows_topic_names_but_not_private_runs(public_root: Path) -> None:
    (public_root / "README.md").write_text("FF6 历史首段，不是完整运行。\n", encoding="utf-8")
    publication.check_public_text_hygiene()
    publication.check_console_case()
    assert all(publication._is_public_path(CASE_PREFIX + name) for name in CASE_FILES)
    for path in (
        "projects/" + "ff6/sources.json",
        "runs/" + "ff6-original/assembly/script.md",
        CASE_PREFIX + "run/assembly/script.md",
        "examples/another-case/case.json",
    ):
        assert not publication._is_public_path(path)


@pytest.mark.parametrize("extra", [
    "run/assembly/script.md", "run/assembly/assembly_report.json", "source/full.txt", ".env",
])
def test_case_rejects_unlisted_run_source_or_environment(
    public_root: Path, extra: str,
) -> None:
    path = public_root / CASE_PREFIX / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("private", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="unexpected"):
        publication.check_console_case()


def test_case_rejects_extra_record_and_missing_dependency(public_root: Path) -> None:
    path = public_root / CASE_PREFIX / "run/rendering/chunk_artifacts.jsonl"
    path.write_text("{}\n{}\n", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="expected 1 excerpt records"):
        publication.check_console_case()
    path.unlink()
    with pytest.raises(publication.PublicationError, match="missing"):
        publication.check_console_case()


@pytest.mark.parametrize("payload,reason", [
    ({"text": "sk-" + "a" * 24}, "key-like value"),
    ({"nested": {"Authorization": "redacted"}}, "private configuration field"),
    ({"api_key_env": "SAVED_AUTHOR_KEY"}, "private configuration field"),
    ({"path": "D:" + r"\private\source.txt"}, "private filesystem path"),
    ({"path": "/home/reader/source.txt"}, "private filesystem path"),
    ({"path": "projects/" + "ff6/source.txt"}, "private filesystem path"),
    ({"cost": 0.25}, "private cost/key metadata"),
    ({"metadata": {"cost_profile": {"output": 0.5}}}, "private cost/key metadata"),
    ({"key_id": "author-key"}, "private cost/key metadata"),
])
def test_case_scans_nested_jsonl_values(
    public_root: Path, payload: dict, reason: str,
) -> None:
    path = public_root / CASE_PREFIX / "run/llm/call_records.jsonl"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match=reason):
        publication.check_console_case()


@pytest.mark.parametrize("raw", [b"not-json\n", b"\xff"])
def test_case_rejects_unreadable_jsonl(public_root: Path, raw: bytes) -> None:
    path = public_root / CASE_PREFIX / "run/llm/call_records.jsonl"
    path.write_bytes(raw)
    with pytest.raises(publication.PublicationError, match="invalid UTF-8 JSON"):
        publication.check_console_case()


def test_jsonl_private_document_references_are_scanned(public_root: Path) -> None:
    path = public_root / "docs/user/payload.jsonl"
    path.write_text(json.dumps({"note": "docs/" + "modules/private.md"}), encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="private documentation"):
        publication.check_internal_references()


@pytest.mark.parametrize("suffix", [".jsonl", ".mjs"])
def test_full_private_case_paths_are_rejected_in_public_text(
    public_root: Path, suffix: str,
) -> None:
    path = public_root / f"docs/user/payload{suffix}"
    path.write_text(json.dumps("runs/" + "ff6-private/output"), encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="private full-case path"):
        publication.check_public_text_hygiene()


@pytest.mark.parametrize("markup", [
    "![excerpt](assets/missing.png)",
    '<video controls src="assets/missing.mp4"></video>',
    '<video poster="assets/missing.png"></video>',
    '<video controls><source src="assets/missing.webm"></video>',
])
def test_missing_public_media_fails_link_check(public_root: Path, markup: str) -> None:
    (public_root / "docs/user/case.md").write_text(markup, encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="missing link target"):
        publication.check_markdown_links()


def test_nested_media_links_with_spaces_are_checked(public_root: Path) -> None:
    (public_root / "docs/user/assets/excerpt image.png").write_bytes(b"image")
    nested = public_root / "docs/user/cases"
    nested.mkdir()
    document = nested / "case.md"
    document.write_text(
        '![excerpt](<../assets/excerpt image.png> "Caption")\n'
        '![excerpt](../assets/excerpt%20image.png)\n', encoding="utf-8",
    )
    publication.check_markdown_links()
    document.write_text("![bad](../../../../outside.png)", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="escapes repository"):
        publication.check_markdown_links()


def test_case_directory_link_requires_complete_closed_file_set(public_root: Path) -> None:
    (public_root / "README.md").write_text(f"[case]({CASE_PREFIX})\n", encoding="utf-8")
    publication.check_markdown_links()
    extra = public_root / CASE_PREFIX / "full_source.txt"
    extra.write_text("private source", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="unexpected full_source.txt"):
        publication.check_markdown_links()
    extra.unlink()
    (public_root / CASE_PREFIX / "run/llm/call_records.jsonl").unlink()
    with pytest.raises(publication.PublicationError, match="missing run/llm/call_records"):
        publication.check_markdown_links()


@pytest.mark.parametrize("target", [
    "examples/", "examples/another/", CASE_PREFIX + "run/",
])
def test_case_directory_exception_does_not_open_other_directories(
    public_root: Path, target: str,
) -> None:
    (public_root / target).mkdir(parents=True, exist_ok=True)
    (public_root / "README.md").write_text(f"[case]({target})\n", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="link targets private file"):
        publication.check_markdown_links()


def test_case_directory_cannot_be_used_as_media(public_root: Path) -> None:
    (public_root / "README.md").write_text(f"![case]({CASE_PREFIX})\n", encoding="utf-8")
    with pytest.raises(publication.PublicationError, match="media target is not a file"):
        publication.check_markdown_links()


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize("name,data,reason", [
    ("examples/console-case/case.json", b"{}", "checkout-only/private data"),
    ("tracecast/records.jsonl", b'{}\n', "runtime JSONL"),
    ("tracecast/.env", b"LOCAL_KEY=value", "local environment"),
    ("docs/user/assets/capture.mp4", b"video", "recording video"),
    ("tracecast/config.json", json.dumps({"key": "sk-" + "x" * 24}).encode(), "private data"),
    ("tracecast/config.json", b'{"path":"/home/author/raw.txt"}', "private data"),
])
def test_distribution_rejects_runtime_records_and_private_payloads(
    public_root: Path, kind: str, name: str, data: bytes, reason: str,
) -> None:
    if kind == "wheel":
        archive = public_root / "example.whl"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(name, data)
        validate = publication._validate_wheel
    else:
        archive = public_root / "example.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            info = tarfile.TarInfo("tracecast-0.1.0/" + name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        validate = publication._validate_sdist_payloads
    with pytest.raises(publication.PublicationError, match=reason):
        validate(archive)
