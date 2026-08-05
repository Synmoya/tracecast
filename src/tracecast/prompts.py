"""Load each LLM route's versioned prompt files.

Route prompts live under stage subdirectories of ``prompt/`` and are referenced
by path from route config. Code fills dynamic data into templates, but prompt
prose stays in files so it is human-editable and diffable. Replaceable Host
configuration is externalized to ``hosts/<host-id>.json`` and projected by the
owning stage.

The file's first line carries a ``<!-- version: <label> -->`` header; the loader
returns that label as the ``prompt_version`` recorded on every
:class:`~tracecast.domain.LlmCallRecord`, so the version stamp and the prompt bytes
are same-source. The header line is stripped from the text sent to the model. When
a route has both system and user templates, callers should combine both version
labels in the call record because both affect model-facing input.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from tracecast.control_prompt import ControlPromptLocale, coerce_control_prompt_locale
from tracecast.prompt_resources import resolve_prompt_path
from tracecast.storage import RuntimeConfig

_VERSION_RE = re.compile(r"^\s*<!--\s*version:\s*(?P<label>\S+)\s*-->\s*$")
_PLACEHOLDER_RE = re.compile(r"{{(?P<key>[A-Za-z0-9_]+)}}")


def substitute_prompt_blocks(
    template: str,
    blocks: Mapping[str, str],
    *,
    prompt_name: str,
) -> str:
    """Strictly substitute one prompt's named, inspectable input blocks.

    Runtime calls and Console reconstruction share this function.  Exact key
    equality prevents a template edit from silently dropping an input block or
    leaving an unresolved placeholder behind.
    """
    expected = set(_PLACEHOLDER_RE.findall(template))
    actual = set(blocks)
    if expected != actual:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append("missing blocks: " + ", ".join(missing))
        if extra:
            details.append("unexpected blocks: " + ", ".join(extra))
        raise ValueError(f"{prompt_name} prompt block mismatch ({'; '.join(details)})")
    # Replace only placeholders that came from the template.  Dynamic block
    # values are opaque payloads: braces inside source text must survive
    # verbatim and must never be interpreted as a second substitution pass.
    return _PLACEHOLDER_RE.sub(
        lambda match: blocks[match.group("key")],
        template,
    ).strip()


def extract_prompt_blocks(template: str, rendered: str) -> dict[str, str]:
    """Recover named block values from a recorded rendered user prompt.

    This is the inverse inspection operation for ``substitute_prompt_blocks``.
    It is intentionally strict and is used only by Console: if current prompt
    prose no longer matches a recorded request, callers report prompt drift and
    show the complete recorded user message as a fallback block.
    """
    matches = list(_PLACEHOLDER_RE.finditer(template))
    if not matches:
        if template.strip() != rendered.strip():
            raise ValueError("recorded prompt differs from placeholder-free template")
        return {}
    pattern_parts: list[str] = [r"^\s*", re.escape(template[: matches[0].start()])]
    seen: set[str] = set()
    ordered_keys: list[str] = []
    for index, match in enumerate(matches):
        key = match.group("key")
        if key in seen:
            raise ValueError(f"prompt placeholder {key!r} occurs more than once")
        seen.add(key)
        ordered_keys.append(key)
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(template)
        suffix = template[match.end() : next_start]
        pattern_parts.append(f"(?P<{key}>.*?)")
        pattern_parts.append(re.escape(suffix))
    pattern_parts.append(r"\s*$")
    parsed = re.match("".join(pattern_parts), rendered, flags=re.DOTALL)
    if parsed is None:
        raise ValueError("recorded prompt no longer matches the current template")
    return {key: parsed.group(key).strip() for key in ordered_keys}


def load_system_prompt(
    route: str,
    config: RuntimeConfig,
    *,
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
    base_dir: str | Path | None = None,
) -> tuple[str, str]:
    """Return ``(system_prompt_text, prompt_version)`` for one LLM route.

    The route's ``system_prompt`` config value is a path. ``prompt/...`` resolves
    against bundled assets; other relative paths require an explicit ``base_dir``.
    Raises rather than silently sending an unversioned or empty system prompt.
    """
    return load_route_prompt(
        route,
        config,
        "system_prompt",
        control_prompt_locale=control_prompt_locale,
        base_dir=base_dir,
    )


def load_user_prompt(
    route: str,
    config: RuntimeConfig,
    *,
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
    base_dir: str | Path | None = None,
) -> tuple[str, str]:
    """Return ``(user_prompt_template, prompt_version)`` for one LLM route."""
    return load_route_prompt(
        route,
        config,
        "user_prompt",
        control_prompt_locale=control_prompt_locale,
        base_dir=base_dir,
    )


def load_route_prompt(
    route: str,
    config: RuntimeConfig,
    field: str,
    *,
    control_prompt_locale: ControlPromptLocale | str = ControlPromptLocale.zh,
    base_dir: str | Path | None = None,
) -> tuple[str, str]:
    """Load one versioned route prompt path from ``field`` on the route config.

    A configured path may contain one ``{locale}`` marker.  It is expanded to
    the actual ``zh``/``en`` asset language before resolving bundled or
    external assets.  Existing scalar paths without the marker remain valid and
    continue to load exactly the same bytes.
    """
    route_cfg = config.route(route)
    rel = getattr(route_cfg, field, None)
    if not rel:
        raise KeyError(f"route {route!r} has no {field!r} path configured")
    locale = coerce_control_prompt_locale(control_prompt_locale)
    rel = rel.replace("{locale}", locale.value)
    path = resolve_prompt_path(rel, base_dir=base_dir)
    text = path.read_text(encoding="utf-8")
    return _split_versioned(text, route=route, path=path)


def _split_versioned(text: str, *, route: str, path: Path) -> tuple[str, str]:
    """Split a prompt file into ``(body, version_label)``; the header is required."""
    lines = text.splitlines()
    if not lines:
        raise ValueError(f"prompt file {path} for route {route!r} is empty")
    match = _VERSION_RE.match(lines[0])
    if match is None:
        raise ValueError(
            f"prompt file {path} for route {route!r} is missing a "
            "'<!-- version: <label> -->' header on its first line"
        )
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError(f"prompt file {path} for route {route!r} has no prompt body")
    return body, match.group("label")
