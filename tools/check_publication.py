"""Validate the public repository boundary and Python distributions.

The check is intentionally standard-library-only at import time. ``python -m
build`` is supplied by the development extra and is invoked only for the
distribution checks.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

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
_TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")


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
    for prefix in _PUBLIC_PREFIXES:
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
    private_example = re.compile(r"\bff6\b", re.IGNORECASE)
    findings: list[str] = []
    for path in _public_files():
        if path.resolve() == self_path or path.suffix.casefold() not in _TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            reasons: list[str] = []
            if bare_pip.search(line):
                reasons.append("bare pip command")
            if private_example.search(line):
                reasons.append("private example identifier")
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
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if " " in target:
        target = target.split(" ", 1)[0]
    return target.split("#", 1)[0]


def check_markdown_links() -> None:
    documents = [ROOT / "README.md", *sorted((ROOT / "docs" / "user").glob("*.md"))]
    findings: list[str] = []
    for document in documents:
        if not document.is_file():
            findings.append(f"missing public document: {document.relative_to(ROOT)}")
            continue
        text = document.read_text(encoding="utf-8")
        for match in _MARKDOWN_LINK.finditer(text):
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
    suspicious = re.compile(
        r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{12,}|sk-[A-Za-z0-9]{16,}|"
        r"Bearer\s+[A-Za-z0-9._~-]{16,}",
        re.IGNORECASE,
    )
    findings = [
        str(path.relative_to(ROOT))
        for path in candidates
        if path.is_file() and suspicious.search(path.read_text(encoding="utf-8"))
    ]
    if findings:
        raise PublicationError("public examples contain key-like values: " + ", ".join(findings))


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
