"""Local-upload adapter for the optional Host Builder workflow."""

from __future__ import annotations

import hashlib
import re
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracecast.domain import estimate_tokens
from tracecast.host_builder import (
    DEFAULT_PIECE_MAX_TOKENS,
    DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET,
    build_profile_sample,
    load_host_build_prompts,
    prepare_corpus,
    profile_sample_rows,
    render_examples_user_prompt,
    render_profile_user_prompt,
    resolve_host_build_control_prompt,
    run_host_build,
    select_style_examples,
    selected_example_rows,
)
from tracecast.host_builder.pipeline import HOST_EXAMPLES_ROUTE, HOST_PROFILE_ROUTE
from tracecast.hosts import host_path
from tracecast.storage import RuntimeConfig, content_hash

from .context import ConsoleContext
from .errors import BadRequest, Conflict, ConsoleError
from .host_catalog import BUILTIN_HOST_IDS
from .jobs import Job
from .overrides import (
    ConfigOverrides,
    apply_config_overrides,
    preflight_prompts,
)
from .run_service import progress_call

_SAFE_FILE_PART = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class UploadedPreferenceFile:
    name: str
    content: str


@dataclass(frozen=True)
class _PreparedBuild:
    config: RuntimeConfig
    seed: int
    estimate: dict[str, Any]


