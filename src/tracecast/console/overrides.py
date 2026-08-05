"""Request-scoped overrides: config (routes/budgets), Host patch, prompt preflight.

Overrides never touch ``configs/runtime.json`` or ``hosts/<host-id>.json``
on disk -- they are merged in memory per request and snapshotted into the derived
run's console manifest, so every derived run records exactly what it ran with.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tracecast.control_prompt import ControlPromptLocale
from tracecast.hosts import HostProfile
from tracecast.prompt_components import (
    load_ingestion_content_kind_catalog,
    load_ingestion_tag_catalog,
    load_planning_protocol_catalog,
    load_rendering_protocol_catalog,
)
from tracecast.prompts import load_route_prompt
from tracecast.rendering import FACT_CONTEXT_ROUTE
from tracecast.storage import RuntimeConfig

from .errors import BadRequest

# --- config overrides -------------------------------------------------------


class RouteOverride(BaseModel):
    """Per-route knobs a rerun may replace (all optional, ``None`` = inherit)."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None
    repair_prompt: str | None = None
    extra_body: dict[str, Any] | None = None
    thinking: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = Field(default=None, ge=0)
    timeout_seconds: int | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, ge=0)
    transport_max_attempts: int | None = Field(default=None, ge=1)
    transport_backoff_base_seconds: float | None = Field(default=None, gt=0)
    transport_backoff_max_seconds: float | None = Field(default=None, gt=0)
    transport_jitter_ratio: float | None = Field(default=None, ge=0, le=1)


class ConfigOverrides(BaseModel):
    """Deep-merge payload over the loaded ``RuntimeConfig``."""

    model_config = ConfigDict(extra="forbid")

    stage_budgets: dict[str, dict[str, bool | int | float]] = Field(default_factory=dict)
    routes: dict[str, RouteOverride] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.stage_budgets and not self.routes


def apply_config_overrides(
    base: RuntimeConfig, overrides: ConfigOverrides | None
) -> RuntimeConfig:
    """Merge ``overrides`` onto ``base`` and re-validate.

    Budget buckets merge per key; route overrides replace only the provided
    fields. Overriding an unknown route is rejected -- a rerun tweaks existing
    routes, it does not define new ones.
    """
    if overrides is None or overrides.is_empty():
        effective = base
    else:
        data = base.model_dump()
        budgets = data.setdefault("stage_budgets", {})
        for bucket, values in overrides.stage_budgets.items():
            merged = dict(budgets.get(bucket) or {})
            merged.update(values)
            budgets[bucket] = merged
        routes = data.setdefault("routes", {})
        for name, override in overrides.routes.items():
            if name not in routes:
                raise BadRequest(f"config override targets unknown route {name!r}")
            routes[name].update(override.model_dump(exclude_none=True))
        effective = RuntimeConfig.model_validate(data)
    validate_console_config(effective)
    return effective


def validate_console_config(config: RuntimeConfig) -> None:
    """Reject invalid spend configuration before estimation or job creation."""
    positive_budget_fields = {
        "source_piece_max_tokens",
        "atom_window_max_input_tokens",
        "pieces_per_call_soft_limit",
        "node_window_max_input_tokens",
        "target_material_tokens_per_node",
        "max_final_node_count",
        "target_final_node_count",
        "max_compression_rounds",
        "node_compress_max_input_tokens",
        "selection_max_input_tokens",
        "target_output_kchars",
        "batch_max_input_tokens",
        "max_nodes_per_batch",
        "target_material_tokens",
        "max_material_tokens",
        "max_materials_per_chunk",
        "max_nodes_per_chunk",
        "render_max_input_tokens",
        "fact_context_min_tokens",
        "fact_context_max_tokens",
    }
    for bucket, values in config.stage_budgets.items():
        for key, value in values.items():
            if key in positive_budget_fields and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                raise BadRequest(f"stage_budgets.{bucket}.{key} must be positive")

    rendering = config.stage_budgets.get("rendering", {})
    target = rendering.get("target_material_tokens")
    maximum = rendering.get("max_material_tokens")
    if isinstance(target, (int, float)) and isinstance(maximum, (int, float)):
        if target > maximum:
            raise BadRequest("target_material_tokens must be <= max_material_tokens")
    ratio = rendering.get("fact_context_ratio")
    if ratio is not None and (
        isinstance(ratio, bool)
        or not isinstance(ratio, (int, float))
        or not 0 < ratio <= 1
    ):
        raise BadRequest("fact_context_ratio must be in (0, 1]")
    minimum_context = rendering.get("fact_context_min_tokens")
    maximum_context = rendering.get("fact_context_max_tokens")
    if isinstance(minimum_context, (int, float)) and isinstance(
        maximum_context, (int, float)
    ) and minimum_context > maximum_context:
        raise BadRequest("fact_context_min_tokens must be <= fact_context_max_tokens")
    if rendering.get("prefer_node_boundary", True) is not True:
        raise BadRequest("v1 requires prefer_node_boundary=true")

    for name, route in config.routes.items():
        for field in (
            "max_output_tokens",
            "timeout_seconds",
            "max_concurrency",
            "transport_max_attempts",
            "transport_backoff_base_seconds",
            "transport_backoff_max_seconds",
        ):
            value = getattr(route, field)
            if value is not None and value <= 0:
                raise BadRequest(f"routes.{name}.{field} must be positive")
        if route.transport_backoff_base_seconds > route.transport_backoff_max_seconds:
            raise BadRequest(
                f"routes.{name}.transport_backoff_base_seconds must be "
                "<= transport_backoff_max_seconds"
            )
        temperature = route.temperature
        if name == FACT_CONTEXT_ROUTE and temperature != 0:
            raise BadRequest(f"routes.{name}.temperature must be 0")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 2
        ):
            raise BadRequest(f"routes.{name}.temperature must be in [0, 2]")


