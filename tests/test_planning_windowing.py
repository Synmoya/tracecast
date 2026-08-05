from tracecast.domain import CandidateNode
from tracecast.planning.policy import target_node_count
from tracecast.planning.windowing import (
    allocate_window_targets,
    retained_window_token_loads,
    window_ratio_targets,
    window_targets_met,
)


def _node(candidate_id: str, source_window: str, material_ids: list[str]) -> CandidateNode:
    return CandidateNode(
        candidate_id=candidate_id,
        source_window=source_window,
        summary=candidate_id,
        material_ids=material_ids,
    )


def test_default_targets_apply_ratio_independently_before_global_cap() -> None:
    retained_tokens = {
        "small": 1_090,
        "large_a": 26_758,
        "large_b": 25_802,
    }

    local_targets = window_ratio_targets(retained_tokens, 650)
    global_target = target_node_count({}, sum(local_targets.values()))

    assert local_targets == {"small": 2, "large_a": 42, "large_b": 40}
    assert global_target == 84


def test_binding_global_cap_never_increases_a_local_ratio_target() -> None:
    retained_tokens = {"small": 100, "large_a": 6_500, "large_b": 6_500}
    local_targets = window_ratio_targets(retained_tokens, 650)

    allocated = allocate_window_targets(
        retained_tokens,
        15,
        upper_bounds=local_targets,
    )

    assert allocated == {"small": 1, "large_a": 7, "large_b": 7}
    assert sum(allocated.values()) == 15
    assert all(allocated[window] <= local_targets[window] for window in allocated)


def test_explicit_global_override_is_distributed_by_retained_tokens() -> None:
    allocated = allocate_window_targets(
        {"small": 100, "large": 900},
        5,
    )

    assert allocated == {"small": 1, "large": 4}


def test_retained_load_excludes_seed_dropped_materials_and_deduplicates_refs() -> None:
    nodes = [
        _node("a", "w1", ["m1", "m2"]),
        _node("b", "w1", ["m2"]),
        _node("c", "w2", ["m3"]),
    ]

    loads = retained_window_token_loads(
        nodes,
        {"m1": 100, "m2": 200, "m3": 300, "dropped": 1_000},
    )

    assert loads == {"w1": 300, "w2": 300}


def test_window_target_is_not_met_when_global_count_alone_is_within_budget() -> None:
    nodes = [
        _node("a", "w1", ["m1"]),
        _node("b", "w1", ["m2"]),
        _node("c", "w2", ["m3"]),
    ]

    assert len(nodes) <= sum({"w1": 1, "w2": 3}.values())
    assert not window_targets_met(nodes, {"w1": 1, "w2": 3})
