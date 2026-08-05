"""Small concurrency helpers for route-scoped LLM work.

Stages own their semantic ordering. This module only centralizes the mechanical
pattern already used by ingestion: run independent calls with a route-owned
worker count, then return results in the original order for deterministic
artifact rebuilds.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import cast

from tracecast.storage import RuntimeConfig

_MISSING = object()


def route_max_concurrency(config: RuntimeConfig, route_name: str) -> int:
    """Return the positive max concurrency configured for a route, or 1."""
    value = config.route(route_name).max_concurrency
    if value is None or value < 1:
        return 1
    return value


def run_ordered_concurrently[T, R](
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    max_workers: int,
) -> list[R]:
    """Run ``worker`` over ``items`` concurrently, preserving input order."""
    if not items:
        return []
    if max_workers <= 1 or len(items) == 1:
        return [worker(item) for item in items]

    results: list[object] = [_MISSING] * len(items)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as executor:
        future_by_index = {
            executor.submit(worker, item): index for index, item in enumerate(items)
        }
        for future in as_completed(future_by_index):
            results[future_by_index[future]] = future.result()

    return [cast(R, result) for result in results]
