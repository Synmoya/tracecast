"""Deterministic access to the repository's bundled prompt assets."""

from __future__ import annotations

from pathlib import Path


def prompt_root() -> Path:
    """Return the prompt asset root in an installed wheel or source checkout."""

    package_root = Path(__file__).resolve().parent / "prompt"
    if package_root.is_dir():
        return package_root
    checkout_root = Path(__file__).resolve().parents[2] / "prompt"
    if checkout_root.is_dir():
        return checkout_root
    raise FileNotFoundError("tracecast prompt assets are not installed")


def resolve_prompt_path(
    configured_path: str | Path,
    *,
    base_dir: str | Path | None = None,
) -> Path:
    """Resolve an absolute, caller-relative, or bundled ``prompt/...`` path."""

    path = Path(configured_path)
    if path.is_absolute():
        return path
    if base_dir is not None:
        return Path(base_dir) / path
    if path.parts and path.parts[0] == "prompt":
        return prompt_root().joinpath(*path.parts[1:])
    raise ValueError(
        f"relative prompt path {path!s} must start with 'prompt/' "
        "or be resolved with base_dir"
    )


def prompt_component_path(stage: str, name: str) -> Path:
    """Return a stage-owned catalog under ``prompt/<stage>/components``."""

    return prompt_root() / stage / "components" / name
