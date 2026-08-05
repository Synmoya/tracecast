"""Deterministic content hashing and JSON canonicalization.

Two needs are served here:

* **Integrity / provenance hashes** -- ``content_hash`` produces the stable
  ``sha256`` hex used for ``SourceSnapshot.content_hash``, ``Provenance``
  lineage, and ``ChunkArtifact`` input/output integrity.
* **Canonical request hashes** -- ``sha256_json`` over :func:`canonical_json`
  gives the stable digest the LLM observer needs for
  ``LlmCallRecord.input_hash`` (canonical provider request envelope).

Canonicalization is deterministic across runs and processes: keys are sorted at
every level and separators are fixed, so two equal payloads always hash to the
same value regardless of dict insertion order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


def sha256_bytes(data: bytes) -> str:
    """Lowercase ``sha256`` hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """``sha256`` hex digest of ``text`` encoded as UTF-8."""
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(payload: Any) -> str:
    """Serialize ``payload`` to a deterministic JSON string.

    Sorted keys, tight separators, and non-ASCII left intact (the bytes are
    UTF-8 either way). Pydantic models are dumped in JSON mode first so nested
    datetimes/enums become their stable serialized form.
    """
    return json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(payload: Any) -> str:
    """``sha256`` hex digest of the :func:`canonical_json` of ``payload``."""
    return sha256_text(canonical_json(payload))


def content_hash(payload: Any) -> str:
    """Stable ``sha256`` hex for source snapshots and artifact integrity.

    ``bytes``/``str`` hash their raw content; pydantic models, mappings, and
    sequences hash their canonical JSON. Returns a bare lowercase hex digest
    (no algorithm prefix).
    """
    if isinstance(payload, bytes):
        return sha256_bytes(payload)
    if isinstance(payload, str):
        return sha256_text(payload)
    return sha256_json(payload)


def _jsonable(payload: Any) -> Any:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, Mapping):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(item) for item in payload]
    return payload
