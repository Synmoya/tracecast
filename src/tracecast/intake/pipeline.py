"""Sampled DSL source-intake orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracecast.control_prompt import (
    ControlPromptDecision,
    ControlPromptLocale,
    ControlPromptSample,
    resolve_control_prompt_locale,
)
from tracecast.domain import LlmCallRecord, estimate_tokens
from tracecast.language import require_bcp47_language_tag
from tracecast.llm import LlmTransportError, call_llm
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.storage import RunStore, RuntimeConfig, content_hash, sha256_text, write_json

from .dsl import (
    IntakeParseError,
    SourceProfileDecision,
    parse_probe_response,
    render_probe_user_prompt,
)
from .sampling import (
    DEFAULT_SAMPLE_TOKEN_BUDGET,
    DEFAULT_SOURCE_PIECE_MAX_TOKENS,
    SourceProbe,
    build_source_probe,
    unique_source_ids,
)

SOURCE_PROFILE_PROBE_ROUTE = "source_profile_probe"
DEFAULT_BATCH_INPUT_TOKENS = 16000
DEFAULT_MAX_SOURCES_PER_BATCH = 12

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


@dataclass(frozen=True)
class IntakeResult:
    """Mechanical summary of one intake proposal run."""

    project_id: str
    run_id: str
    proposal_path: str
    report_path: str
    raw_sources: int
    proposed_sources: int
    needs_review: int
    calls: int
    wrote_sources: bool
    control_prompt_decision: ControlPromptDecision


@dataclass(frozen=True)
class IntakeEstimate:
    """Read-only preparation result for an Intake proposal."""

    sources: int
    calls: int
    estimated_input_tokens: int
    source_stats: tuple[dict[str, Any], ...]
    models: dict[str, str]
    control_prompt_decision: ControlPromptDecision


def estimate_intake(
    *,
    project_dir: str | Path,
    profiles_path: str | Path,
    config: RuntimeConfig,
    sample_token_budget: int = DEFAULT_SAMPLE_TOKEN_BUDGET,
    batch_max_input_tokens: int = DEFAULT_BATCH_INPUT_TOKENS,
    max_sources_per_batch: int = DEFAULT_MAX_SOURCES_PER_BATCH,
) -> IntakeEstimate:
    """Prepare the exact Intake sampling/batching surface without writes or calls."""

    project_dir = Path(project_dir)
    profile_templates = json.loads(Path(profiles_path).read_text(encoding="utf-8"))
    raw_paths = _raw_source_paths(project_dir)
    raw_text_by_path = {path: path.read_text(encoding="utf-8") for path in raw_paths}
    existing = _load_existing_sources(project_dir)
    decision = resolve_intake_control_prompt(
        project_dir=project_dir,
        raw_text_by_path=raw_text_by_path,
        existing=existing,
    )
    source_ids = unique_source_ids(_rel_path(project_dir, path) for path in raw_paths)
    probes = [
        build_source_probe(
            source_alias=f"S{index:03d}",
            path=_rel_path(project_dir, path),
            raw_text=raw_text_by_path[path],
            source_id_guess=source_ids[_rel_path(project_dir, path)],
            source_piece_max_tokens=_source_piece_max_tokens(config),
            sample_token_budget=sample_token_budget,
        )
        for index, path in enumerate(raw_paths, start=1)
    ]
    system_prompt, _ = load_system_prompt(
        SOURCE_PROFILE_PROBE_ROUTE,
        config,
        control_prompt_locale=decision.variant,
    )
    user_template, _ = load_user_prompt(
        SOURCE_PROFILE_PROBE_ROUTE,
        config,
        control_prompt_locale=decision.variant,
    )
    batches = _batch_probes(
        probes,
        profile_templates=profile_templates,
        user_prompt_template=user_template,
        batch_max_input_tokens=batch_max_input_tokens,
        max_sources_per_batch=max_sources_per_batch,
    )
    estimated_input_tokens = sum(
        estimate_tokens(system_prompt)
        + estimate_tokens(
            render_probe_user_prompt(
                template=user_template,
                profile_templates=profile_templates,
                probes=batch,
            )
        )
        for batch in batches
    )
    source_stats = tuple(
        {
            "path": probe.path,
            "source_id_guess": probe.source_id_guess,
            "chars": probe.char_count,
            "lines": probe.line_count,
            "sample_estimated_tokens": sum(
                estimate_tokens(sample.text) for sample in probe.samples
            ),
        }
        for probe in probes
    )
    return IntakeEstimate(
        sources=len(probes),
        calls=len(batches),
        estimated_input_tokens=estimated_input_tokens,
        source_stats=source_stats,
        models={SOURCE_PROFILE_PROBE_ROUTE: config.route(SOURCE_PROFILE_PROBE_ROUTE).model},
        control_prompt_decision=decision,
    )


def run_intake(
    *,
    project_dir: str | Path,
    profiles_path: str | Path,
    output_language: str,
    config: RuntimeConfig,
    runs_root: str | Path = "runs",
    run_id: str | None = None,
    write_sources: bool = False,
    call: LlmCall = call_llm,
    sample_token_budget: int = DEFAULT_SAMPLE_TOKEN_BUDGET,
    batch_max_input_tokens: int = DEFAULT_BATCH_INPUT_TOKENS,
    max_sources_per_batch: int = DEFAULT_MAX_SOURCES_PER_BATCH,
) -> IntakeResult:
    """Build ``sources.proposed.json`` from sampled raw-file profile probes."""
    output_language = require_bcp47_language_tag(
        output_language, field="intake output_language"
    )
    project_dir = Path(project_dir)
    project_id = project_dir.name
    run_id = run_id or f"{project_id}-intake-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    store = RunStore.create_for_run(runs_root, run_id)

    profile_templates = json.loads(Path(profiles_path).read_text(encoding="utf-8"))
    source_piece_max_tokens = _source_piece_max_tokens(config)
    existing = _load_existing_sources(project_dir)
    if isinstance(existing.get("project_id"), str) and existing["project_id"]:
        project_id = existing["project_id"]

    raw_paths = _raw_source_paths(project_dir)
    raw_text_by_path = {path: path.read_text(encoding="utf-8") for path in raw_paths}
    control_prompt_decision = resolve_intake_control_prompt(
        project_dir=project_dir,
        raw_text_by_path=raw_text_by_path,
        existing=existing,
    )
    source_ids = unique_source_ids(_rel_path(project_dir, path) for path in raw_paths)
    probes = [
        build_source_probe(
            source_alias=f"S{index:03d}",
            path=_rel_path(project_dir, path),
            raw_text=raw_text_by_path[path],
            source_id_guess=source_ids[_rel_path(project_dir, path)],
            source_piece_max_tokens=source_piece_max_tokens,
            sample_token_budget=sample_token_budget,
        )
        for index, path in enumerate(raw_paths, start=1)
    ]

    decisions, calls, probe_reports = _run_probe_batches(
        probes,
        profile_templates=profile_templates,
        config=config,
        store=store,
        call=call,
        batch_max_input_tokens=batch_max_input_tokens,
        max_sources_per_batch=max_sources_per_batch,
        control_prompt_locale=control_prompt_decision.variant,
    )
    proposal, compile_reports = _compile_sources_proposal(
        project_id=project_id,
        output_language=output_language,
        existing=existing,
        probes=probes,
        decisions=decisions,
        profile_templates=profile_templates,
    )
    proposal_path = project_dir / "sources.proposed.json"
    write_json(proposal_path, proposal)
    if write_sources:
        write_json(project_dir / "sources.json", proposal)

    report_path = project_dir / "intake_report.md"
    report_path.write_text(
        _render_report(
            project_id=project_id,
            run_id=run_id,
            probes=probes,
            decisions=decisions,
            proposal=proposal,
            reports=[*probe_reports, *compile_reports],
            wrote_sources=write_sources,
            control_prompt_decision=control_prompt_decision,
        ),
        encoding="utf-8",
    )

    decision_by_alias = {decision.source_alias: decision for decision in decisions}
    needs_review = sum(
        1
        for probe in probes
        if decision_by_alias.get(probe.source_alias) is None
        or decision_by_alias[probe.source_alias].status == "needs_review"
    )
    return IntakeResult(
        project_id=project_id,
        run_id=run_id,
        proposal_path=str(proposal_path),
        report_path=str(report_path),
        raw_sources=len(probes),
        proposed_sources=len(proposal.get("sources", ())),
        needs_review=needs_review,
        calls=calls,
        wrote_sources=write_sources,
        control_prompt_decision=control_prompt_decision,
    )


def _run_probe_batches(
    probes: list[SourceProbe],
    *,
    profile_templates: dict,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
    batch_max_input_tokens: int,
    max_sources_per_batch: int,
    control_prompt_locale: ControlPromptLocale,
) -> tuple[list[SourceProfileDecision], int, list[str]]:
    if not probes:
        return [], 0, []
    system_prompt, system_version = load_system_prompt(
        SOURCE_PROFILE_PROBE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    user_prompt_template, user_version = load_user_prompt(
        SOURCE_PROFILE_PROBE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    prompt_version = (
        f"{system_version}+{user_version}+{_profile_catalog_version(profile_templates)}"
    )
    known_profiles = set(profile_templates["profiles"])

    decisions: list[SourceProfileDecision] = []
    reports: list[str] = []
    calls = 0
    for batch in _batch_probes(
        probes,
        profile_templates=profile_templates,
        user_prompt_template=user_prompt_template,
        batch_max_input_tokens=batch_max_input_tokens,
        max_sources_per_batch=max_sources_per_batch,
    ):
        user_prompt = render_probe_user_prompt(
            template=user_prompt_template,
            profile_templates=profile_templates,
            probes=batch,
        )
        expected_aliases = {probe.source_alias for probe in batch}
        try:
            text, _record = call(
                operation_route=SOURCE_PROFILE_PROBE_ROUTE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                config=config,
                store=store,
                prompt_version=prompt_version,
            )
            calls += 1
            decisions.extend(
                parse_probe_response(
                    text,
                    known_profiles=known_profiles,
                    expected_aliases=expected_aliases,
                )
            )
        except (IntakeParseError, LlmTransportError) as exc:
            reports.append(
                "batch "
                + ",".join(sorted(expected_aliases))
                + f" failed source-profile probe: {exc}"
            )
            decisions.extend(_needs_review_decisions(batch, str(exc)))
    return decisions, calls, reports


def _batch_probes(
    probes: list[SourceProbe],
    *,
    profile_templates: dict,
    user_prompt_template: str,
    batch_max_input_tokens: int,
    max_sources_per_batch: int,
) -> list[list[SourceProbe]]:
    batches: list[list[SourceProbe]] = []
    current: list[SourceProbe] = []
    for probe in probes:
        candidate = [*current, probe]
        rendered = render_probe_user_prompt(
            template=user_prompt_template,
            profile_templates=profile_templates,
            probes=candidate,
        )
        over_tokens = estimate_tokens(rendered) > batch_max_input_tokens
        over_count = len(candidate) > max_sources_per_batch
        if current and (over_tokens or over_count):
            batches.append(current)
            current = [probe]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _needs_review_decisions(
    probes: Iterable[SourceProbe], reason: str
) -> list[SourceProfileDecision]:
    return [
        SourceProfileDecision(
            source_alias=probe.source_alias,
            profile="unknown",
            language="unknown",
            status="needs_review",
            title=probe.source_id_guess,
            note="needs manual source profile review",
            reason=reason,
        )
        for probe in probes
    ]


def _compile_sources_proposal(
    *,
    project_id: str,
    output_language: str,
    existing: dict[str, Any],
    probes: list[SourceProbe],
    decisions: list[SourceProfileDecision],
    profile_templates: dict,
) -> tuple[dict[str, Any], list[str]]:
    profiles = profile_templates["profiles"]
    proposal = {key: value for key, value in existing.items() if key != "sources"}
    proposal["project_id"] = project_id
    proposal["output_language"] = output_language
    existing_rows = [
        dict(row) for row in existing.get("sources", ()) if isinstance(row, dict)
    ]
    proposal_sources = list(existing_rows)
    reports: list[str] = []
    existing_output_language = existing.get("output_language")
    if (
        isinstance(existing_output_language, str)
        and existing_output_language
        and existing_output_language != output_language
    ):
        reports.append(
            "proposal output_language explicitly changed from "
            f"{existing_output_language!r} to {output_language!r}"
        )

    existing_paths = {_norm_path(row.get("path")) for row in existing_rows if row.get("path")}
    existing_source_ids = {
        str(row.get("source_id")) for row in existing_rows if row.get("source_id")
    }
    decision_by_alias = {decision.source_alias: decision for decision in decisions}
    probe_by_path = {_norm_path(probe.path): probe for probe in probes}

    for row in existing_rows:
        path = _norm_path(row.get("path"))
        probe = probe_by_path.get(path)
        if probe is None:
            reports.append(f"existing source path is missing under raw/: {path}")
            continue
        decision = decision_by_alias.get(probe.source_alias)
        if decision is None:
            reports.append(f"existing source {path} has no probe decision")
            continue
        if decision.status == "ready" and row.get("profile") != decision.profile:
            reports.append(
                f"existing source {path} keeps profile={row.get('profile')!r}; "
                f"probe suggested {decision.profile!r}"
            )
        if decision.status == "needs_review":
            reports.append(f"existing source {path} needs review: {decision.reason}")

    for probe in probes:
        path = _norm_path(probe.path)
        if path in existing_paths:
            continue
        decision = decision_by_alias.get(probe.source_alias)
        if decision is None:
            reports.append(f"new raw source {path} has no probe decision")
            continue
        if decision.status != "ready":
            reports.append(f"new raw source {path} not added: {decision.reason}")
            continue
        template = profiles[decision.profile]
        source_id = _dedupe_source_id(probe.source_id_guess, existing_source_ids)
        existing_source_ids.add(source_id)
        proposal_sources.append(
            {
                "source_id": source_id,
                "path": path,
                "profile": decision.profile,
                "language": decision.language,
                "source_type": "local_text",
                "source_class": template["default_source_class"],
                "trust_tier": template["default_trust_tier"],
                "title": decision.title or probe.source_id_guess,
                "note": decision.note,
                "content_hash": f"sha256:{probe.content_hash}",
            }
        )
    proposal["sources"] = proposal_sources
    return proposal, reports


def _render_report(
    *,
    project_id: str,
    run_id: str,
    probes: list[SourceProbe],
    decisions: list[SourceProfileDecision],
    proposal: dict[str, Any],
    reports: list[str],
    wrote_sources: bool,
    control_prompt_decision: ControlPromptDecision,
) -> str:
    decision_by_alias = {decision.source_alias: decision for decision in decisions}
    lines = [
        f"# Intake Report: {project_id}",
        "",
        f"- run_id: `{run_id}`",
        f"- raw sources scanned: {len(probes)}",
        f"- sources in proposal: {len(proposal.get('sources', ())) }",
        f"- wrote sources.json: {str(wrote_sources).lower()}",
        f"- source language: `{control_prompt_decision.source_language.value}`; "
        f"control prompt variant: `{control_prompt_decision.variant.value}` "
        f"({control_prompt_decision.policy_version}, "
        f"confidence={control_prompt_decision.confidence:.3f})",
        "",
        "## Decisions",
        "",
    ]
    for probe in probes:
        decision = decision_by_alias.get(probe.source_alias)
        if decision is None:
            lines.append(f"- {probe.path}: missing decision")
            continue
        lines.append(
            f"- {probe.path}: profile={decision.profile}, language={decision.language}, "
            f"status={decision.status}; {decision.reason}"
        )
    lines.extend(["", "## Reports", ""])
    if reports:
        lines.extend(f"- {item}" for item in reports)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def resolve_intake_control_prompt(
    *,
    project_dir: str | Path,
    raw_text_by_path: dict[Path, str] | None = None,
    existing: dict[str, Any] | None = None,
) -> ControlPromptDecision:
    """Resolve one control locale for all raw files in an Intake draft.

    Existing source rows contribute their normalized language declaration when
    their project-relative path still matches.  Newly imported files have no
    declaration yet and therefore remain ``unknown`` and use the English
    control variant instead of letting Intake's classifier assume its own answer.
    """

    root = Path(project_dir)
    texts = raw_text_by_path
    if texts is None:
        texts = {path: path.read_text(encoding="utf-8") for path in _raw_source_paths(root)}
    current = existing if existing is not None else _load_existing_sources(root)
    language_by_path = {
        _norm_path(row.get("path")): str(row.get("language") or "") or None
        for row in current.get("sources", ())
        if isinstance(row, dict)
    }
    return resolve_control_prompt_locale(
        [
            ControlPromptSample(
                text=text,
                language_tag=language_by_path.get(_rel_path(root, path)),
            )
            for path, text in sorted(texts.items(), key=lambda item: str(item[0]).casefold())
        ]
    )


def _source_piece_max_tokens(config: RuntimeConfig) -> int:
    value = (config.stage_budgets.get("ingestion") or {}).get("source_piece_max_tokens")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_SOURCE_PIECE_MAX_TOKENS


def _raw_source_paths(project_dir: Path) -> list[Path]:
    raw_dir = project_dir / "raw"
    if not raw_dir.exists():
        return []
    return sorted(
        path for path in raw_dir.iterdir() if path.is_file() and not path.name.startswith(".")
    )


def _rel_path(project_dir: Path, path: Path) -> str:
    return path.relative_to(project_dir).as_posix()


def _norm_path(value: object) -> str:
    return str(value or "").replace("\\", "/")


def _load_existing_sources(project_dir: Path) -> dict[str, Any]:
    path = project_dir / "sources.json"
    if not path.exists():
        data: dict[str, Any] = {"project_id": project_dir.name, "sources": []}
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object")
    data.pop("persona", None)
    data.setdefault("sources", [])
    return data


def _profile_catalog_version(profile_templates: dict[str, Any]) -> str:
    version = profile_templates.get("version")
    if isinstance(version, str) and version:
        return version
    return "source_profiles@" + sha256_text(json.dumps(profile_templates, sort_keys=True))[:12]


def _dedupe_source_id(source_id: str, used: set[str]) -> str:
    if source_id not in used:
        return source_id
    suffix = content_hash(source_id)[:8]
    candidate = f"{source_id}_{suffix}"
    index = 2
    while candidate in used:
        candidate = f"{source_id}_{suffix}_{index}"
        index += 1
    return candidate
