"""Chunk packing policy: pure-material budgets Packing operates under.

These budgets are *parameters injected by the pipeline driver*, never read from
config or disk inside Packing -- that keeps Packing a pure, deterministic
transform with no dependency on the storage/config layer. A real run overrides
the defaults from ``runtime.json`` at the driver layer.

Chunk boundaries are driven by estimated tokens over ``MaterialAtom.content``
only. Prompt scaffolding, directives, persona and continuity context do not
distort this primary material-volume control.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any


@dataclass(frozen=True)
class ChunkPolicy:
    """Deterministic chunk-packing budgets plus the unit-formula weights.

    ``target_material_tokens`` is the soft close point and
    ``max_material_tokens`` is the hard cap. Both measure only render-visible
    keep/condense material content. A single node over the hard cap is reported
    and isolated, never silently split.
    """

    target_material_tokens: int = 3000
    max_material_tokens: int = 4500
    max_materials_per_chunk: int = 80
    max_nodes_per_chunk: int | None = None
    prefer_node_boundary: bool = True

    def __post_init__(self) -> None:
        for name in (
            "target_material_tokens",
            "max_material_tokens",
            "max_materials_per_chunk",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"ChunkPolicy.{name} must be positive")
        if self.max_material_tokens < self.target_material_tokens:
            raise ValueError(
                "ChunkPolicy.max_material_tokens must be >= target_material_tokens"
            )
        if self.max_nodes_per_chunk is not None and self.max_nodes_per_chunk <= 0:
            raise ValueError("ChunkPolicy.max_nodes_per_chunk must be positive or None")

    def to_policy_dict(self) -> dict[str, Any]:
        """The policy as a plain JSON-able dict, for ``ChunkPlan.policy``."""
        return asdict(self)


DEFAULT_CHUNK_POLICY = ChunkPolicy()


def chunk_policy_from_budgets(
    budgets: Mapping[str, Any],
    *,
    base: ChunkPolicy = DEFAULT_CHUNK_POLICY,
) -> ChunkPolicy:
    """Apply configured rendering-stage overrides to a packing policy."""

    supported = (
        "target_material_tokens",
        "max_material_tokens",
        "max_materials_per_chunk",
        "max_nodes_per_chunk",
        "prefer_node_boundary",
    )
    return replace(base, **{key: budgets[key] for key in supported if key in budgets})
