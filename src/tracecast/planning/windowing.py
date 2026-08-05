"""Source-isolated Planning window validation, repacking, and target allocation."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil

from tracecast.control_prompt import ControlPromptLocale
from tracecast.domain import (
    MATERIAL_ALIASES,
    AliasManifest,
    CandidateNode,
    MaterialAtom,
    SourceWindow,
    estimate_tokens,
    window_id,
)

from .projection import Projection, render_planner_user_prompt
from .vocab import STAGE_PLANNING, diagnostic


@dataclass(frozen=True)
class PlanningWindowPack:
    """One source-isolated Planning provider input."""

    window: SourceWindow
    atoms: list[MaterialAtom]


def group_atoms_by_source(
    windows: Sequence[SourceWindow], atoms: Sequence[MaterialAtom]
) -> tuple[dict[str, list[MaterialAtom]], list[str], list[dict]]:
    """Validate atom ownership against ingestion windows and group by source."""
    piece_to_window: dict[str, str] = {}
    window_source: dict[str, str] = {}
    source_order: list[str] = []
    seen_sources: set[str] = set()
    for window in windows:
        window_source[window.window_id] = window.source_id
        if window.source_id not in seen_sources:
            seen_sources.add(window.source_id)
            source_order.append(window.source_id)
        for piece in window.piece_ids:
            piece_to_window[piece] = window.window_id
    grouped: dict[str, list[MaterialAtom]] = {source_id: [] for source_id in source_order}
    diagnostics: list[dict] = []
    for atom in atoms:
        owner_ids = {
            piece_to_window[piece]
            for piece in atom.provenance.piece_refs
            if piece in piece_to_window
        }
        if len(owner_ids) == 1:
            source_id = window_source[owner_ids.pop()]
            if source_id != atom.provenance.source_ref:
                diagnostics.append(
                    diagnostic(
                        STAGE_PLANNING,
                        "atom_source_mismatch",
                        "atom source_ref does not match its owning ingestion window source; "
                        "excluded from planning",
                        atom_id=atom.id,
                        source_ref=atom.provenance.source_ref,
                        window_source=source_id,
                    )
                )
                continue
            grouped.setdefault(source_id, []).append(atom)
            if source_id not in seen_sources:
                seen_sources.add(source_id)
                source_order.append(source_id)
            continue
        diagnostics.append(
            diagnostic(
                STAGE_PLANNING,
                "orphan_atom",
                "atom does not map to exactly one window; excluded from planning",
                atom_id=atom.id,
            )
        )
    return grouped, source_order, diagnostics


def pack_planning_windows(
    grouped: Mapping[str, list[MaterialAtom]],
    source_order: Sequence[str],
    *,
    budget: int,
    planner_user_template: str,
    control_prompt_locale: ControlPromptLocale = ControlPromptLocale.zh,
) -> list[PlanningWindowPack]:
    """Greedily repack source-local atoms into planner call windows."""
    packs: list[PlanningWindowPack] = []
    order_index = 0
    overhead_cache: dict[int, int] = {}

    def overhead(alias_count: int) -> int:
        if alias_count not in overhead_cache:
            manifest = AliasManifest.number(
                MATERIAL_ALIASES,
                (f"planning-window-placeholder-{index}" for index in range(alias_count)),
            )
            prompt = render_planner_user_prompt(
                template=planner_user_template,
                projection=Projection("", manifest),
                control_prompt_locale=control_prompt_locale,
            )
            overhead_cache[alias_count] = estimate_tokens(prompt)
        return overhead_cache[alias_count]

    def atom_tokens(atom: MaterialAtom, alias_index: int) -> int:
        alias = MATERIAL_ALIASES.format(alias_index)
        header = (
            f"M {alias} | src={atom.provenance.source_ref} | so={atom.source_order} "
            f"| k={atom.content_kind.value} | tg={','.join(atom.tags)}"
        )
        return estimate_tokens(f"{header}\nTXT {atom.content}")

    def flush(source_id: str, current: list[MaterialAtom], block_tokens: int) -> None:
        nonlocal order_index
        piece_refs = list(
            dict.fromkeys(ref for atom in current for ref in atom.provenance.piece_refs)
        )
        if not piece_refs:
            return
        window = SourceWindow(
            window_id=window_id(source_id, piece_refs),
            source_id=source_id,
            piece_ids=piece_refs,
            order_index=order_index,
            estimated_input_tokens=overhead(len(current)) + block_tokens,
            metadata={"planning_repack": True, "material_count": len(current)},
        )
        packs.append(PlanningWindowPack(window=window, atoms=list(current)))
        order_index += 1

    for source_id in source_order:
        current: list[MaterialAtom] = []
        current_tokens = 0
        for atom in sorted(grouped.get(source_id, []), key=lambda item: item.source_order):
            next_tokens = atom_tokens(atom, len(current) + 1)
            if current and overhead(len(current) + 1) + current_tokens + next_tokens > budget:
                flush(source_id, current, current_tokens)
                current = []
                current_tokens = 0
                next_tokens = atom_tokens(atom, 1)
            current.append(atom)
            current_tokens += next_tokens
        if current:
            flush(source_id, current, current_tokens)
    return packs


def retained_window_token_loads(
    nodes: Sequence[CandidateNode],
    material_tokens_of: Mapping[str, int],
) -> dict[str, int]:
    """Return seed-retained material tokens for each planning window."""
    material_ids_by_window: dict[str, set[str]] = {}
    for node in nodes:
        material_ids_by_window.setdefault(node.source_window, set()).update(
            node.material_ids
        )
    return {
        source_window: sum(
            material_tokens_of[material_id] for material_id in material_ids
        )
        for source_window, material_ids in material_ids_by_window.items()
    }


def window_ratio_targets(
    retained_tokens: Mapping[str, int],
    target_material_tokens_per_node: int,
) -> dict[str, int]:
    """Derive each window's target independently from its retained tokens."""
    if target_material_tokens_per_node <= 0:
        raise ValueError("target_material_tokens_per_node must be positive")
    return {
        source_window: max(1, ceil(max(0, tokens) / target_material_tokens_per_node))
        for source_window, tokens in retained_tokens.items()
    }


