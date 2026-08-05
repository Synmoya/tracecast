"""UTF-8 JSON file IO for Tracecast artifacts.

Every write is atomic and pretty-printed with ``ensure_ascii=False`` and sorted
keys, so Chinese round-trips as human-readable text (not ``\\uXXXX``) and the
byte layout of an artifact is deterministic for equal content. Model helpers
dump/load a single Pydantic v2 model or a JSON array of models.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ._fsutil import atomic_write_text


def write_json(path: str | os.PathLike[str], obj: Any) -> None:
    """Atomically write ``obj`` as pretty UTF-8 JSON (sorted keys, trailing ``\\n``)."""
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(path, text + "\n")


def read_json(path: str | os.PathLike[str]) -> Any:
    """Load JSON from ``path`` (UTF-8)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_model(path: str | os.PathLike[str], model: BaseModel) -> None:
    """Dump a single Pydantic model to a JSON file."""
    write_json(path, model.model_dump(mode="json"))


def read_json_model[ModelT: BaseModel](
    path: str | os.PathLike[str], model_type: type[ModelT]
) -> ModelT:
    """Load and validate a single Pydantic model from a JSON file."""
    return model_type.model_validate(read_json(path))


def write_json_models(path: str | os.PathLike[str], models: Iterable[BaseModel]) -> None:
    """Dump a list of Pydantic models to a JSON array file."""
    write_json(path, [model.model_dump(mode="json") for model in models])


def read_json_models[ModelT: BaseModel](
    path: str | os.PathLike[str], model_type: type[ModelT]
) -> list[ModelT]:
    """Load and validate a JSON array of Pydantic models."""
    data = read_json(path)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array in {os.fspath(path)!r}, got {type(data).__name__}")
    return [model_type.model_validate(item) for item in data]
