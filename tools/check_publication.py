"""Validate the public repository boundary and Python distributions.

The check is intentionally standard-library-only at import time. ``python -m
build`` is supplied by the development extra and is invoked only for the
distribution checks.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]

_PUBLIC_EXACT = {
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "LICENSE",
    "README.md",
    "configs/runtime.example.json",
    "configs/source_profile_templates.example.json",
    "hosts/.gitignore",
    "hosts/example_host_zh.json",
    "projects/.gitkeep",
    "runs/.gitignore",
    "pyproject.toml",
}
_PUBLIC_PREFIXES = (
    ".github/workflows/",
    "docs/user/",
    "projects/example/",
    "prompt/",
    "src/",
    "tests/",
    "tools/",
)
_TEXT_SUFFIXES = {
    ".css", ".html", ".js", ".json", ".jsonl", ".md", ".mjs", ".py", ".toml", ".txt",
    ".yaml", ".yml",
}
_MARKDOWN_LINK = re.compile(r"(?P<image>!)?\[[^\]]+\]\((?P<target>[^)]+)\)")
_HTML_MEDIA = re.compile(
    r"<(?:img|video|audio|source)\b[^>]*?\b(?:src|poster)=[\"'](?P<target>[^\"']+)[\"']",
    re.IGNORECASE,
)
_CASE_PREFIX = "examples/console-case/"
_CASE_CALL_ID = "call_07fadc9482cd4672aa74992c6b6cf173"
_CASE_FILES = frozenset({
    "README.md",
    "case.json",
    "run/console/manifest.json",
    "run/ingestion/material_atoms.jsonl",
    "run/ingestion/source_pieces.jsonl",
    "run/ingestion/source_snapshots.jsonl",
    "run/packing/chunk_plan.json",
    "run/packing/render_contracts.jsonl",
    "run/rendering/chunk_artifacts.jsonl",
    "run/llm/call_records.jsonl",
    f"run/llm/raw/{_CASE_CALL_ID}.request.json",
    f"run/llm/raw/{_CASE_CALL_ID}.response.json",
})
_CASE_RECORD_COUNTS = {
    "run/ingestion/material_atoms.jsonl": 10,
    "run/ingestion/source_pieces.jsonl": 13,
    "run/ingestion/source_snapshots.jsonl": 1,
    "run/packing/render_contracts.jsonl": 1,
    "run/rendering/chunk_artifacts.jsonl": 1,
    "run/llm/call_records.jsonl": 1,
}
_KEY_LIKE = re.compile(
    r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{12,}|sk-[A-Za-z0-9]{16,}|"
    r"Bearer\s+[A-Za-z0-9._~-]{16,}",
    re.IGNORECASE,
)
_LOCAL_PATH = re.compile(r"\b[A-Z]:[\\/]|/(?:Users|home)/", re.IGNORECASE)
_PRIVATE_CASE_PATH = re.compile(r"(?:projects[\\/]ff6|runs[\\/]ff6-)", re.IGNORECASE)


class PublicationError(RuntimeError):
    """One or more publication invariants failed."""


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [line.replace("\\", "/") for line in result.stdout.splitlines() if line]


def _is_public_path(path: str) -> bool:
    if path.startswith("examples/"):
        return path.startswith(_CASE_PREFIX) and path[len(_CASE_PREFIX):] in _CASE_FILES
    if path in _PUBLIC_EXACT:
        return True
    if path in {
        "tools/atom_boundary_sidecar.py",
        "tests/test_atom_boundary_sidecar.py",
    }:
        return False
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def check_tracked_paths() -> None:
    tracked = _git_lines("ls-files")
    unexpected = sorted(path for path in tracked if not _is_public_path(path))
    if unexpected:
        raise PublicationError(
            "tracked files fall outside the public allowlist:\n  "
            + "\n  ".join(unexpected)
        )


def _public_files() -> list[Path]:
    files = [ROOT / relative for relative in _PUBLIC_EXACT if (ROOT / relative).is_file()]
    for prefix in (*_PUBLIC_PREFIXES, _CASE_PREFIX):
        root = ROOT / prefix.rstrip("/")
        if root.is_dir():
            files.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and _is_public_path(path.relative_to(ROOT).as_posix())
            )
    return files


def check_internal_references() -> None:
    self_path = Path(__file__).resolve()
    private_names = (
        "AGENTS.md",
        "architecture.md",
        "assembly-tts.md",
        "backlog.md",
        "console.md",
        "contracts.md",
        "current-state.md",
        "decisions.md",
        "documentation-guide.md",
        "host-builder.md",
        "hosts.md",
        "ingestion.md",
        "intake.md",
        "lessons-learned.md",
        "llm-observer.md",
        "name-resolution.md",
        "node-edit.md",
        "packing.md",
        "planning-selection.md",
        "prompt-construction.zh.md",
        "rendering.md",
        "roadmap.md",
        "validation.md",
    )
    private_paths = (
        "docs/history/",
        "docs/modules/",
        "prompt-decisions/",
    )
    findings: list[str] = []
    for path in _public_files():
        if path.resolve() == self_path or path.suffix.casefold() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(name in line for name in private_names) or any(
                fragment in line for fragment in private_paths
            ):
                findings.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
    if findings:
        raise PublicationError(
            "public files reference private documentation:\n  " + "\n  ".join(findings)
        )


def check_public_text_hygiene() -> None:
    self_path = Path(__file__).resolve()
    bare_pip = re.compile(r"(?<!-m )\bpip\s+install\b", re.IGNORECASE)
    findings: list[str] = []
    for path in _public_files():
        if path.resolve() == self_path or path.suffix.casefold() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            reasons: list[str] = []
            if bare_pip.search(line):
                reasons.append("bare pip command")
            if _PRIVATE_CASE_PATH.search(line):
                reasons.append("private full-case path")
            if reasons:
                findings.append(
                    f"{path.relative_to(ROOT)}:{line_number} ({', '.join(reasons)}): "
                    f"{line.strip()}"
                )
    if findings:
        raise PublicationError(
            "public files contain publication-only residue:\n  " + "\n  ".join(findings)
        )


def _split_link_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1:target.index(">")]
    else:
        target = target.split(" ", 1)[0]
    return unquote(target.split("#", 1)[0].split("?", 1)[0])


def check_markdown_links() -> None:
    documents = [
        ROOT / "README.md", *sorted((ROOT / "docs" / "user").rglob("*.md")),
        ROOT / _CASE_PREFIX / "README.md",
    ]
    findings: list[str] = []
    for document in documents:
        if not document.is_file():
            findings.append(f"missing public document: {document.relative_to(ROOT)}")
            continue
        text = document.read_text(encoding="utf-8")
        matches = [
            *((match, bool(match.group("image"))) for match in _MARKDOWN_LINK.finditer(text)),
            *((match, True) for match in _HTML_MEDIA.finditer(text)),
        ]
        for match, is_media in matches:
            raw_target = match.group("target")
            target = _split_link_target(raw_target)
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            try:
                relative = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                findings.append(
                    f"{document.relative_to(ROOT)}: link escapes repository: {raw_target}"
                )
                continue
            if not resolved.exists():
                findings.append(f"{document.relative_to(ROOT)}: missing link target: {raw_target}")
            elif is_media and not resolved.is_file():
                findings.append(
                    f"{document.relative_to(ROOT)}: media target is not a file: {raw_target}"
                )
            elif relative == _CASE_PREFIX.rstrip("/") and resolved.is_dir():
                try:
                    check_console_case()
                except PublicationError as exc:
                    findings.append(
                        f"{document.relative_to(ROOT)}: invalid case directory: {raw_target}: {exc}"
                    )
            elif not _is_public_path(relative):
                findings.append(
                    f"{document.relative_to(ROOT)}: link targets private file: {raw_target}"
                )
    if findings:
        raise PublicationError("invalid public Markdown links:\n  " + "\n  ".join(findings))


def check_public_examples() -> None:
    candidates = [
        ROOT / ".env.example",
        ROOT / "configs" / "runtime.example.json",
        ROOT / "configs" / "source_profile_templates.example.json",
        ROOT / "hosts" / "example_host_zh.json",
        ROOT / "projects" / "example" / "sources.json",
    ]
    findings = [
        str(path.relative_to(ROOT))
        for path in candidates
        if path.is_file() and _KEY_LIKE.search(path.read_text(encoding="utf-8"))
    ]
    if findings:
        raise PublicationError("public examples contain key-like values: " + ", ".join(findings))


def check_console_case() -> None:
    """Allow one reviewed excerpt, not arbitrary projects, runs or source dumps."""
    root = ROOT / _CASE_PREFIX
    files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    for name in files:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise PublicationError(f"{name}: case resource must be a local regular file")
    _validate_case_payloads({name: (root / name).read_bytes() for name in files})


def _validate_case_payloads(payloads: dict[str, bytes]) -> None:
    missing = sorted(_CASE_FILES - payloads.keys())
    unexpected = sorted(payloads.keys() - _CASE_FILES)
    if missing or unexpected:
        details = [*(f"missing {name}" for name in missing)]
        details.extend(f"unexpected {name}" for name in unexpected)
        raise PublicationError("console case file boundary failed:\n  " + "\n  ".join(details))
    findings: list[str] = []

    def inspect(value: object, filename: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.casefold()
                if normalized in {"api_key", "authorization", "api_key_env", "base_url",
                                  "gateway_url"}:
                    findings.append(f"{filename}: private configuration field {key}")
                if normalized in {"cost", "cost_kind", "cost_profile", "key_id"} and (
                    child is not None
                ):
                    findings.append(f"{filename}: private cost/key metadata {key}")
                inspect(child, filename)
        elif isinstance(value, list):
            for child in value:
                inspect(child, filename)
        elif isinstance(value, str):
            if _KEY_LIKE.search(value):
                findings.append(f"{filename}: key-like value")
            if _LOCAL_PATH.search(value) or _PRIVATE_CASE_PATH.search(value):
                findings.append(f"{filename}: private filesystem path")

    for name, data in sorted(payloads.items()):
        try:
            raw = data.decode("utf-8")
            if name.endswith(".jsonl"):
                values = [json.loads(line) for line in raw.splitlines() if line.strip()]
            elif name.endswith(".json"):
                values = [json.loads(raw)]
            else:
                values = [raw]
        except (ValueError, UnicodeError) as exc:
            findings.append(f"{name}: invalid UTF-8 JSON: {exc}")
            continue
        if name in _CASE_RECORD_COUNTS and len(values) != _CASE_RECORD_COUNTS[name]:
            findings.append(f"{name}: expected {_CASE_RECORD_COUNTS[name]} excerpt records")
        for value in values:
            inspect(value, name)
    if findings:
        raise PublicationError("console case contains private data:\n  " + "\n  ".join(findings))


def _validate_archive_payloads(payloads: dict[str, bytes]) -> None:
    """The reusable case is checkout-only; distributions contain no run records."""
    findings: list[str] = []
    for name, data in payloads.items():
        parts = PurePosixPath(name).parts
        if any(part.startswith(".env") and part != ".env.example" for part in parts):
            findings.append(f"{name}: distribution contains local environment configuration")
        if PurePosixPath(name).suffix.casefold() in {".mp4", ".webm", ".mov"}:
            findings.append(f"{name}: distribution contains recording video")
        if any(part in {"examples", "runs", "projects"} for part in parts) and not (
            name.startswith("projects/example/")
        ):
            findings.append(f"{name}: distribution contains checkout-only/private data")
        if name.endswith(".jsonl"):
            findings.append(f"{name}: distribution contains runtime JSONL records")
        if PurePosixPath(name).suffix.casefold() in {".json", ".jsonl"}:
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                findings.append(f"{name}: invalid UTF-8 data")
                continue
            if (
                _KEY_LIKE.search(text) or _LOCAL_PATH.search(text)
                or _PRIVATE_CASE_PATH.search(text)
            ):
                findings.append(f"{name}: distribution contains private data")
    if findings:
        raise PublicationError("distribution data boundary failed:\n  " + "\n  ".join(findings))


def _validate_sdist_payloads(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        payloads = {}
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            relative = PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
            stream = bundle.extractfile(member)
            if stream is not None:
                payloads[relative] = stream.read()
    _validate_archive_payloads(payloads)


def _run_build(source: Path, outdir: Path, *targets: str) -> None:
    command = [
        sys.executable,
        "-m",
        "build",
        "--no-isolation",
        "--outdir",
        str(outdir),
        *targets,
        str(source),
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise PublicationError(
            "distribution build failed:\n" + result.stdout + result.stderr
        )


def _sdist_files(archive: Path) -> set[str]:
    with tarfile.open(archive, "r:gz") as bundle:
        names: set[str] = set()
        for member in bundle.getmembers():
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts
            if len(parts) > 1:
                names.add(PurePosixPath(*parts[1:]).as_posix())
        return names


def _validate_sdist(files: set[str]) -> None:
    forbidden = (
        ".github/",
        ".claude/",
        "docs/" + "architecture.md",
        "docs/" + "contracts.md",
        "docs/" + "history/",
        "docs/" + "modules/",
        "prompt-" + "decisions/",
        "tests/",
        "tools/",
        "hosts/research/",
    )
    leaked = sorted(
        path for path in files if path == "AGENTS" + ".md" or path.startswith(forbidden)
    )
    if leaked:
        raise PublicationError("sdist contains excluded files:\n  " + "\n  ".join(leaked))
    expected = {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "configs/runtime.example.json",
        "configs/source_profile_templates.example.json",
        "hosts/example_host_zh.json",
        "projects/example/sources.json",
        "docs/user/getting-started.md",
        "docs/user/configuration.md",
        "docs/user/privacy-cost-safety.md",
    }
    missing = sorted(expected - files)
    if missing or not any(path.startswith("src/tracecast/") for path in files) or not any(
        path.startswith("prompt/") for path in files
    ):
        details = [*(f"missing {path}" for path in missing)]
        if not any(path.startswith("src/tracecast/") for path in files):
            details.append("missing src/tracecast/")
        if not any(path.startswith("prompt/") for path in files):
            details.append("missing prompt/")
        raise PublicationError("sdist is incomplete:\n  " + "\n  ".join(details))


def _validate_wheel(archive: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        files = set(bundle.namelist())
        _validate_archive_payloads({
            path: bundle.read(path) for path in files if not path.endswith("/")
        })
    expected_prompt = {
        f"tracecast/prompt/{path.relative_to(ROOT / 'prompt').as_posix()}"
        for path in (ROOT / "prompt").rglob("*")
        if path.is_file()
    }
    expected_console = {
        f"tracecast/{path.relative_to(ROOT / 'src' / 'tracecast').as_posix()}"
        for root in (
            ROOT / "src" / "tracecast" / "console" / "resources",
            ROOT / "src" / "tracecast" / "console" / "starters",
            ROOT / "src" / "tracecast" / "console" / "static",
        )
        for path in root.rglob("*")
        if path.is_file()
    }
    missing = sorted((expected_prompt | expected_console) - files)
    licenses = [path for path in files if path.endswith(".dist-info/licenses/LICENSE")]
    if missing or not licenses:
        details = [*(f"missing {path}" for path in missing)]
        if not licenses:
            details.append("missing .dist-info/licenses/LICENSE")
        raise PublicationError("wheel is incomplete:\n  " + "\n  ".join(details))
    forbidden = [
        path
        for path in files
        if path.startswith(("tests/", "tools/", "docs/", "prompt-" + "decisions/"))
    ]
    if forbidden:
        raise PublicationError("wheel contains development files:\n  " + "\n  ".join(forbidden))


def check_distributions() -> None:
    with tempfile.TemporaryDirectory(prefix="tracecast-publication-") as temp_name:
        temp = Path(temp_name)
        artifacts = temp / "artifacts"
        artifacts.mkdir()
        _run_build(ROOT, artifacts, "--sdist", "--wheel")
        sdists = list(artifacts.glob("*.tar.gz"))
        wheels = list(artifacts.glob("*.whl"))
        if len(sdists) != 1 or len(wheels) != 1:
            raise PublicationError("build must produce exactly one sdist and one wheel")
        _validate_sdist(_sdist_files(sdists[0]))
        _validate_sdist_payloads(sdists[0])
        _validate_wheel(wheels[0])

        unpacked = temp / "unpacked"
        unpacked.mkdir()
        with tarfile.open(sdists[0], "r:gz") as bundle:
            bundle.extractall(unpacked, filter="data")
        source_dirs = [path for path in unpacked.iterdir() if path.is_dir()]
        if len(source_dirs) != 1:
            raise PublicationError("sdist must contain one source root")
        rebuilt = temp / "rebuilt"
        rebuilt.mkdir()
        _run_build(source_dirs[0], rebuilt, "--wheel")
        rebuilt_wheels = list(rebuilt.glob("*.whl"))
        if len(rebuilt_wheels) != 1:
            raise PublicationError("sdist rebuild must produce exactly one wheel")
        _validate_wheel(rebuilt_wheels[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-repository-boundary",
        action="store_true",
        help="Skip git tracked-file allowlist while a private-file index cleanup is pending.",
    )
    args = parser.parse_args(argv)
    checks = [
        check_internal_references,
        check_public_text_hygiene,
        check_markdown_links,
        check_public_examples,
        check_console_case,
        check_distributions,
    ]
    if not args.skip_repository_boundary:
        checks.insert(0, check_tracked_paths)
    try:
        for check in checks:
            check()
    except (OSError, PublicationError, subprocess.SubprocessError) as exc:
        print(f"publication check failed: {exc}", file=sys.stderr)
        return 1
    print("publication check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
