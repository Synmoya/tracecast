"""First-run and day-to-day model connection setup for the local Console.

This module owns local configuration writes.  It never returns a provider secret,
keeps the secret in ``.env`` only, and validates the complete runtime payload before
atomically creating or updating ``runtime.json``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from tracecast.storage import RuntimeConfig

from .context import ConsoleContext
from .errors import BadRequest, Conflict

API_KEY_ENV = "TRACECAST_LLM_API_KEY"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_ENV_ASSIGNMENT_RE = re.compile(rf"^[ \t]*(?:export[ \t]+)?{API_KEY_ENV}[ \t]*=")


_ROUTES: dict[str, tuple[str, str | None, str | None, str | None, int, int]] = {
    "atom_boundary_dsl": (
        "atomizer", "prompt/ingestion/atom_boundary_dsl.system.{locale}.md",
        "prompt/ingestion/atom_boundary_dsl.user.{locale}.md",
        "prompt/ingestion/atom_boundary_dsl.repair.{locale}.md", 40960, 8,
    ),
    "source_profile_probe": (
        "intake", "prompt/intake/source_profile_probe.system.{locale}.md",
        "prompt/intake/source_profile_probe.user.{locale}.md", None, 4096, 1,
    ),
    "host_profile_distill": (
        "host_builder", "prompt/host_builder/host_profile_distill.system.{locale}.md",
        "prompt/host_builder/host_profile_distill.user.{locale}.md", None, 8192, 1,
    ),
    "host_style_example_reverse": (
        "host_builder", "prompt/host_builder/host_style_example_reverse.system.{locale}.md",
        "prompt/host_builder/host_style_example_reverse.user.{locale}.md", None, 8192, 1,
    ),
    "planner_graph_dsl": (
        "planner", "prompt/planning/planner_graph_dsl.system.{locale}.md",
        "prompt/planning/planner_graph_dsl.user.{locale}.md", None, 40960, 6,
    ),
    "planner_node_compress_dsl": (
        "planner", "prompt/planning/planner_node_compress_dsl.system.{locale}.md",
        "prompt/planning/planner_node_compress_dsl.user.{locale}.md", None, 40960, 6,
    ),
    "selector_path_dsl": (
        "selector", "prompt/selection/selector_path_dsl.system.{locale}.md",
        "prompt/selection/selector_path_dsl.user.{locale}.md", None, 40960, 1,
    ),
    "selector_directive_dsl": (
        "selector", "prompt/selection/selector_directive_dsl.system.{locale}.md",
        "prompt/selection/selector_directive_dsl.user.{locale}.md", None, 40960, 1,
    ),
    "node_edit": (
        "node_editor", "prompt/node_edit/node_edit.system.{locale}.md",
        "prompt/node_edit/node_edit.user.{locale}.md", None, 40960, 8,
    ),
    "node_edit_block_repair": (
        "node_editor", "prompt/node_edit/node_edit.system.{locale}.md",
        "prompt/node_edit/node_edit_block_repair.user.{locale}.md", None, 40960, 8,
    ),
    "name_table": (
        "name_resolution", "prompt/name_resolution/name_table.system.{locale}.md",
        "prompt/name_resolution/name_table.user.{locale}.md", None, 4096, 1,
    ),
    "render_fact_context_dsl": (
        "renderer", "prompt/rendering/render_fact_context_dsl.system.{locale}.md",
        "prompt/rendering/render_fact_context_dsl.user.{locale}.md", None, 8192, 1,
    ),
    "render_chunk": (
        "renderer", "prompt/rendering/render_chunk.system.{locale}.md",
        "prompt/rendering/render_chunk.user.{locale}.md", None, 40960, 8,
    ),
}

_SELECTION_ROUTES = {"selector_path_dsl", "selector_directive_dsl"}

_STAGE_BUDGETS: dict[str, dict[str, bool | int | float]] = {
    "ingestion": {
        "source_piece_max_tokens": 120,
        "atom_window_max_input_tokens": 32768,
        "pieces_per_call_soft_limit": 128,
    },
    "planning": {
        "node_window_max_input_tokens": 16000,
        "target_material_tokens_per_node": 650,
        "max_final_node_count": 170,
        "max_compression_rounds": 4,
        "selection_max_input_tokens": 65536,
    },
    "node_edit": {"batch_max_input_tokens": 32768, "max_nodes_per_batch": 5},
    "name_resolution": {"table_max_input_tokens": 49152, "max_rules": 256},
    "rendering": {
        "render_max_input_tokens": 65536,
        "target_material_tokens": 3000,
        "max_material_tokens": 4500,
        "max_materials_per_chunk": 80,
        "fact_context_ratio": 0.1,
        "fact_context_min_tokens": 80,
        "fact_context_max_tokens": 320,
        "prefer_node_boundary": True,
    },
}


def setup_status(ctx: ConsoleContext) -> dict[str, Any]:
    """Return first-run state without exposing the configured secret."""

    runtime_state = "missing"
    runtime_error: str | None = None
    runtime_config: RuntimeConfig | None = None
    if ctx.config_path.is_file():
        try:
            runtime_config = RuntimeConfig.load(ctx.config_path, load_env=False)
            runtime_state = "ready"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            runtime_state = "invalid"
            runtime_error = "runtime configuration could not be validated"
    try:
        profiles_payload = json.loads(ctx.profiles_path.read_text(encoding="utf-8"))
        source_profiles = [
            {"id": name, "description": str(profile.get("description") or "")}
            for name, profile in profiles_payload.get("profiles", {}).items()
        ]
    except (OSError, TypeError, ValueError):
        source_profiles = []
    return {
        "runtime": {
            "state": runtime_state,
            "configured": runtime_state == "ready",
            "path": str(ctx.config_path),
            "error": runtime_error,
        },
        "api_key_configured": _runtime_has_configured_key(ctx.env_path, runtime_config)
        if runtime_config is not None
        else bool(os.environ.get(API_KEY_ENV) or _dotenv_has_nonempty_key(ctx.env_path)),
        "api_key_env": API_KEY_ENV,
        "source_profiles": source_profiles,
    }


def update_runtime(
    ctx: ConsoleContext,
    *,
    gateway_url: str,
    api_key: str,
    standard_model: str,
    selection_model: str | None,
    completion_path: str,
    max_output_tokens_field: Literal["max_tokens", "max_completion_tokens"],
    extra_body: dict[str, Any] | None,
    confirm: bool,
) -> dict[str, Any]:
    """Create a runtime or safely update one single-provider connection."""

    if not confirm:
        raise BadRequest("runtime update requires explicit confirm=true")
    gateway_url = gateway_url.strip().rstrip("/")
    standard_model = standard_model.strip()
    selection_model = (selection_model or "").strip() or standard_model
    if not gateway_url or not standard_model:
        raise BadRequest("gateway_url and standard_model are required")
    parsed = urlsplit(gateway_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BadRequest("gateway_url must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BadRequest("gateway_url cannot contain userinfo, query, or fragment")
    if not api_key or "\r" in api_key or "\n" in api_key:
        raise BadRequest("api_key must be non-empty and single-line")

    existing: RuntimeConfig | None = None
    if ctx.config_path.is_file():
        try:
            existing = RuntimeConfig.load(ctx.config_path, load_env=False)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            existing = None
    try:
        if existing is None:
            config, provider_name = _new_quick_runtime(
                gateway_url=gateway_url,
                standard_model=standard_model,
                selection_model=selection_model,
                completion_path=completion_path,
                max_output_tokens_field=max_output_tokens_field,
                extra_body=extra_body,
            )
            update_mode = "created"
        else:
            config, provider_name = _update_single_provider_runtime(
                existing,
                gateway_url=gateway_url,
                standard_model=standard_model,
                selection_model=selection_model,
                completion_path=completion_path,
                max_output_tokens_field=max_output_tokens_field,
                extra_body=extra_body,
            )
            update_mode = "updated"
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"invalid runtime configuration: {exc}") from exc

    old_env = ctx.env_path.read_bytes() if ctx.env_path.exists() else None
    try:
        _write_dotenv_key(ctx.env_path, api_key)
        if _read_dotenv_key(ctx.env_path) != api_key:
            raise RuntimeError("secret file round-trip validation failed")
        _atomic_write_json(ctx.config_path, config.model_dump(mode="json", exclude_none=True))
    except Exception:
        _restore_file(ctx.env_path, old_env)
        raise
    os.environ[API_KEY_ENV] = api_key
    return {
        "configured": True,
        "provider": provider_name,
        "standard_model": standard_model,
        "selection_model": selection_model,
        "selection_model_inherits": selection_model == standard_model,
        "api_key_configured": True,
        "update_mode": update_mode,
    }


def _new_quick_runtime(
    *,
    gateway_url: str,
    standard_model: str,
    selection_model: str,
    completion_path: str,
    max_output_tokens_field: Literal["max_tokens", "max_completion_tokens"],
    extra_body: dict[str, Any] | None,
) -> tuple[RuntimeConfig, str]:
    route_extra = dict(extra_body or {})
    routes: dict[str, dict[str, Any]] = {}
    for name, (stage, system, user, repair, max_tokens, concurrency) in _ROUTES.items():
        route = {
            "stage": stage,
            "provider": "gateway",
            "model": selection_model if name in _SELECTION_ROUTES else standard_model,
            "system_prompt": system,
            "user_prompt": user,
            "temperature": 0,
            "max_output_tokens": max_tokens,
            "timeout_seconds": 180,
            "max_concurrency": concurrency,
            "extra_body": route_extra,
        }
        if repair is not None:
            route["repair_prompt"] = repair
        routes[name] = route
    payload = {
        "providers": {
            "gateway": {
                "base_url": gateway_url,
                "chat_completions_path": completion_path,
                "max_output_tokens_field": max_output_tokens_field,
                "keys": [{"key_id": "gateway_default", "api_key_env": API_KEY_ENV}],
            }
        },
        "routes": routes,
        "stage_budgets": _STAGE_BUDGETS,
    }
    return RuntimeConfig.model_validate(payload), "gateway"


def _update_single_provider_runtime(
    existing: RuntimeConfig,
    *,
    gateway_url: str,
    standard_model: str,
    selection_model: str,
    completion_path: str,
    max_output_tokens_field: Literal["max_tokens", "max_completion_tokens"],
    extra_body: dict[str, Any] | None,
) -> tuple[RuntimeConfig, str]:
    provider_names = set(existing.providers)
    route_providers = {route.provider for route in existing.routes.values()}
    if len(provider_names) != 1 or route_providers != provider_names:
        raise Conflict(
            "quick setup can update only a single-provider runtime; "
            "keep multi-provider connections in runtime.json"
        )
    provider_name = next(iter(provider_names))
    payload = existing.model_dump(mode="json", exclude_none=True)
    provider = payload["providers"][provider_name]
    provider.update(
        {
            "base_url": gateway_url,
            "chat_completions_path": completion_path,
            "max_output_tokens_field": max_output_tokens_field,
            "keys": [{"key_id": "quick_default", "api_key_env": API_KEY_ENV}],
        }
    )
    for name, route in payload["routes"].items():
        if name not in _ROUTES:
            continue
        route["model"] = selection_model if name in _SELECTION_ROUTES else standard_model
        if extra_body is not None:
            route["extra_body"] = dict(extra_body)
    return RuntimeConfig.model_validate(payload), provider_name


def authorize_setup_write(
    *, client_host: str | None, origin: str | None, request_url: str
) -> None:
    """Reject secret/config writes outside the loopback, same-origin Console."""

    normalized_client = (client_host or "").strip("[]").casefold()
    if normalized_client not in _LOOPBACK_HOSTS:
        raise BadRequest("setup writes are only allowed from the loopback Console")
    if not origin:
        raise BadRequest("setup write origin must match the Console origin")
    expected = urlsplit(request_url)
    supplied = urlsplit(origin)
    if (supplied.scheme, supplied.hostname, supplied.port) != (
        expected.scheme,
        expected.hostname,
        expected.port,
    ):
        raise BadRequest("setup write origin must match the Console origin")


def _dotenv_has_nonempty_key(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in lines:
        if not _ENV_ASSIGNMENT_RE.match(line):
            continue
        value = line.split("=", 1)[1].strip()
        return bool(value and value not in {'""', "''"})
    return False


def _runtime_has_configured_key(path: Path, config: RuntimeConfig) -> bool:
    try:
        from dotenv import dotenv_values

        values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    except (ImportError, OSError, UnicodeError):
        values = {}
    return any(
        bool(os.environ.get(key.api_key_env) or values.get(key.api_key_env))
        for provider in config.providers.values()
        for key in provider.keys
    )


def _write_dotenv_key(path: Path, secret: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else b""
    has_bom = original.startswith(b"\xef\xbb\xbf")
    text = original[3:].decode("utf-8") if has_bom else original.decode("utf-8")
    lines = text.splitlines(keepends=True)
    replacement = f"{API_KEY_ENV}={_dotenv_quote(secret)}"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if _ENV_ASSIGNMENT_RE.match(line):
            if not replaced:
                ending_match = re.search(r"(\r\n|\n|\r)$", line)
                updated.append(replacement + (ending_match.group(1) if ending_match else ""))
                replaced = True
            continue
        updated.append(line)
    if not replaced:
        newline_match = re.search(r"\r\n|\n|\r", text)
        separator = newline_match.group(0) if newline_match else "\n"
        if not text:
            updated.append(replacement + separator)
        elif text.endswith(("\r", "\n")):
            updated.append(replacement + separator)
        else:
            updated.append(separator + replacement)
    encoded = "".join(updated).encode("utf-8")
    if has_bom:
        encoded = b"\xef\xbb\xbf" + encoded
    _atomic_write_bytes(path, encoded)


def _dotenv_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _read_dotenv_key(path: Path) -> str | None:
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover - core dependency provides it
        raise RuntimeError("python-dotenv is required for Console setup") from exc
    value = dotenv_values(path, interpolate=False).get(API_KEY_ENV)
    return value if isinstance(value, str) else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write_bytes(path, previous)


__all__ = [
    "API_KEY_ENV",
    "authorize_setup_write",
    "setup_status",
    "update_runtime",
]
