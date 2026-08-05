"""Selection stage: lock an editorial path, then attach whole-path directives.

Main chain: project the whole graph (``C###`` aliases) and existing edges into
the selector user template, inject the persona/viewpoint layers, call
``selector_path_dsl``, parse and lock the accepted ``P`` path, normalize known
enum spellings, then call
``selector_directive_dsl`` once over the complete materializable path. Code
expands both calls' local aliases and materializes ``not_selected`` for every
other candidate.

``selected_sequence`` preserves the path row order verbatim (code never
reorders). Directive output can fill only the optional beat-control cell; it
cannot add, remove, or reorder decisions. Malformed/invalid rows become review
diagnostics, never fabricated decisions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.domain import (
    CandidateGraph,
    LlmCallRecord,
    LlmWorkUnitRef,
    Role,
    SelectionDecision,
    SelectionPlan,
    SelectionStatus,
    Treatment,
    estimate_tokens,
    selection_plan_id,
)
from tracecast.llm import LlmTransportError, call_llm
from tracecast.prompts import load_system_prompt, load_user_prompt
from tracecast.storage import RunStore, RuntimeConfig

from .dsl import (
    RawDecision,
    RawDirective,
    parse_selector_directive_dsl,
    parse_selector_dsl,
)
from .persona import PersonaConfig, render_persona_block
from .projection import (
    project_selector_directive_path,
    project_selector_graph,
    render_selector_directive_user_prompt,
    render_selector_user_prompt,
)
from .results import (
    SelectionPlanBuildOutput,
    annotate_diagnostics_with_call,
    diagnostics_to_audit_reports,
)
from .vocab import (
    SELECTOR_DIRECTIVE_ROUTE,
    SELECTOR_ROUTE,
    STAGE_SELECTION,
    diagnostic,
)

#: Patchable provider-call seam (signature-compatible with ``llm.call_llm``).
LlmCall = Callable[..., tuple[str, LlmCallRecord]]

_TREATMENT_TOKENS: dict[str, Treatment] = {
    "q": Treatment.quote,
    "quote": Treatment.quote,
    "p": Treatment.paraphrase,
    "para": Treatment.paraphrase,
    "paraphrase": Treatment.paraphrase,
    "paraphase": Treatment.paraphrase,
    "s": Treatment.summarize,
    "sum": Treatment.summarize,
    "summarize": Treatment.summarize,
}
_TREATMENT_CODES: dict[Treatment, str] = {
    Treatment.quote: "q",
    Treatment.paraphrase: "p",
    Treatment.summarize: "s",
}


@dataclass
class _PreparedDecision:
    raw: RawDecision
    candidate_id: str
    role: Role | None
    treatment: Treatment | None
    directive: str | None = None
    directive_call_id: str | None = None


@dataclass(frozen=True)
class _DirectiveCallResult:
    protocol: str | None = None
    record: LlmCallRecord | None = None


def _message(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def build_selection_plan(
    graph: CandidateGraph,
    *,
    persona: PersonaConfig | Mapping[str, Any],
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall = call_llm,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> SelectionPlanBuildOutput:
    """Build one path and its directives under ``persona`` via two global calls.

    ``persona`` is the injected viewpoint/expression config (a
    :class:`PersonaConfig` or a matching mapping). ``call`` is the patchable
    provider seam (default :func:`llm.call_llm`).
    """
    persona = PersonaConfig.coerce(persona)
    control_prompt_locale = coerce_control_prompt_locale(control_prompt_locale)
    selector_system, selector_version = load_system_prompt(
        SELECTOR_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    selector_user_template, selector_user_version = load_user_prompt(
        SELECTOR_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    selector_prompt_version = f"{selector_version}+{selector_user_version}"

    graph_projection = project_selector_graph(graph.nodes, graph.edges)
    manifest = graph_projection.manifest
    user = render_selector_user_prompt(
        template=selector_user_template,
        projection=graph_projection,
        persona_block=render_persona_block(
            persona,
            control_prompt_locale=control_prompt_locale,
        ),
        source_token_estimate=_source_token_estimate(graph, graph_projection),
        target_output_kchars=_target_output_kchars(config),
        control_prompt_locale=control_prompt_locale,
    )

    diagnostics: list[dict] = []
    budget = config.stage_budgets.get(STAGE_SELECTION, {}).get(
        "selection_max_input_tokens"
    ) or config.stage_budgets.get("planning", {}).get("selection_max_input_tokens")
    if budget is not None and estimate_tokens(user) > budget:
        diagnostics.append(
            diagnostic(STAGE_SELECTION, "selection_over_budget",
                       "selector projection exceeds selection_max_input_tokens")
        )

    text, record = call(
        operation_route=SELECTOR_ROUTE,
        messages=[_message("system", selector_system), _message("user", user)],
        config=config,
        store=store,
        prompt_version=selector_prompt_version,
        work_unit=LlmWorkUnitRef(
            kind="selection_graph",
            unit_id=graph.graph_id,
            input_refs=[node.candidate_id for node in graph.nodes],
        ),
    )
    parsed = parse_selector_dsl(text)
    annotate_diagnostics_with_call(parsed.diagnostics, record)
    diagnostics.extend(parsed.diagnostics)

    prepared = _prepare_decisions(
        parsed.decisions,
        manifest=manifest,
        record=record,
        diagnostics=diagnostics,
    )
    _report_unresolved_enums(prepared, record=record, diagnostics=diagnostics)
    directive_calls = _attach_directives(
        graph,
        prepared,
        persona=persona,
        config=config,
        store=store,
        call=call,
        primary_record=record,
        diagnostics=diagnostics,
        control_prompt_locale=control_prompt_locale,
    )
    decisions, selected_sequence = _materialize_decisions(
        graph,
        prepared,
        producer_call_id=record.call_id,
    )

    metadata: dict[str, Any] = {
        "selector_protocol": selector_prompt_version,
        "call_id": record.call_id,
    }
    if persona.host_version is not None:
        metadata["host_version"] = persona.host_version
    if persona.host_content_hash is not None:
        metadata["host_content_hash"] = persona.host_content_hash
    if directive_calls.protocol is not None:
        metadata["directive_protocol"] = directive_calls.protocol
    if directive_calls.record is not None:
        metadata["directive_call_id"] = directive_calls.record.call_id
    plan = SelectionPlan(
        plan_id=selection_plan_id(graph.project_id, persona.persona_id, selected_sequence),
        project_id=graph.project_id,
        persona_id=persona.persona_id,
        selected_sequence=selected_sequence,
        decisions=decisions,
        metadata=metadata,
    )
    return SelectionPlanBuildOutput(
        plan=plan,
        reports=diagnostics_to_audit_reports(
            diagnostics,
            artifact_ref=plan.plan_id,
        ),
    )


def _source_token_estimate(graph: CandidateGraph, projection: Any) -> int | None:
    """Best-effort soft scale hint for the selector prompt."""
    raw_input = graph.metadata.get("input")
    if isinstance(raw_input, Mapping):
        value = _positive_number(raw_input.get("material_estimated_tokens"))
        if value is not None:
            return int(round(value))
    return estimate_tokens(projection.text)


def _target_output_kchars(config: RuntimeConfig) -> float | None:
    """Optional target script size hint, configured per run when available."""
    for bucket_name in (STAGE_SELECTION, "planning", "rendering"):
        bucket = config.stage_budgets.get(bucket_name, {})
        for key in ("target_output_kchars", "target_script_kchars"):
            value = _positive_number(bucket.get(key))
            if value is not None:
                return value
    return None


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _prepare_decisions(
    raw_decisions: list[RawDecision],
    *,
    manifest: Any,
    record: LlmCallRecord,
    diagnostics: list[dict],
) -> list[_PreparedDecision]:
    """Resolve path aliases and normalize enum tokens without losing DIR."""
    prepared: list[_PreparedDecision] = []
    seen: set[str] = set()
    for raw in raw_decisions:
        try:
            candidate = manifest.resolve(raw.alias)
        except (KeyError, ValueError):
            diagnostics.append(
                _record_diagnostic(
                    record,
                    "unknown_candidate_alias",
                    f"path alias {raw.alias!r} not in graph manifest",
                    alias=raw.alias,
                )
            )
            continue
        if candidate in seen:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    "duplicate_path_candidate",
                    f"candidate {raw.alias!r} appears twice in the path",
                    alias=raw.alias,
                )
            )
            continue
        seen.add(candidate)
        try:
            role = Role(raw.role)
        except ValueError:
            role = None
        treatment = _normalize_treatment(
            raw.treatment,
            alias=raw.alias,
            record=record,
            diagnostics=diagnostics,
        )
        prepared.append(
            _PreparedDecision(
                raw=raw,
                candidate_id=candidate,
                role=role,
                treatment=treatment,
            )
        )
    return prepared


def _normalize_treatment(
    token: str,
    *,
    alias: str,
    record: LlmCallRecord,
    diagnostics: list[dict],
) -> Treatment | None:
    treatment = _treatment_from_token(token)
    if treatment is None:
        return None
    canonical = _TREATMENT_CODES[treatment]
    if token != canonical:
        diagnostics.append(
            _record_diagnostic(
                record,
                "selection_treatment_normalized",
                f"normalized treatment token {token!r} to {treatment.value!r}",
                level="info",
                usable=True,
                alias=alias,
                raw_value=token,
                normalized_value=treatment.value,
                canonical_code=canonical,
            )
        )
    return treatment


def _treatment_from_token(token: str) -> Treatment | None:
    return _TREATMENT_TOKENS.get(token.strip().casefold())


def _report_unresolved_enums(
    prepared: list[_PreparedDecision],
    *,
    record: LlmCallRecord,
    diagnostics: list[dict],
) -> None:
    for decision in prepared:
        if decision.role is None:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    "invalid_role",
                    f"role {decision.raw.role!r} not allowed; path row omitted",
                    alias=decision.raw.alias,
                    candidate_id=decision.candidate_id,
                    value=decision.raw.role,
                )
            )
        if decision.treatment is None:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    "invalid_treatment",
                    f"treatment {decision.raw.treatment!r} not allowed; path row omitted",
                    alias=decision.raw.alias,
                    candidate_id=decision.candidate_id,
                    value=decision.raw.treatment,
                )
            )


def _attach_directives(
    graph: CandidateGraph,
    prepared: list[_PreparedDecision],
    *,
    persona: PersonaConfig,
    config: RuntimeConfig,
    store: RunStore,
    call: LlmCall,
    primary_record: LlmCallRecord,
    diagnostics: list[dict],
    control_prompt_locale: ControlPromptLocale,
) -> _DirectiveCallResult:
    locked = [
        decision
        for decision in prepared
        if decision.role is not None and decision.treatment is not None
    ]
    if not locked:
        return _DirectiveCallResult()
    if SELECTOR_DIRECTIVE_ROUTE not in config.routes:
        diagnostics.append(
            _record_diagnostic(
                primary_record,
                "selection_directive_unavailable",
                "selector directive route is not configured; locked path kept with null directives",
                candidate_ids=[decision.candidate_id for decision in locked],
            )
        )
        return _DirectiveCallResult()

    projection = project_selector_directive_path(
        graph.nodes,
        [
            (
                decision.candidate_id,
                decision.role.value,
                decision.treatment.value,
            )
            for decision in locked
            if decision.role is not None and decision.treatment is not None
        ],
    )
    directive_system, system_version = load_system_prompt(
        SELECTOR_DIRECTIVE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    directive_template, user_version = load_user_prompt(
        SELECTOR_DIRECTIVE_ROUTE,
        config,
        control_prompt_locale=control_prompt_locale,
    )
    protocol = f"{system_version}+{user_version}"
    directive_user = render_selector_directive_user_prompt(
        template=directive_template,
        projection=projection,
        persona_block=render_persona_block(
            persona,
            control_prompt_locale=control_prompt_locale,
        ),
    )
    try:
        text, record = call(
            operation_route=SELECTOR_DIRECTIVE_ROUTE,
            messages=[
                _message("system", directive_system),
                _message("user", directive_user),
            ],
            config=config,
            store=store,
            prompt_version=protocol,
            work_unit=LlmWorkUnitRef(
                kind="selection_directives",
                unit_id=f"{graph.graph_id}:directives",
                input_refs=[decision.candidate_id for decision in locked],
                metadata={"path_call_id": primary_record.call_id},
            ),
        )
    except LlmTransportError as exc:
        diagnostics.append(
            _record_diagnostic(
                exc.record,
                "selection_directive_failed",
                "whole-path directive call failed; locked path kept with null directives",
                candidate_ids=[decision.candidate_id for decision in locked],
                path_call_id=primary_record.call_id,
            )
        )
        return _DirectiveCallResult(protocol=protocol, record=exc.record)

    parsed = parse_selector_directive_dsl(text)
    annotate_diagnostics_with_call(parsed.diagnostics, record)
    diagnostics.extend(parsed.diagnostics)
    locked_by_alias = {
        projection.manifest.alias_for(decision.candidate_id): decision
        for decision in locked
    }
    accepted_aliases = _apply_directive_rows(
        parsed.directives,
        expected=locked_by_alias,
        record=record,
        diagnostics=diagnostics,
    )
    pending_aliases = [
        alias for alias in locked_by_alias if alias not in accepted_aliases
    ]
    for alias in pending_aliases:
        diagnostics.append(
            _record_diagnostic(
                record,
                "directive_missing_alias",
                f"whole-path directive output omitted or invalidated {alias!r}",
                alias=alias,
                candidate_id=locked_by_alias[alias].candidate_id,
            )
        )
    return _DirectiveCallResult(
        protocol=protocol,
        record=record,
    )


def _apply_directive_rows(
    rows: list[RawDirective],
    *,
    expected: Mapping[str, _PreparedDecision],
    record: LlmCallRecord,
    diagnostics: list[dict],
) -> set[str]:
    rows_by_alias: dict[str, list[RawDirective]] = {}
    for row in rows:
        rows_by_alias.setdefault(row.alias, []).append(row)
    accepted: set[str] = set()
    for alias, alias_rows in rows_by_alias.items():
        decision = expected.get(alias)
        if decision is None:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    "directive_unknown_alias",
                    f"directive alias {alias!r} is not in the locked path",
                    alias=alias,
                )
            )
            continue
        if len(alias_rows) != 1:
            diagnostics.append(
                _record_diagnostic(
                    record,
                    "directive_duplicate_alias",
                    f"directive alias {alias!r} appears more than once",
                    alias=alias,
                    candidate_id=decision.candidate_id,
                )
            )
            continue
        decision.directive = alias_rows[0].directive
        decision.directive_call_id = record.call_id
        accepted.add(alias)
    return accepted


def _record_diagnostic(
    record: LlmCallRecord,
    kind: str,
    message: str,
    **context: Any,
) -> dict[str, Any]:
    item = diagnostic(STAGE_SELECTION, kind, message, **context)
    annotate_diagnostics_with_call([item], record)
    return item


def _materialize_decisions(
    graph: CandidateGraph,
    prepared: list[_PreparedDecision],
    *,
    producer_call_id: str,
) -> tuple[list[SelectionDecision], list[str]]:
    """Build accepted rows in primary order, then code-fill ``not_selected``."""
    accepted: dict[str, SelectionDecision] = {}
    selected_sequence: list[str] = []
    for decision in prepared:
        if decision.role is None or decision.treatment is None:
            continue
        metadata: dict[str, Any] = {
            "producer_unit_id": graph.graph_id,
            "producer_call_id": producer_call_id,
        }
        if decision.directive_call_id is not None:
            metadata["directive_call_id"] = decision.directive_call_id
        accepted[decision.candidate_id] = SelectionDecision(
            candidate_id=decision.candidate_id,
            status=SelectionStatus.accepted,
            role=decision.role,
            treatment=decision.treatment,
            directive=decision.directive,
            metadata=metadata,
        )
        selected_sequence.append(decision.candidate_id)

    decisions = [accepted[c] for c in selected_sequence]
    for node in graph.nodes:
        if node.candidate_id not in accepted:
            decisions.append(
                SelectionDecision(
                    candidate_id=node.candidate_id,
                    status=SelectionStatus.not_selected,
                )
            )
    return decisions, selected_sequence
