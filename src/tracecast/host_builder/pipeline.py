"""Optional two-call workflow that compiles preference text into a Host."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from tracecast.control_prompt import (
    ControlPromptDecision,
    ControlPromptLocale,
    ControlPromptSample,
    resolve_control_prompt_locale,
)
from tracecast.domain import LlmCallRecord, LlmWorkUnitRef
from tracecast.hosts import (
    STYLE_EXAMPLE_POSITIONS,
    HostExpression,
    HostProfile,
    HostThinking,
    StyleExamplePosition,
    host_path,
)
from tracecast.llm import call_llm
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.storage import (
    LLM_CALL_RECORDS,
    RunStore,
    RuntimeConfig,
    atomic_write_text,
    content_hash,
    write_json_model,
)

from .dsl import (
    parse_examples_response,
    parse_profile_response,
    render_examples_user_prompt,
    render_profile_user_prompt,
    selected_example_rows,
)
from .models import (
    HostBuildCandidate,
    HostBuildManifest,
    HostBuildResult,
    HostBuildSelectedExample,
)
from .sampling import (
    DEFAULT_PIECE_MAX_TOKENS,
    DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET,
    PreferencePiece,
    PreparedCorpus,
    build_position_pools,
    build_profile_sample,
    is_bcp47_language_tag,
    language_script_family,
    prepare_corpus,
    select_style_examples,
)

HOST_PROFILE_ROUTE = "host_profile_distill"
HOST_EXAMPLES_ROUTE = "host_style_example_reverse"
HOST_BUILD_MANIFEST_PATH = "host_builder/manifest.json"
HOST_BUILD_PROPOSAL_PATH = "host_builder/host.proposed.json"
HOST_BUILD_REPORT_PATH = "host_builder/report.md"

LlmCall = Callable[..., tuple[str, LlmCallRecord]]


def run_host_build(
    *,
    inputs: Sequence[str | Path],
    host_id: str,
    config: RuntimeConfig,
    display_name: str | None = None,
    version: str | None = None,
    piece_max_tokens: int = DEFAULT_PIECE_MAX_TOKENS,
    seed: int | None = None,
    profile_sample_token_budget: int = DEFAULT_PROFILE_SAMPLE_TOKEN_BUDGET,
    hosts_dir: str | Path = "hosts",
    runs_root: str | Path = "runs",
    run_id: str | None = None,
    call: LlmCall = call_llm,
) -> HostBuildResult:
    """Build and exclusively install one Host from local preference texts."""

    target = host_path(hosts_dir, host_id)
    if target.exists():
        raise FileExistsError(f"host already exists: {target}")
    corpus = prepare_corpus(
        list(inputs),
        piece_max_tokens=piece_max_tokens,
        seed=seed,
    )
    selected = select_style_examples(corpus)
    profile_sample = build_profile_sample(
        corpus,
        selected,
        token_budget=profile_sample_token_budget,
    )
    control_prompt_decision = resolve_host_build_control_prompt(corpus)
    prompt_set = load_host_build_prompts(
        config,
        control_prompt_locale=control_prompt_decision.variant,
    )
    actual_run_id = run_id or f"{host_id}-hostbuild-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    store = RunStore.create_for_run(runs_root, actual_run_id)
    selected_rows = selected_example_rows(selected)
    candidate_pools = build_position_pools(corpus)
    manifest = HostBuildManifest(
        run_id=actual_run_id,
        host_id=host_id,
        piece_max_tokens=piece_max_tokens,
        profile_sample_token_budget=profile_sample_token_budget,
        seed=corpus.seed,
        dominant_script=corpus.dominant_script,
        control_prompt_decision=control_prompt_decision,
        inputs=list(corpus.input_records),
        candidates=[
            HostBuildCandidate(
                candidate_id=piece.candidate_id,
                position=position,
                input_path=piece.input_path,
                input_content_hash=piece.input_content_hash,
                span_start=piece.span_start,
                span_end=piece.span_end,
                render_hash=content_hash(piece.text),
                estimated_tokens=piece.estimated_tokens,
            )
            for position in STYLE_EXAMPLE_POSITIONS
            for piece in candidate_pools[position]
        ],
        selected_examples=[
            HostBuildSelectedExample(
                candidate_id=piece.candidate_id,
                position=position,
                input_path=piece.input_path,
                input_content_hash=piece.input_content_hash,
                span_start=piece.span_start,
                span_end=piece.span_end,
                render_hash=content_hash(piece.text),
                estimated_tokens=piece.estimated_tokens,
            )
            for _alias, position, piece in selected_rows
        ],
    )
    _write_manifest(store, manifest)

    try:
        profile_rows = profile_sample_rows(profile_sample, selected, corpus)
        profile_text, profile_record = call(
            operation_route=HOST_PROFILE_ROUTE,
            messages=[
                {"role": "system", "content": prompt_set["profile_system"]},
                {
                    "role": "user",
                    "content": render_profile_user_prompt(
                        template=prompt_set["profile_user"],
                        samples=profile_rows,
                    ),
                },
            ],
            config=config,
            store=store,
            prompt_version=prompt_set["profile_version"],
            work_unit=LlmWorkUnitRef(
                kind="host_profile_distill",
                unit_id=host_id,
                input_refs=[piece.candidate_id for _, _, piece in profile_rows],
                metadata={"seed": corpus.seed},
            ),
        )
        manifest.call_ids.append(profile_record.call_id)
        _sync_call_ids(store, manifest)
        profile = parse_profile_response(profile_text)
        language_primary = profile.output_language.strip().casefold().split("-", 1)[0]
        if (
            not is_bcp47_language_tag(profile.output_language)
            or language_primary in {"und", "unknown"}
        ):
            raise ValueError(
                f"profile output_language must be a concrete BCP-47 language tag: "
                f"{profile.output_language!r}"
            )
        detected_family = language_script_family(profile.output_language)
        if detected_family != corpus.dominant_script:
            raise ValueError(
                f"profile output_language {profile.output_language!r} maps to "
                f"{detected_family!r}, corpus is {corpus.dominant_script!r}"
            )
        manifest.output_language = profile.output_language
        _write_manifest(store, manifest)

        expected = {alias: (position, piece) for alias, position, piece in selected_rows}
        examples_text, examples_record = call(
            operation_route=HOST_EXAMPLES_ROUTE,
            messages=[
                {"role": "system", "content": prompt_set["examples_system"]},
                {
                    "role": "user",
                    "content": render_examples_user_prompt(
                        template=prompt_set["examples_user"],
                        samples=selected_rows,
                    ),
                },
            ],
            config=config,
            store=store,
            prompt_version=prompt_set["examples_version"],
            work_unit=LlmWorkUnitRef(
                kind="host_style_example_reverse",
                unit_id=host_id,
                input_refs=[piece.candidate_id for _, _, piece in selected_rows],
                metadata={"seed": corpus.seed},
            ),
        )
        manifest.call_ids.append(examples_record.call_id)
        _sync_call_ids(store, manifest)
        compiled = parse_examples_response(examples_text, expected=expected)
        host = HostProfile(
            schema_version="tracecast-host/2",
            host_id=host_id,
            display_name=(display_name or host_id).strip(),
            version=(version or f"{host_id}@v1").strip(),
            output_language=profile.output_language,
            host_thinking=HostThinking(
                viewpoint=profile.viewpoint,
                prefer=profile.prefer,
                avoid=profile.avoid,
                human_directive=[],
                rules=profile.thinking_rules,
            ),
            host_expression=HostExpression(
                description=profile.expression_description,
                rules=profile.expression_rules,
            ),
            style_examples_by_position={
                position: tuple(
                    compiled[alias]
                    for alias, row_position, _piece in selected_rows
                    if row_position == position
                )
                for position in STYLE_EXAMPLE_POSITIONS
            },
        )
        write_json_model(store.path_for(HOST_BUILD_PROPOSAL_PATH), host)
        _exclusive_write_host(target, host)
        manifest.status = "complete"
        manifest.installed_path = str(target.resolve())
        _write_manifest(store, manifest)
        _write_report(store, manifest)
        return HostBuildResult(
            run_id=actual_run_id,
            run_dir=str(store.run_dir),
            host_path=str(target),
            host=host,
            calls=len(manifest.call_ids),
            sampled_examples=len(selected_rows),
        )
    except Exception as exc:
        _sync_call_ids(store, manifest)
        manifest.status = "failed"
        manifest.diagnostics.append(f"{type(exc).__name__}: {exc}")
        _write_manifest(store, manifest)
        _write_report(store, manifest)
        raise


def load_host_build_prompts(
    config: RuntimeConfig,
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, str]:
    """Load both prompt pairs for the CLI and Console dry-run adapter."""

    profile_system, profile_system_version = load_system_prompt(
        HOST_PROFILE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    profile_user, profile_user_version = load_user_prompt(
        HOST_PROFILE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    examples_system, examples_system_version = load_system_prompt(
        HOST_EXAMPLES_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    examples_user, examples_user_version = load_user_prompt(
        HOST_EXAMPLES_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    return {
        "profile_system": profile_system,
        "profile_user": profile_user,
        "profile_version": f"{profile_system_version}+{profile_user_version}",
        "examples_system": examples_system,
        "examples_user": examples_user,
        "examples_version": f"{examples_system_version}+{examples_user_version}",
    }


def resolve_host_build_control_prompt(corpus: PreparedCorpus) -> ControlPromptDecision:
    """Resolve one locale from the complete eligible preference corpus.

    Host Builder has no operator-authored language declaration before its first
    model call.  The resolver therefore records ``unknown`` and uses the English
    fallback variant, avoiding a false Chinese assumption for Japanese, Korean,
    or other corpora that share the broad CJK script family.
    """

    return resolve_control_prompt_locale(
        [
            ControlPromptSample(text=document.text, language_tag=None)
            for document in corpus.documents
        ]
    )


def profile_sample_rows(
    profile_sample: Sequence[PreferencePiece],
    selected: Mapping[StyleExamplePosition, tuple[PreferencePiece, ...]],
    corpus: PreparedCorpus,
) -> list[tuple[str, StyleExamplePosition, PreferencePiece]]:
    """Project the source-balanced profile sample with stable call-local aliases."""

    positions: dict[str, StyleExamplePosition] = {}
    for position, pieces in build_position_pools(corpus).items():
        for piece in pieces:
            positions.setdefault(piece.candidate_id, position)
    for position, pieces in selected.items():
        for piece in pieces:
            positions[piece.candidate_id] = position
    return [
        (f"P{index:03d}", positions[piece.candidate_id], piece)
        for index, piece in enumerate(profile_sample, start=1)
    ]


def _exclusive_write_host(path: Path, host: HostProfile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        host.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            handle.write(payload)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def _write_manifest(store: RunStore, manifest: HostBuildManifest) -> None:
    write_json_model(store.path_for(HOST_BUILD_MANIFEST_PATH), manifest)


def _sync_call_ids(store: RunStore, manifest: HostBuildManifest) -> None:
    """Mirror every persisted provider attempt into the typed build manifest."""

    ledger_path = store.path_for(LLM_CALL_RECORDS.relpath)
    if not ledger_path.exists():
        return
    manifest.call_ids = [record.call_id for record in store.read_records(LLM_CALL_RECORDS)]


def _write_report(store: RunStore, manifest: HostBuildManifest) -> None:
    excluded = [row for row in manifest.inputs if not row.eligible]
    lines = [
        f"# Host Build Report: {manifest.host_id}",
        "",
        f"- status: `{manifest.status}`",
        f"- run_id: `{manifest.run_id}`",
        f"- dominant script: `{manifest.dominant_script}`",
        f"- output language: `{manifest.output_language or 'unknown'}`",
        f"- seed: `{manifest.seed}`",
        f"- selected examples: {len(manifest.selected_examples)}",
        f"- candidates: {len(manifest.candidates)}",
        f"- excluded inputs: {len(excluded)}",
        f"- calls: {len(manifest.call_ids)}",
        "",
        "## Diagnostics",
        "",
    ]
    lines.extend(f"- {item}" for item in manifest.diagnostics)
    if not manifest.diagnostics:
        lines.append("- none")
    lines.extend(["", "## Excluded inputs", ""])
    lines.extend(f"- `{row.path}`: {row.exclusion_reason}" for row in excluded)
    if not excluded:
        lines.append("- none")
    lines.append("")
    atomic_write_text(store.path_for(HOST_BUILD_REPORT_PATH), "\n".join(lines))


__all__ = [
    "HOST_BUILD_MANIFEST_PATH",
    "HOST_BUILD_PROPOSAL_PATH",
    "HOST_BUILD_REPORT_PATH",
    "HOST_EXAMPLES_ROUTE",
    "HOST_PROFILE_ROUTE",
    "load_host_build_prompts",
    "profile_sample_rows",
    "run_host_build",
]
