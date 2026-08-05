"""Read-only Host catalog and lineage-stable run Host resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tracecast.hosts import HostProfile, list_hosts, load_host, require_host_language
from tracecast.storage import content_hash

from .context import ConsoleContext
from .errors import BadRequest, NotFound
from .inventory import open_run, read_console_manifest

BUILTIN_HOST_IDS = ("starter-zh-cn", "starter-en-us")
_BUILTIN_HOSTS_DIR = Path(__file__).parent / "starters"


def host_summaries(ctx: ConsoleContext) -> list[dict[str, Any]]:
    try:
        hosts = _catalog_hosts(ctx)
    except (OSError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot load host catalog: {exc}") from exc
    return [
        {
            "host_id": host.host_id,
            "schema_version": host.schema_version,
            "display_name": host.display_name,
            "version": host.version,
            "output_language": host.output_language,
            "content_hash": host.snapshot()["content_hash"],
            "catalog_origin": origin,
            "read_only": origin == "builtin",
        }
        for host, origin in hosts
    ]


def host_detail(ctx: ConsoleContext, host_id: str) -> dict[str, Any]:
    try:
        host, origin = _catalog_host(ctx, host_id)
    except FileNotFoundError as exc:
        raise NotFound(f"unknown host {host_id!r}") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot load host {host_id!r}: {exc}") from exc
    result = host.snapshot()
    result["catalog_origin"] = origin
    result["read_only"] = origin == "builtin"
    return result


def load_console_host(
    ctx: ConsoleContext,
    host_id: str,
    *,
    output_language: str | None = None,
) -> HostProfile:
    try:
        host, _origin = _catalog_host(ctx, host_id)
        if output_language is not None:
            require_host_language(host, output_language)
        return host
    except FileNotFoundError as exc:
        raise NotFound(f"unknown host {host_id!r}") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot load host {host_id!r}: {exc}") from exc


def resolve_run_host(
    ctx: ConsoleContext,
    run_id: str,
    expected_host_id: str,
    *,
    output_language: str,
) -> HostProfile:
    """Prefer a run's immutable Host snapshot; fall back for legacy manifests."""

    seen: set[str] = set()
    current: str | None = run_id
    while current is not None:
        if current in seen:
            raise BadRequest(f"cycle in console run lineage at {current!r}")
        seen.add(current)
        manifest = read_console_manifest(open_run(ctx.runs_root, current))
        effective = (manifest or {}).get("effective")
        snapshot = effective.get("host") if isinstance(effective, dict) else None
        if isinstance(snapshot, dict) and isinstance(snapshot.get("profile"), dict):
            try:
                host = HostProfile.model_validate(snapshot["profile"])
            except (TypeError, ValueError) as exc:
                raise BadRequest(f"run {current!r} has an invalid host snapshot: {exc}") from exc
            actual_hash = content_hash(host.model_dump(mode="json"))
            if snapshot.get("content_hash") != actual_hash:
                raise BadRequest(f"run {current!r} host snapshot hash mismatch")
            if host.host_id != expected_host_id:
                raise BadRequest(
                    f"run {current!r} host {host.host_id!r} does not match "
                    f"artifact host {expected_host_id!r}"
                )
            try:
                require_host_language(host, output_language)
            except ValueError as exc:
                raise BadRequest(str(exc)) from exc
            return host
        parent = (manifest or {}).get("base_run_id")
        current = parent if isinstance(parent, str) and parent else None
    return load_console_host(ctx, expected_host_id, output_language=output_language)


def _catalog_hosts(ctx: ConsoleContext) -> list[tuple[HostProfile, str]]:
    builtins = [(load_host(_BUILTIN_HOSTS_DIR, host_id), "builtin") for host_id in BUILTIN_HOST_IDS]
    reserved = set(BUILTIN_HOST_IDS)
    personal = [
        (host, "personal")
        for host in list_hosts(ctx.hosts_dir)
        if host.host_id not in reserved
    ]
    return builtins + personal


def _catalog_host(ctx: ConsoleContext, host_id: str) -> tuple[HostProfile, str]:
    if host_id in BUILTIN_HOST_IDS:
        return load_host(_BUILTIN_HOSTS_DIR, host_id), "builtin"
    return load_host(ctx.hosts_dir, host_id), "personal"


__all__ = [
    "BUILTIN_HOST_IDS",
    "host_detail",
    "host_summaries",
    "load_console_host",
    "resolve_run_host",
]
