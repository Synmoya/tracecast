"""Pure compilation of model proposals and operator name overrides."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from tracecast.domain import (
    AuditReportItem,
    NameOverride,
    NameResolutionRule,
    issue_id,
    name_resolution_rule_id,
)

from .corpus import ExposureItem, SelectedMaterial
from .matching import contains_surface, iter_surface_spans, stable_name_script_mismatch

STAGE_NAME_RESOLUTION = "name_resolution"
ProposalSource = Literal["model", "operator"]


class NameResolutionError(RuntimeError):
    def __init__(self, message: str, reports: Sequence[AuditReportItem]) -> None:
        super().__init__(message)
        self.reports = list(reports)


@dataclass(frozen=True)
class ProposedRule:
    aliases: tuple[str, ...]
    stable_name: str
    source: ProposalSource
    call_id: str | None = None


@dataclass(frozen=True)
class CompileResult:
    rules: list[NameResolutionRule]
    protected_surfaces: list[str]
    reports: list[AuditReportItem]
    accepted_model_rules: int


def compile_name_table(
    proposals: Sequence[ProposedRule],
    protected_surfaces: Sequence[str],
    *,
    overrides: Sequence[NameOverride],
    output_language: str,
    project_id: str,
    materials: Sequence[SelectedMaterial],
    exposure_items: Sequence[ExposureItem],
    call_id: str | None = None,
    raw_response_path: str | None = None,
) -> CompileResult:
    """Validate literal model output, apply operator authority, and build rules."""

    reports: list[AuditReportItem] = []
    operator_rules = _compile_override_proposals(
        overrides,
        output_language=output_language,
        exposure_items=exposure_items,
    )
    model_rules, model_reports = _validate_model_proposals(
        proposals,
        materials=materials,
        call_id=call_id,
        raw_response_path=raw_response_path,
    )
    reports.extend(model_reports)
    model_rules, bridge_reports = _drop_operator_bridge_proposals(
        model_rules,
        operator_rules=operator_rules,
        call_id=call_id,
        raw_response_path=raw_response_path,
    )
    reports.extend(bridge_reports)
    components = _components([*operator_rules, *model_rules])
    rules: list[NameResolutionRule] = []
    accepted_model_rule_ids: set[int] = set()
    for component in components:
        compiled, component_reports, accepted_indexes = _compile_component(
            component,
            project_id=project_id,
            output_language=output_language,
            materials=materials,
            exposure_items=exposure_items,
            call_id=call_id,
            raw_response_path=raw_response_path,
        )
        reports.extend(component_reports)
        if compiled is not None:
            rules.append(compiled)
            accepted_model_rule_ids.update(accepted_indexes)
    rules.sort(key=lambda rule: (rule.evidence_chunk_ids, rule.aliases))

    aliases = {alias for rule in rules for alias in rule.aliases}
    operator_aliases = {
        alias
        for rule in rules
        for alias in (
            rule.aliases
            if rule.metadata.get("source") == "operator"
            else rule.metadata.get("operator_aliases") or []
        )
    }
    protections, protection_reports = _compile_protections(
        protected_surfaces,
        aliases=aliases - operator_aliases,
        operator_aliases=operator_aliases,
        materials=materials,
        call_id=call_id,
        raw_response_path=raw_response_path,
    )
    reports.extend(protection_reports)
    return CompileResult(
        rules=rules,
        protected_surfaces=protections,
        reports=reports,
        accepted_model_rules=len(accepted_model_rule_ids),
    )


def normalize_surface(surface: str) -> str:
    """Remove only speaker-label delimiters that must survive replacement."""

    return surface.strip().rstrip(":：").strip()


def build_report(
    kind: str,
    message: str,
    *,
    artifact_ref: str | None,
    call_id: str | None = None,
    raw_response_path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    level: str = "warning",
    usable: bool = True,
) -> AuditReportItem:
    return AuditReportItem(
        issue_id=issue_id(STAGE_NAME_RESOLUTION, kind, artifact_ref, message),
        stage=STAGE_NAME_RESOLUTION,
        kind=kind,
        level=level,
        message=message,
        usable=usable,
        artifact_ref=artifact_ref,
        trace_refs=[artifact_ref] if artifact_ref else [],
        call_id=call_id,
        raw_response_path=raw_response_path,
        metadata=dict(metadata or {}),
    )


def _compile_override_proposals(
    overrides: Sequence[NameOverride],
    *,
    output_language: str,
    exposure_items: Sequence[ExposureItem],
) -> list[ProposedRule]:
    target_by_alias: dict[str, str] = {}
    proposals: list[ProposedRule] = []
    for override in overrides:
        if override.output_language != output_language:
            continue
        stable_name = override.target_surface.strip()
        aliases = tuple(
            dict.fromkeys(
                normalized
                for alias in override.aliases
                if (normalized := normalize_surface(alias))
                and any(
                    contains_surface(item.content, normalized)
                    for item in exposure_items
                )
            )
        )
        if not aliases:
            continue
        for alias in aliases:
            previous = target_by_alias.get(alias)
            if previous is not None and previous != stable_name:
                report = build_report(
                    "name_override_target_conflict",
                    f"override alias '{alias}' has conflicting stable names: "
                    f"{previous}, {stable_name}",
                    artifact_ref="name_resolution",
                    level="error",
                    usable=False,
                )
                raise NameResolutionError(report.message, [report])
            target_by_alias[alias] = stable_name
        proposals.append(
            ProposedRule(
                aliases=aliases,
                stable_name=stable_name,
                source="operator",
            )
        )
    return proposals


def _validate_model_proposals(
    proposals: Sequence[ProposedRule],
    *,
    materials: Sequence[SelectedMaterial],
    call_id: str | None,
    raw_response_path: str | None,
) -> tuple[list[tuple[int, ProposedRule]], list[AuditReportItem]]:
    reports: list[AuditReportItem] = []
    validated: list[tuple[int, ProposedRule]] = []
    for index, proposal in enumerate(proposals):
        aliases = tuple(
            dict.fromkeys(
                normalized
                for alias in proposal.aliases
                if (normalized := normalize_surface(alias))
            )
        )
        present = tuple(
            alias
            for alias in aliases
            if any(contains_surface(material.content, alias) for material in materials)
        )
        missing = sorted(set(aliases) - set(present))
        if missing:
            reports.append(
                build_report(
                    "name_table_invalid_entry",
                    "replacement aliases are absent from selected materials: "
                    + ", ".join(missing),
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                    metadata={"entry_index": index},
                )
            )
            continue
        if not present:
            continue
        if all(alias == proposal.stable_name for alias in present):
            reports.append(
                build_report(
                    "name_table_identity_entry",
                    "replacement entry has no render-visible effect and was omitted",
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                    metadata={"entry_index": index},
                )
            )
            continue
        validated.append(
            (
                index,
                ProposedRule(
                    aliases=present,
                    stable_name=proposal.stable_name,
                    source="model",
                    call_id=proposal.call_id,
                ),
            )
        )

    duplicates = {
        alias
        for _index, proposal in validated
        for alias in proposal.aliases
        if sum(alias in other.aliases for _other_index, other in validated) > 1
    }
    if duplicates:
        reports.append(
            build_report(
                "name_table_alias_conflict",
                "model assigned aliases to multiple replacement entries: "
                + ", ".join(sorted(duplicates)),
                artifact_ref="name_resolution",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        )
        validated = [
            (index, proposal)
            for index, proposal in validated
            if not duplicates.intersection(proposal.aliases)
        ]
    return validated, reports


def _drop_operator_bridge_proposals(
    proposals: Sequence[tuple[int, ProposedRule]],
    *,
    operator_rules: Sequence[ProposedRule],
    call_id: str | None,
    raw_response_path: str | None,
) -> tuple[list[tuple[int, ProposedRule]], list[AuditReportItem]]:
    """Reject model entries that would join distinct operator decisions."""

    accepted: list[tuple[int, ProposedRule]] = []
    reports: list[AuditReportItem] = []
    for index, proposal in proposals:
        targets = {
            operator.stable_name
            for operator in operator_rules
            if set(proposal.aliases).intersection(operator.aliases)
        }
        if len(targets) <= 1:
            accepted.append((index, proposal))
            continue
        reports.append(
            build_report(
                "name_table_operator_bridge",
                "model replacement overlaps different operator stable names and "
                "was omitted",
                artifact_ref="name_resolution",
                call_id=call_id,
                raw_response_path=raw_response_path,
                metadata={"entry_index": index},
            )
        )
    return accepted, reports


def _components(
    proposals: Sequence[ProposedRule | tuple[int, ProposedRule]],
) -> list[list[tuple[int | None, ProposedRule]]]:
    normalized = [
        (row[0], row[1]) if isinstance(row, tuple) else (None, row)
        for row in proposals
    ]
    components: list[list[tuple[int | None, ProposedRule]]] = []
    for indexed in normalized:
        aliases = set(indexed[1].aliases)
        matches = [
            component
            for component in components
            if aliases.intersection(
                alias for _index, proposal in component for alias in proposal.aliases
            )
        ]
        if not matches:
            components.append([indexed])
            continue
        merged = [indexed]
        for component in matches:
            merged.extend(component)
            components.remove(component)
        components.append(merged)
    return components


def _compile_component(
    component: Sequence[tuple[int | None, ProposedRule]],
    *,
    project_id: str,
    output_language: str,
    materials: Sequence[SelectedMaterial],
    exposure_items: Sequence[ExposureItem],
    call_id: str | None,
    raw_response_path: str | None,
) -> tuple[NameResolutionRule | None, list[AuditReportItem], set[int]]:
    reports: list[AuditReportItem] = []
    aliases = tuple(
        sorted({alias for _index, proposal in component for alias in proposal.aliases})
    )
    operator_aliases = {
        alias
        for _index, proposal in component
        if proposal.source == "operator"
        for alias in proposal.aliases
    }
    operator_targets = {
        proposal.stable_name
        for _index, proposal in component
        if proposal.source == "operator"
    }
    model_targets = {
        proposal.stable_name
        for _index, proposal in component
        if proposal.source == "model"
    }
    if len(operator_targets) > 1:
        report = build_report(
            "name_override_target_conflict",
            "overlapping operator name overrides have different stable names",
            artifact_ref="name_resolution",
            level="error",
            usable=False,
        )
        raise NameResolutionError(report.message, [report])
    if operator_targets:
        stable_name = next(iter(operator_targets))
    elif len(model_targets) == 1:
        stable_name = next(iter(model_targets))
    else:
        reports.append(
            build_report(
                "name_table_target_conflict",
                "overlapping model entries disagree on the stable name and were omitted",
                artifact_ref="name_resolution",
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        )
        return None, reports, set()

    chunk_ids = tuple(
        sorted(
            {
                chunk_id
                for material in materials
                if any(contains_surface(material.content, alias) for alias in aliases)
                for chunk_id in material.chunk_ids
            }
            | {
                item.chunk_id
                for item in exposure_items
                if any(
                    contains_surface(item.content, alias) for alias in operator_aliases
                )
            }
        )
    )
    if not chunk_ids:
        return None, reports, set()
    sources = {proposal.source for _index, proposal in component}
    metadata: dict[str, Any] = {
        "source": (
            "operator+model"
            if sources == {"operator", "model"}
            else next(iter(sources))
        )
    }
    if sources == {"operator", "model"}:
        metadata["operator_aliases"] = sorted(operator_aliases)
    model_call_ids = sorted(
        {
            proposal.call_id
            for _index, proposal in component
            if proposal.source == "model" and proposal.call_id is not None
        }
    )
    if model_call_ids:
        metadata["model_call_ids"] = model_call_ids
    if stable_name_script_mismatch(stable_name, output_language):
        metadata["script_mismatch"] = True
        reports.append(
            build_report(
                "name_stable_script_mismatch",
                f"stable_name '{stable_name}' for aliases {', '.join(aliases)} "
                f"contains no {output_language} script characters",
                artifact_ref=chunk_ids[0],
                call_id=call_id,
                raw_response_path=raw_response_path,
            )
        )
    accepted_indexes = {
        index
        for index, proposal in component
        if index is not None and proposal.source == "model"
    }
    return (
        NameResolutionRule(
            rule_id=name_resolution_rule_id(project_id, aliases, chunk_ids),
            aliases=list(aliases),
            stable_name=stable_name,
            evidence_chunk_ids=list(chunk_ids),
            metadata=metadata,
        ),
        reports,
        accepted_indexes,
    )


def _compile_protections(
    surfaces: Sequence[str],
    *,
    aliases: set[str],
    operator_aliases: set[str],
    materials: Sequence[SelectedMaterial],
    call_id: str | None,
    raw_response_path: str | None,
) -> tuple[list[str], list[AuditReportItem]]:
    reports: list[AuditReportItem] = []
    protected: list[str] = []
    for surface in dict.fromkeys(value.strip() for value in surfaces if value.strip()):
        if surface in aliases or surface in operator_aliases:
            reports.append(
                build_report(
                    "name_table_invalid_protection",
                    f"surface '{surface}' cannot be both replaced and protected",
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                )
            )
            continue
        blocked_operator_aliases = sorted(
            alias
            for alias in operator_aliases
            if next(iter_surface_spans(surface, alias), None) is not None
        )
        if blocked_operator_aliases:
            reports.append(
                build_report(
                    "name_table_invalid_protection",
                    f"protected surface '{surface}' would block operator aliases: "
                    + ", ".join(blocked_operator_aliases),
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                )
            )
            continue
        if not any(contains_surface(material.content, surface) for material in materials):
            reports.append(
                build_report(
                    "name_table_invalid_protection",
                    f"protected surface '{surface}' is absent from selected materials",
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                )
            )
            continue
        relevant = any(
            len(alias) < len(surface)
            and next(iter_surface_spans(surface, alias), None) is not None
            for alias in aliases
        )
        if not relevant:
            reports.append(
                build_report(
                    "name_table_irrelevant_protection",
                    f"protected surface '{surface}' shields no accepted shorter alias",
                    artifact_ref="name_resolution",
                    call_id=call_id,
                    raw_response_path=raw_response_path,
                )
            )
            continue
        protected.append(surface)
    return sorted(protected, key=lambda value: (-len(value), value)), reports