# --- persona patch ----------------------------------------------------------


class ViewpointProfilePatch(BaseModel):
    """Leaf-level replacement for the Selection feedback layer."""

    model_config = ConfigDict(extra="forbid")

    prefer: list[str] | None = None
    avoid: list[str] | None = None
    human_directive: list[str] | None = None


class PersonaPatch(BaseModel):
    """Transient Host override; omitted fields inherit the selected Host."""

    model_config = ConfigDict(extra="forbid")

    viewpoint: str | None = None
    expression: str | None = None
    viewpoint_profile: ViewpointProfilePatch | None = None

    def is_empty(self) -> bool:
        return (
            self.viewpoint is None
            and self.expression is None
            and (self.viewpoint_profile is None or self.viewpoint_profile.model_dump(
                exclude_none=True
            ) == {})
        )


def apply_persona_patch(persona: Mapping[str, Any], patch: PersonaPatch | None) -> dict[str, Any]:
    """Apply a leaf-wholesale persona patch (provided lists replace, not append)."""
    effective = dict(persona)
    if patch is None:
        return effective
    if patch.viewpoint is not None:
        effective["viewpoint"] = patch.viewpoint
    if patch.expression is not None:
        effective["expression"] = patch.expression
    if patch.viewpoint_profile is not None:
        profile = dict(effective.get("viewpoint_profile") or {})
        for key, value in patch.viewpoint_profile.model_dump(exclude_none=True).items():
            profile[key] = value
        effective["viewpoint_profile"] = profile
    return effective


def apply_host_patch(host: HostProfile, patch: PersonaPatch | None) -> HostProfile:
    """Apply transient Console feedback without creating another persisted host layer."""

    if patch is None or patch.is_empty():
        return host
    data = host.model_dump(mode="json")
    thinking = data["host_thinking"]
    expression = data["host_expression"]
    if patch.viewpoint is not None:
        thinking["viewpoint"] = patch.viewpoint
    if patch.expression is not None:
        expression["description"] = patch.expression
    if patch.viewpoint_profile is not None:
        for key, value in patch.viewpoint_profile.model_dump(exclude_none=True).items():
            thinking[key] = value
    return HostProfile.model_validate(data)


# --- prompt preflight / catalog reload --------------------------------------

_PROMPT_FIELDS = ("system_prompt", "user_prompt", "repair_prompt")


def preflight_prompts(
    config: RuntimeConfig,
    routes: Iterable[str],
    *,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> dict[str, dict[str, str]]:
    """Eagerly load every configured prompt of the given routes before any spend.

    Returns ``{route: {field: version}}``; a missing file, missing version
    header, or empty body surfaces as a 400 instead of failing mid-job.
    """
    versions: dict[str, dict[str, str]] = {}
    for name in routes:
        try:
            route_cfg = config.route(name)
        except KeyError as exc:
            raise BadRequest(str(exc)) from exc
        loaded: dict[str, str] = {}
        for field in _PROMPT_FIELDS:
            if not getattr(route_cfg, field, None):
                continue
            try:
                _body, version = load_route_prompt(
                    name,
                    config,
                    field,
                    control_prompt_locale=control_prompt_locale,
                )
            except (OSError, ValueError, KeyError) as exc:
                raise BadRequest(f"route {name!r} {field}: {exc}") from exc
            loaded[field] = version
        versions[name] = loaded
    return versions


def reload_component_caches() -> list[str]:
    """Clear the ``@cache``d prompt-component catalogs so edits take effect."""
    loaders = (
        load_ingestion_content_kind_catalog,
        load_ingestion_tag_catalog,
        load_planning_protocol_catalog,
        load_rendering_protocol_catalog,
    )
    for loader in loaders:
        loader.cache_clear()
    return [loader.__name__ for loader in loaders]


def route_snapshot(config: RuntimeConfig, routes: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Manifest-facing snapshot of the effective route configs (no secrets)."""
    snapshot: dict[str, dict[str, Any]] = {}
    for name in routes:
        route_cfg = config.route(name)
        snapshot[name] = route_cfg.model_dump(exclude_none=True)
    return snapshot
