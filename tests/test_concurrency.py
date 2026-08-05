"""Shared route-concurrency helper tests."""

from __future__ import annotations

import time

from tracecast.concurrency import route_max_concurrency, run_ordered_concurrently
from tracecast.storage import RuntimeConfig


def test_route_max_concurrency_defaults_to_one() -> None:
    config = RuntimeConfig.model_validate(
        {
            "providers": {"fake": {"base_url": "https://fake.invalid", "keys": []}},
            "routes": {
                "a": {"stage": "x", "provider": "fake", "model": "fake"},
                "b": {
                    "stage": "x",
                    "provider": "fake",
                    "model": "fake",
                    "max_concurrency": 0,
                },
                "c": {
                    "stage": "x",
                    "provider": "fake",
                    "model": "fake",
                    "max_concurrency": 7,
                },
            },
        }
    )

    assert route_max_concurrency(config, "a") == 1
    assert route_max_concurrency(config, "b") == 1
    assert route_max_concurrency(config, "c") == 7


def test_run_ordered_concurrently_preserves_input_order() -> None:
    def worker(value: int) -> str:
        time.sleep((4 - value) * 0.01)
        return f"done-{value}"

    assert run_ordered_concurrently([1, 2, 3], worker, max_workers=3) == [
        "done-1",
        "done-2",
        "done-3",
    ]