def build_host(
    ctx: ConsoleContext,
    *,
    files: Sequence[UploadedPreferenceFile],
    host_id: str,
    display_name: str | None = None,
    version: str | None = None,
    piece_max_tokens: int = DEFAULT_PIECE_MAX_TOKENS,
    seed: int | None = None,
    config_overrides: ConfigOverrides | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Estimate or submit a two-call Host build from browser-selected text files."""

    normalized = _normalize_files(files)
    _validate_identity(ctx, host_id, display_name=display_name, version=version)
    effective_display_name = (display_name or host_id).strip()
    effective_version = (version or f"{host_id}@v1").strip()
    effective_seed = seed if seed is not None else _upload_seed(normalized)
    prepared = _prepare(
        ctx,
        files=normalized,
        host_id=host_id,
        display_name=effective_display_name,
        version=effective_version,
        piece_max_tokens=piece_max_tokens,
        seed=effective_seed,
        config_overrides=config_overrides,
    )
    if not confirm:
        return {"confirm_required": True, "estimate": prepared.estimate}

    run_id = _run_id(host_id)

    def target(job: Job) -> dict[str, Any]:
        with _staged_files(normalized) as staged:
            result = run_host_build(
                inputs=staged.paths,
                host_id=host_id,
                display_name=effective_display_name,
                version=effective_version,
                piece_max_tokens=piece_max_tokens,
                seed=prepared.seed,
                config=prepared.config,
                hosts_dir=ctx.hosts_dir,
                runs_root=ctx.runs_root,
                run_id=run_id,
                call=progress_call(ctx, job, "Host Builder"),
            )
        snapshot = result.host.snapshot()
        return {
            "host_id": result.host.host_id,
            "display_name": result.host.display_name,
            "version": result.host.version,
            "output_language": result.host.output_language,
            "content_hash": snapshot["content_hash"],
            "calls": result.calls,
            "sampled_examples": result.sampled_examples,
        }

    job = ctx.registry.submit(
        kind="host_build",
        run_id=run_id,
        base_run_id=None,
        target=target,
        progress_total=2,
    )
    return {
        "job_id": job.job_id,
        "run_id": run_id,
        "host_id": host_id,
    }


def _prepare(
    ctx: ConsoleContext,
    *,
    files: Sequence[UploadedPreferenceFile],
    host_id: str,
    display_name: str,
    version: str,
    piece_max_tokens: int,
    seed: int,
    config_overrides: ConfigOverrides | None,
) -> _PreparedBuild:
    try:
        config = apply_config_overrides(ctx.load_config(), config_overrides)
        with _staged_files(files) as staged:
            corpus = prepare_corpus(
                list(staged.paths),
                piece_max_tokens=piece_max_tokens,
                seed=seed,
            )
            control_prompt_decision = resolve_host_build_control_prompt(corpus)
            preflight_prompts(
                config,
                [HOST_PROFILE_ROUTE, HOST_EXAMPLES_ROUTE],
                control_prompt_locale=control_prompt_decision.variant,
            )
            selected = select_style_examples(corpus)
            profile_sample = build_profile_sample(
                corpus,
                selected,
                token_budget=DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET,
            )
            selected_rows = selected_example_rows(selected)
            profile_rows = profile_sample_rows(profile_sample, selected, corpus)
            prompts = load_host_build_prompts(
                config,
                control_prompt_locale=control_prompt_decision.variant,
            )
            profile_user = render_profile_user_prompt(
                template=prompts["profile_user"],
                samples=profile_rows,
            )
            examples_user = render_examples_user_prompt(
                template=prompts["examples_user"],
                samples=selected_rows,
            )
            selected_preview = [
                {
                    "position": position,
                    "file_name": staged.labels[str(Path(piece.input_path).resolve())],
                    "text": piece.text,
                    "estimated_tokens": piece.estimated_tokens,
                }
                for _alias, position, piece in selected_rows
            ]
            excluded = [
                {
                    "file_name": staged.labels[str(Path(row.path).resolve())],
                    "reason": row.exclusion_reason,
                }
                for row in corpus.input_records
                if not row.eligible
            ]
            estimate = {
                "host_id": host_id,
                "display_name": display_name,
                "version": version,
                "piece_max_tokens": piece_max_tokens,
                "calls": 2,
                "est_input_tokens": sum(
                    estimate_tokens(value)
                    for value in (
                        prompts["profile_system"],
                        profile_user,
                        prompts["examples_system"],
                        examples_user,
                    )
                ),
                "models": {
                    HOST_PROFILE_ROUTE: config.route(HOST_PROFILE_ROUTE).model,
                    HOST_EXAMPLES_ROUTE: config.route(HOST_EXAMPLES_ROUTE).model,
                },
                "dominant_script": corpus.dominant_script,
                "seed": corpus.seed,
                "files": len(corpus.input_records),
                "eligible_files": sum(row.eligible for row in corpus.input_records),
                "excluded_file_count": len(excluded),
                "excluded_files": excluded,
                "profile_sample_count": len(profile_sample),
                "profile_sample_tokens": sum(
                    piece.estimated_tokens for piece in profile_sample
                ),
                "selected_examples": selected_preview,
                "control_prompt_decision": control_prompt_decision.model_dump(mode="json"),
                "notes": [
                    "固定两次串行调用，无 repair",
                    "输出语言由 Profile 调用确定，并须匹配检测到的脚本族",
                    f"目标 Host: {host_id}",
                ],
            }
    except ConsoleError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot prepare Host build: {exc}") from exc
    return _PreparedBuild(config=config, seed=seed, estimate=estimate)


def _validate_identity(
    ctx: ConsoleContext,
    host_id: str,
    *,
    display_name: str | None,
    version: str | None,
) -> None:
    if not host_id or host_id != host_id.strip():
        raise BadRequest("host_id must be non-empty and must not have outer whitespace")
    if host_id in BUILTIN_HOST_IDS:
        raise Conflict(f"built-in host id is reserved: {host_id}")
    if display_name is not None and not display_name.strip():
        raise BadRequest("display_name must be non-empty when provided")
    if version is not None and not version.strip():
        raise BadRequest("version must be non-empty when provided")
    try:
        target = host_path(ctx.hosts_dir, host_id)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    if target.exists():
        raise Conflict(f"host already exists: {target}")


def _normalize_files(
    files: Sequence[UploadedPreferenceFile],
) -> tuple[UploadedPreferenceFile, ...]:
    if not files:
        raise BadRequest("select at least one UTF-8 .txt reference")
    normalized: list[UploadedPreferenceFile] = []
    seen: set[str] = set()
    for row in files:
        name = row.name.strip()
        if not name or name != Path(name).name or "/" in name or "\\" in name:
            raise BadRequest(f"invalid reference file name: {row.name!r}")
        if Path(name).suffix.casefold() != ".txt":
            raise BadRequest(f"reference file must be .txt: {name}")
        folded = name.casefold()
        if folded in seen:
            raise BadRequest(f"duplicate reference file name: {name}")
        if not row.content.strip():
            raise BadRequest(f"reference file is empty: {name}")
        seen.add(folded)
        normalized.append(UploadedPreferenceFile(name=name, content=row.content))
    return tuple(normalized)


def _upload_seed(files: Sequence[UploadedPreferenceFile]) -> int:
    payload = "\x1e".join(
        f"{index}\x1f{row.name}\x1f{content_hash(row.content)}"
        for index, row in enumerate(files, start=1)
    )
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16], 16)


@dataclass(frozen=True)
class _StagedFiles:
    paths: tuple[Path, ...]
    labels: dict[str, str]


@contextmanager
def _staged_files(
    files: Sequence[UploadedPreferenceFile],
) -> Iterator[_StagedFiles]:
    with tempfile.TemporaryDirectory(prefix="tracecast-hostbuild-") as temp:
        root = Path(temp)
        paths: list[Path] = []
        labels: dict[str, str] = {}
        for index, row in enumerate(files, start=1):
            safe_name = _SAFE_FILE_PART.sub("-", row.name).strip(".-") or "reference.txt"
            if not safe_name.casefold().endswith(".txt"):
                safe_name += ".txt"
            path = root / f"{index:04d}-{safe_name}"
            path.write_text(row.content, encoding="utf-8", newline="\n")
            paths.append(path)
            labels[str(path.resolve())] = row.name
        yield _StagedFiles(paths=tuple(paths), labels=labels)


def _run_id(host_id: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", host_id).strip("-") or "host"
    return f"{label}-hostbuild-{datetime.now(tz=UTC):%Y%m%dT%H%M%S%fZ}"


__all__ = ["UploadedPreferenceFile", "build_host"]