def allocate_window_targets(
    retained_tokens: Mapping[str, int],
    target_node_count: int,
    *,
    upper_bounds: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Allocate a global target by retained token load.

    Every non-empty seed window keeps at least one slot. When ``upper_bounds``
    is supplied (the default ratio path under a binding global cap), no window
    receives more slots than its independently derived ratio target.
    """
    positive = [
        (source_window, max(0, int(tokens)))
        for source_window, tokens in retained_tokens.items()
    ]
    if not positive:
        return {}
    allocations = {source_window: 1 for source_window, _tokens in positive}
    remaining = max(0, target_node_count - len(positive))
    capacities = {
        source_window: (
            max(0, int(upper_bounds.get(source_window, 1)) - 1)
            if upper_bounds is not None
            else remaining
        )
        for source_window, _tokens in positive
    }

    while remaining:
        eligible = [
            (index, source_window, tokens)
            for index, (source_window, tokens) in enumerate(positive)
            if capacities[source_window] > 0
        ]
        if not eligible:
            break
        round_remaining = remaining
        total_tokens = sum(tokens for _index, _window, tokens in eligible)
        divisor = total_tokens or len(eligible)
        shares: list[tuple[float, int, str, int]] = []
        extras: list[tuple[str, int]] = []
        for index, source_window, tokens in eligible:
            weight = tokens if total_tokens else 1
            exact = round_remaining * weight / divisor
            extra = min(capacities[source_window], int(exact))
            extras.append((source_window, extra))
            shares.append((exact - int(exact), -index, source_window, weight))
        assigned = sum(extra for _source_window, extra in extras)
        for source_window, extra in extras:
            allocations[source_window] += extra
            capacities[source_window] -= extra
        remaining -= assigned
        if not remaining:
            break
        ranked = sorted(
            (
                share
                for share in shares
                if capacities[share[2]] > 0
            ),
            reverse=True,
        )
        for _remainder, _index, source_window, _weight in ranked[:remaining]:
            allocations[source_window] += 1
            capacities[source_window] -= 1
            remaining -= 1
    return allocations


def window_targets_met(
    nodes: Sequence[CandidateNode],
    window_targets: Mapping[str, int],
) -> bool:
    """Return whether every current window frontier is within its own target."""
    counts = Counter(node.source_window for node in nodes)
    return all(
        count <= window_targets.get(source_window, 0)
        for source_window, count in counts.items()
    )
