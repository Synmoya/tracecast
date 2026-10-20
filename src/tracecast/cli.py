"""Tracecast command-line entry point.

``python -m tracecast run --project example`` runs the full pipeline for a project under
``projects/<id>/`` and writes ``runs/<run_id>/`` artifacts including the final script.
``--max-sources`` / ``--limit-source-chars`` bound a live *slice* validation so the
whole chain can be proven for real at minimal provider spend before a full run.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from tracecast import __version__
from tracecast.host_builder import HostBuildResult, run_host_build
from tracecast.hosts import load_host, require_host_language
from tracecast.intake import IntakeResult, run_intake
from tracecast.language import require_bcp47_language_tag
from tracecast.pipeline import RunResult, run_pipeline
from tracecast.project import load_project
from tracecast.storage import DEFAULT_RUNTIME_CONFIG_PATH, RuntimeConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tracecast",
        description="Evidence-grounded long-form podcast script generator.",
    )
    parser.add_argument("--version", action="version", version=f"tracecast {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run the pipeline for a project.")
    run.add_argument("--project", required=True, help="Project id under --projects-dir.")
    run.add_argument("--host", required=True, dest="host_id", help="Host id under --hosts-dir.")
    run.add_argument(
        "--projects-dir", default="projects", help="Projects root (default: projects)."
    )
    run.add_argument("--hosts-dir", default="hosts", help="Hosts root (default: hosts).")
    run.add_argument(
        "--config",
        default=str(DEFAULT_RUNTIME_CONFIG_PATH),
        help="Runtime config JSON (default: configs/runtime.json).",
    )
    run.add_argument(
        "--profiles",
        default="configs/source_profile_templates.json",
        help="Source-profile templates JSON.",
    )
    run.add_argument("--runs-root", default="runs", help="Run-artifacts root (default: runs).")
    run.add_argument("--run-id", default=None, help="Explicit run id (default: <project>-<UTC>).")
    run.add_argument(
        "--max-sources", type=int, default=None, help="Slice: ingest only the first N sources."
    )
    run.add_argument(
        "--limit-source-chars",
        type=int,
        default=None,
        help="Slice: truncate each source's raw text to N chars.",
    )
    run.add_argument(
        "--output-language",
        default=None,
        help="Override the project's target language for this root run (BCP 47).",
    )

    intake = sub.add_parser("intake", help="Sample raw sources and propose sources.json.")
    intake.add_argument("--project", required=True, help="Project id under --projects-dir.")
    intake.add_argument(
        "--projects-dir", default="projects", help="Projects root (default: projects)."
    )
    intake.add_argument(
        "--config",
        default=str(DEFAULT_RUNTIME_CONFIG_PATH),
        help="Runtime config JSON (default: configs/runtime.json).",
    )
    intake.add_argument(
        "--profiles",
        default="configs/source_profile_templates.json",
        help="Source-profile templates JSON.",
    )
    intake.add_argument(
        "--output-language",
        required=True,
        help="Explicit target language for the generated project proposal (for example zh-CN).",
    )
    intake.add_argument("--runs-root", default="runs", help="Run-artifacts root (default: runs).")
    intake.add_argument("--run-id", default=None, help="Explicit intake run id.")
    intake.add_argument(
        "--write-sources",
        action="store_true",
        help="Write the compiled proposal to sources.json after sources.proposed.json.",
    )
    intake.add_argument(
        "--sample-token-budget",
        type=int,
        default=1200,
        help="Approximate per-source sampled text budget (default: 1200).",
    )
    intake.add_argument(
        "--batch-max-input-tokens",
        type=int,
        default=16000,
        help="Approximate max intake probe user-prompt tokens per batch.",
    )
    intake.add_argument(
        "--max-sources-per-batch",
        type=int,
        default=12,
        help="Max raw sources per intake probe call.",
    )

    host_build = sub.add_parser(
        "host-build",
        help="Build and install a reusable Host from preference texts.",
    )
    host_build.add_argument("inputs", nargs="+", help="UTF-8 text files or directories.")
    host_build.add_argument("--host-id", required=True, help="New Host id under --hosts-dir.")
    host_build.add_argument(
        "--display-name",
        default=None,
        help="Human-facing Host name (default: host id).",
    )
    host_build.add_argument(
        "--version",
        default=None,
        help="Host content version (default: <host-id>@v1).",
    )
    host_build.add_argument(
        "--piece-max-tokens",
        type=int,
        default=180,
        help="Hard estimated-token cap per sampled passage (default: 180).",
    )
    host_build.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional deterministic sampling seed (default: input-derived).",
    )
    host_build.add_argument(
        "--config",
        default=str(DEFAULT_RUNTIME_CONFIG_PATH),
        help="Runtime config JSON (default: configs/runtime.json).",
    )
    host_build.add_argument("--hosts-dir", default="hosts", help="Hosts root (default: hosts).")
    host_build.add_argument(
        "--runs-root", default="runs", help="Run-artifacts root (default: runs)."
    )
    host_build.add_argument("--run-id", default=None, help="Explicit Host Builder run id.")

    console = sub.add_parser(
        "console",
        help="Launch the local debugging console (requires the 'console' extra).",
    )
    console.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; the console has no auth).",
    )
    console.add_argument("--port", type=int, default=8766, help="Port (default: 8766).")
    console.add_argument(
        "--projects-dir", default="projects", help="Projects root (default: projects)."
    )
    console.add_argument("--hosts-dir", default="hosts", help="Hosts root (default: hosts).")
    console.add_argument(
        "--config",
        default=str(DEFAULT_RUNTIME_CONFIG_PATH),
        help="Runtime config JSON (default: configs/runtime.json).",
    )
    console.add_argument(
        "--profiles",
        default="configs/source_profile_templates.json",
        help="Source-profile templates JSON.",
    )
    console.add_argument(
        "--runs-root", default="runs", help="Run-artifacts root (default: runs)."
    )
    return parser


def _print_summary(result: RunResult) -> None:
    print(f"run_id            {result.run_id}")
    print(f"artifacts         {result.runs_root}/{result.run_id}/")
    print(f"sources/atoms     {result.sources} / {result.atoms}")
    print(f"nodes/selected    {result.nodes} / {result.selected_nodes}")
    print(f"chunks            {result.chunks}")
    print(f"script chars      {result.script_chars}")
    print(f"llm calls         {result.calls}")
    if result.control_prompt_decision is not None:
        print(
            "control prompt    "
            f"source={result.control_prompt_decision.source_language.value} "
            f"variant={result.control_prompt_decision.variant.value} "
            f"({result.control_prompt_decision.confidence:.3f})"
        )
    print(f"audit findings    {result.audit_items}")
    print(f"flagged w/ issues {result.upstream_has_issues}")


def _print_intake_summary(result: IntakeResult) -> None:
    print(f"run_id            {result.run_id}")
    print(f"proposal          {result.proposal_path}")
    print(f"report            {result.report_path}")
    print(f"raw sources       {result.raw_sources}")
    print(f"proposal sources  {result.proposed_sources}")
    print(f"needs review      {result.needs_review}")
    print(f"llm calls         {result.calls}")
    print(f"wrote sources     {result.wrote_sources}")


def _print_host_build_summary(result: HostBuildResult) -> None:
    print(f"run_id            {result.run_id}")
    print(f"artifacts         {result.run_dir}/")
    print(f"host              {result.host_path}")
    print(f"output language   {result.host.output_language}")
    print(f"style examples    {result.sampled_examples}")
    print(f"llm calls         {result.calls}")


def _run_console(args: argparse.Namespace) -> int:
    """Serve the debugging console; deps are an optional extra, so import lazily."""
    try:
        import uvicorn

        from tracecast.console import create_app
    except ImportError:
        print("console dependencies missing; install with: python -m pip install '.[console]'")
        return 2
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"warning: console has no auth; binding to {args.host!r} exposes it beyond localhost")
    app = create_app(
        runs_root=args.runs_root,
        projects_dir=args.projects_dir,
        hosts_dir=args.hosts_dir,
        config_path=args.config,
        profiles_path=args.profiles,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"run", "intake", "host-build", "console"}:
        parser.print_help()
        return 0

    if args.command == "console":
        return _run_console(args)

    if args.command == "host-build":
        config = RuntimeConfig.load(args.config)
        result = run_host_build(
            inputs=args.inputs,
            host_id=args.host_id,
            display_name=args.display_name,
            version=args.version,
            piece_max_tokens=args.piece_max_tokens,
            seed=args.seed,
            config=config,
            hosts_dir=args.hosts_dir,
            runs_root=args.runs_root,
            run_id=args.run_id,
        )
        _print_host_build_summary(result)
        return 0

    project_dir = Path(args.projects_dir) / args.project
    if not project_dir.is_dir():
        parser.error(f"project directory not found: {project_dir}")

    if args.command == "intake":
        config = RuntimeConfig.load(args.config)
        result = run_intake(
            project_dir=project_dir,
            profiles_path=args.profiles,
            output_language=args.output_language,
            config=config,
            runs_root=args.runs_root,
            run_id=args.run_id,
            write_sources=args.write_sources,
            sample_token_budget=args.sample_token_budget,
            batch_max_input_tokens=args.batch_max_input_tokens,
            max_sources_per_batch=args.max_sources_per_batch,
        )
        _print_intake_summary(result)
        return 0

    project = load_project(project_dir, args.profiles)
    try:
        output_language = require_bcp47_language_tag(
            (
                args.output_language
                if args.output_language is not None
                else project.output_language
            ),
            field="output_language",
        )
        host = load_host(args.hosts_dir, args.host_id)
        require_host_language(host, output_language)
    except (OSError, TypeError, ValueError) as exc:
        parser.error(f"cannot load host {args.host_id!r}: {exc}")
    config = RuntimeConfig.load(args.config)
    result = run_pipeline(
        project,
        project_dir,
        host=host,
        config=config,
        runs_root=args.runs_root,
        run_id=args.run_id,
        max_sources=args.max_sources,
        limit_source_chars=args.limit_source_chars,
        output_language=output_language,
    )
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
