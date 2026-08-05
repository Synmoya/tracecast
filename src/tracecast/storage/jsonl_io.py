"""UTF-8 JSON Lines IO: one JSON object per line.

Used for artifact collections and append-only ledgers (e.g. the ``LlmCallRecord``
call ledger). Lines are compact, sorted-key, ``ensure_ascii=False`` JSON so each
row is deterministic and Chinese stays human-readable. A full rewrite
(:func:`write_jsonl`) is atomic; :func:`append_jsonl` appends in place, which is
the natural mode for a per-attempt ledger.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ._fsutil import atomic_write_text


def _row_to_line(row: BaseModel | dict[str, Any]) -> str:
    obj = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def write_jsonl(
    path: str | os.PathLike[str], rows: Iterable[BaseModel | dict[str, Any]]
) -> None:
    """Atomically (re)write a JSONL file from ``rows`` (models or dicts)."""
    text = "".join(_row_to_line(row) + "\n" for row in rows)
    atomic_write_text(path, text)


def append_jsonl(
    path: str | os.PathLike[str], rows: Iterable[BaseModel | dict[str, Any]]
) -> None:
    """Append ``rows`` to a JSONL file, creating it (and parents) if needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_row_to_line(row) + "\n")


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load all rows from a JSONL file as dicts (blank lines skipped)."""
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def read_jsonl_models[ModelT: BaseModel](
    path: str | os.PathLike[str], model_type: type[ModelT]
) -> list[ModelT]:
    """Load and validate every row of a JSONL file into ``model_type``."""
    return [model_type.model_validate(row) for row in read_jsonl(path)]
