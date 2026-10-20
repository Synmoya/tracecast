"""Copy the checked-in historical excerpt into an isolated Console workspace.

This prepares saved artifacts; it does not generate text or invoke a provider.
Run from a source checkout with Tracecast and its ``console`` extra installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from tracecast.storage import ARTIFACT_SPECS, RunStore, RuntimeConfig

CASE_ROOT = Path(__file__).resolve().parents[1] / "examples" / "console-case"


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    treatment: str


class ConsoleCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    scope: str
    local_run_id: str
    original_run_id: str
    recorded_date: str
    chunk_id: str
    call_id: str
    chunk_index: int
    original_chunk_count: int
    complete_run_included: bool
    full_sources_included: bool
    saved_request_authoritative: bool
    source_title: str
    source_url: str
    source_attribution_note: str
    hashes: dict[str, str]
    redactions: list[str]
    files: list[CaseFile]


def _case_path(case_root: Path, relative: str) -> Path:
    path = case_root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(case_root.resolve()):
        raise ValueError(f"case path escapes the resource directory: {relative}")
    return path


def load_case(case_root: Path = CASE_ROOT) -> ConsoleCase:
    """Verify the complete file set and typed records before copying anything."""
    case = ConsoleCase.model_validate_json(_case_path(case_root, "case.json").read_bytes())
    _case_path(case_root, "README.md")
    run_root = _case_path(case_root, "run")
    RunStore.for_run(case_root, case.local_run_id)  # Apply the normal run ID policy.
    expected: set[str] = set()
    for entry in case.files:
        relative = PurePosixPath(entry.path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in entry.path
            or relative.parts[:1] != ("run",)
            or entry.path in expected
        ):
            raise ValueError(f"invalid or duplicate case path: {entry.path}")
        expected.add(entry.path)
        path = _case_path(case_root, entry.path)
        data = path.read_bytes()
        if len(data) != entry.bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError(f"case file integrity check failed: {entry.path}")
    actual: set[str] = set()
    for path in run_root.rglob("*"):
        relative = path.relative_to(case_root).as_posix()
        _case_path(case_root, relative)
        if path.is_file():
            actual.add(relative)
    if expected != actual:
        raise ValueError("case manifest does not match the bundled run files")
    store = RunStore(case_root / "run")
    for spec in ARTIFACT_SPECS:
        if not store.exists(spec):
            continue
        if spec.format.value == "json_model":
            store.read_model(spec)
        else:
            store.read_records(spec)
    return case


def prepare_workspace(output: Path, *, case_root: Path = CASE_ROOT) -> ConsoleCase:
    """Create a new workspace; existing directories are never overwritten."""
    case = load_case(case_root)
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(case_root / "run", output / "runs" / case.local_run_id)
    shutil.copyfile(case_root / "case.json", output / "case.json")
    shutil.copyfile(case_root / "README.md", output / "CASE.md")
    (output / "projects").mkdir()
    (output / "hosts").mkdir()
    configs = output / "configs"
    configs.mkdir()
    # A valid, empty config keeps the normal Console in browse mode. No route,
    # provider, credential name, active project or .env is copied from the user.
    (configs / "runtime.json").write_text(
        RuntimeConfig().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (configs / "source_profile_templates.json").write_text(
        json.dumps({"profiles": {}}, indent=2) + "\n", encoding="utf-8"
    )
    return case


def console_command(output: Path, port: int) -> str:
    args = [
        sys.executable, "-m", "tracecast", "console",
        "--host", "127.0.0.1", "--port", str(port),
        "--runs-root", str(output / "runs"),
        "--projects-dir", str(output / "projects"),
        "--hosts-dir", str(output / "hosts"),
        "--config", str(output / "configs" / "runtime.json"),
        "--profiles", str(output / "configs" / "source_profile_templates.json"),
    ]
    if os.name == "nt":
        return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in args)
    return shlex.join(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("tmp/console-case"),
        help="New workspace directory (default: tmp/console-case); never overwrite.",
    )
    parser.add_argument(
        "--port", type=int, default=8766,
        help="Port included in the printed Console command and links (default: 8766).",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    output = args.output.expanduser().resolve()
    try:
        case = prepare_workspace(output)
    except (OSError, ValueError) as exc:
        print(f"Cannot prepare Console case: {exc}", file=sys.stderr)
        return 1
    base = f"http://127.0.0.1:{args.port}/#/run/{case.local_run_id}"
    print(f"Prepared historical excerpt: {output}")
    print(f"Original chunk: {case.chunk_index}/{case.original_chunk_count}; no complete run.")
    print("Start the normal Console with:")
    print(console_command(output, args.port))
    print(f"Open the Console home page: http://127.0.0.1:{args.port}/")
    print("Choose the historical excerpt in the left sidebar, then Diagnostics.")
    print(f"Body and materials (Stage Trace): {base}/diagnostics/trace")
    print(f"Recorded request and response: {base}/diagnostics/calls/{case.call_id}")
    print("No API key is needed for browsing. No model is configured; do not run setup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
