"""Artifact storage: deterministic JSON/JSONL IO, hashing, run store, config.

The persistence layer every stage uses to write and reload its artifacts. It
owns *how bytes hit disk* (UTF-8 human-readable Chinese, atomic writes,
deterministic key order, stable hashes) and the ``runs/<run_id>/`` layout; it
owns no stage or LLM logic.
"""

from __future__ import annotations

from ._fsutil import atomic_write_text
from .config import (
    DEFAULT_RUNTIME_CONFIG_PATH,
    ProviderConfig,
    ProviderKey,
    ProviderLimits,
    RouteConfig,
    RuntimeConfig,
    load_dotenv_file,
)
from .hashing import (
    canonical_json,
    content_hash,
    sha256_bytes,
    sha256_json,
    sha256_text,
)
from .json_io import (
    read_json,
    read_json_model,
    read_json_models,
    write_json,
    write_json_model,
    write_json_models,
)
from .jsonl_io import (
    append_jsonl,
    read_jsonl,
    read_jsonl_models,
    write_jsonl,
)
from .run_store import (
    ARTIFACT_SPECS,
    ASSEMBLY_REPORT,
    AUDIT_REPORT,
    CANDIDATE_GRAPH,
    CHUNK_ARTIFACTS,
    CHUNK_PLAN,
    EDITED_MATERIALS,
    LLM_CALL_RECORDS,
    MATERIAL_ATOMS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    NODE_EDIT_PLAN,
    RENDER_CONTRACTS,
    RENDER_FACT_CONTEXTS,
    SCRIPT,
    SELECTION_PLAN,
    SOURCE_PIECES,
    SOURCE_SNAPSHOTS,
    SOURCE_WINDOWS,
    ArtifactFormat,
    ArtifactSpec,
    RawRole,
    RunStore,
    TextArtifactSpec,
)

__all__ = [
    # filesystem
    "atomic_write_text",
    # hashing
    "sha256_bytes",
    "sha256_text",
    "sha256_json",
    "canonical_json",
    "content_hash",
    # json io
    "read_json",
    "write_json",
    "read_json_model",
    "write_json_model",
    "read_json_models",
    "write_json_models",
    # jsonl io
    "read_jsonl",
    "write_jsonl",
    "append_jsonl",
    "read_jsonl_models",
    # run store
    "RunStore",
    "ArtifactSpec",
    "TextArtifactSpec",
    "ArtifactFormat",
    "RawRole",
    "ARTIFACT_SPECS",
    # run store: artifact slots
    "SOURCE_SNAPSHOTS",
    "SOURCE_PIECES",
    "SOURCE_WINDOWS",
    "MATERIAL_ATOMS",
    "CANDIDATE_GRAPH",
    "SELECTION_PLAN",
    "NODE_EDIT_PLAN",
    "EDITED_MATERIALS",
    "NAME_RESOLUTION_PLAN",
    "NAME_RESOLUTION_REPORT",
    "CHUNK_PLAN",
    "RENDER_CONTRACTS",
    "RENDER_FACT_CONTEXTS",
    "CHUNK_ARTIFACTS",
    "AUDIT_REPORT",
    "ASSEMBLY_REPORT",
    "SCRIPT",
    "LLM_CALL_RECORDS",
    # config
    "RuntimeConfig",
    "ProviderConfig",
    "ProviderKey",
    "ProviderLimits",
    "RouteConfig",
    "load_dotenv_file",
    "DEFAULT_RUNTIME_CONFIG_PATH",
]
