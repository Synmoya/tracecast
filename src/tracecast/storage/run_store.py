"""Run artifact store: the ``runs/<run_id>/`` on-disk layout for a pipeline run.

Each stage persists its owned artifacts under a stage sub-directory; the
cross-cutting LLM observer keeps its
``LlmCallRecord`` ledger and raw provider payloads under ``llm/``. Persisting
artifacts this way makes every completed or failed run inspectable. The canonical
pipeline does not resume into an existing run id.

Layout::

    runs/<run_id>/
      ingestion/   source_snapshots.jsonl  source_pieces.jsonl
                   source_windows.jsonl     material_atoms.jsonl
      planning/    candidate_graph.json
      selection/   selection_plan.json
      node_edit/   node_edit_plan.json      edited_materials.jsonl
      packing/     chunk_plan.json          render_contracts.jsonl
      name_resolution/name_resolution_plan.json
                                            name_resolution_report.jsonl
      rendering/   fact_contexts.jsonl       chunk_artifacts.jsonl
      validation/  audit_report.jsonl
      assembly/    assembly_report.json     script.md
      llm/         call_records.jsonl       raw/<call_id>.<role>.{json,txt}

This module owns *addressing and IO only* -- it contains no LLM logic. It merely
provides the generic slots the ``llm`` stage uses to persist its records and raw
request/response payloads.

Multi-source ingestion: a project may have many sources, so ingestion artifacts
are aggregated into per-collection JSONL files where each row self-identifies its
source (``source_id`` on every record) rather than nested per-source directories.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel

from tracecast.domain import (
    AssemblyReport,
    AuditReportItem,
    CandidateGraph,
    ChunkArtifact,
    ChunkPlan,
    LlmCallRecord,
    MaterialAtom,
    NameResolutionPlan,
    NodeEditPlan,
    RenderContract,
    RenderFactContext,
    SelectionPlan,
    SourcePiece,
    SourceSnapshot,
    SourceWindow,
)

from . import json_io, jsonl_io
from ._fsutil import atomic_write_text

RawRole = Literal["request", "response"]


class ArtifactFormat(StrEnum):
    """On-disk shape of an artifact slot."""

    json_model = "json_model"  # one Pydantic model in a JSON file
    jsonl = "jsonl"  # many Pydantic models, one per line


@dataclass(frozen=True)
class ArtifactSpec[ModelT: BaseModel]:
    """A typed, model-backed artifact slot addressed by ``stage/filename``."""

    stage: str
    filename: str
    format: ArtifactFormat
    model_type: type[ModelT]

    @property
    def relpath(self) -> str:
        return f"{self.stage}/{self.filename}"


@dataclass(frozen=True)
class TextArtifactSpec:
    """A plain-text artifact slot (e.g. the assembled ``script.md``)."""

    stage: str
    filename: str

    @property
    def relpath(self) -> str:
        return f"{self.stage}/{self.filename}"


# --- Ingestion -------------------------------------------------------------
SOURCE_SNAPSHOTS: ArtifactSpec[SourceSnapshot] = ArtifactSpec(
    "ingestion", "source_snapshots.jsonl", ArtifactFormat.jsonl, SourceSnapshot
)
SOURCE_PIECES: ArtifactSpec[SourcePiece] = ArtifactSpec(
    "ingestion", "source_pieces.jsonl", ArtifactFormat.jsonl, SourcePiece
)
SOURCE_WINDOWS: ArtifactSpec[SourceWindow] = ArtifactSpec(
    "ingestion", "source_windows.jsonl", ArtifactFormat.jsonl, SourceWindow
)
MATERIAL_ATOMS: ArtifactSpec[MaterialAtom] = ArtifactSpec(
    "ingestion", "material_atoms.jsonl", ArtifactFormat.jsonl, MaterialAtom
)

# --- Planning / Selection --------------------------------------------------
CANDIDATE_GRAPH: ArtifactSpec[CandidateGraph] = ArtifactSpec(
    "planning", "candidate_graph.json", ArtifactFormat.json_model, CandidateGraph
)
SELECTION_PLAN: ArtifactSpec[SelectionPlan] = ArtifactSpec(
    "selection", "selection_plan.json", ArtifactFormat.json_model, SelectionPlan
)

# --- Node Edit -------------------------------------------------------------
NODE_EDIT_PLAN: ArtifactSpec[NodeEditPlan] = ArtifactSpec(
    "node_edit", "node_edit_plan.json", ArtifactFormat.json_model, NodeEditPlan
)
# keep-referenced originals + run-scoped condensed atoms; both are MaterialAtom.
EDITED_MATERIALS: ArtifactSpec[MaterialAtom] = ArtifactSpec(
    "node_edit", "edited_materials.jsonl", ArtifactFormat.jsonl, MaterialAtom
)
# --- Packing / Rendering ---------------------------------------------------
CHUNK_PLAN: ArtifactSpec[ChunkPlan] = ArtifactSpec(
    "packing", "chunk_plan.json", ArtifactFormat.json_model, ChunkPlan
)
RENDER_CONTRACTS: ArtifactSpec[RenderContract] = ArtifactSpec(
    "packing", "render_contracts.jsonl", ArtifactFormat.jsonl, RenderContract
)
NAME_RESOLUTION_PLAN: ArtifactSpec[NameResolutionPlan] = ArtifactSpec(
    "name_resolution",
    "name_resolution_plan.json",
    ArtifactFormat.json_model,
    NameResolutionPlan,
)
NAME_RESOLUTION_REPORT: ArtifactSpec[AuditReportItem] = ArtifactSpec(
    "name_resolution",
    "name_resolution_report.jsonl",
    ArtifactFormat.jsonl,
    AuditReportItem,
)
RENDER_FACT_CONTEXTS: ArtifactSpec[RenderFactContext] = ArtifactSpec(
    "rendering", "fact_contexts.jsonl", ArtifactFormat.jsonl, RenderFactContext
)
CHUNK_ARTIFACTS: ArtifactSpec[ChunkArtifact] = ArtifactSpec(
    "rendering", "chunk_artifacts.jsonl", ArtifactFormat.jsonl, ChunkArtifact
)

# --- Validation / Assembly -------------------------------------------------
AUDIT_REPORT: ArtifactSpec[AuditReportItem] = ArtifactSpec(
    "validation", "audit_report.jsonl", ArtifactFormat.jsonl, AuditReportItem
)
ASSEMBLY_REPORT: ArtifactSpec[AssemblyReport] = ArtifactSpec(
    "assembly", "assembly_report.json", ArtifactFormat.json_model, AssemblyReport
)
SCRIPT: TextArtifactSpec = TextArtifactSpec("assembly", "script.md")

# --- LLM observer (cross-cutting) ------------------------------------------
LLM_CALL_RECORDS: ArtifactSpec[LlmCallRecord] = ArtifactSpec(
    "llm", "call_records.jsonl", ArtifactFormat.jsonl, LlmCallRecord
)

#: Every registered model-backed artifact slot, for inventory and lineage tooling.
ARTIFACT_SPECS: tuple[ArtifactSpec[Any], ...] = (
    SOURCE_SNAPSHOTS,
    SOURCE_PIECES,
    SOURCE_WINDOWS,
    MATERIAL_ATOMS,
    CANDIDATE_GRAPH,
    SELECTION_PLAN,
    NODE_EDIT_PLAN,
    EDITED_MATERIALS,
    CHUNK_PLAN,
    RENDER_CONTRACTS,
    NAME_RESOLUTION_PLAN,
    NAME_RESOLUTION_REPORT,
    RENDER_FACT_CONTEXTS,
    CHUNK_ARTIFACTS,
    AUDIT_REPORT,
    ASSEMBLY_REPORT,
    LLM_CALL_RECORDS,
)

_LLM_RAW_DIR = "llm/raw"


def _safe_segment(name: str, *, what: str) -> str:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"unsafe {what}: {name!r}")
    return name


class RunStore:
    """Read/write a single run's artifacts under ``runs/<run_id>/``."""

    def __init__(self, run_dir: str | os.PathLike[str]) -> None:
        self.run_dir = Path(run_dir)
        self._lock = RLock()

    @classmethod
    def for_run(cls, runs_root: str | os.PathLike[str], run_id: str) -> RunStore:
        """Build a store for ``run_id`` under ``runs_root`` (e.g. ``runs/``)."""
        _safe_segment(run_id, what="run_id")
        return cls(Path(runs_root) / run_id)

    @classmethod
    def create_for_run(
        cls,
        runs_root: str | os.PathLike[str],
        run_id: str,
    ) -> RunStore:
        """Reserve a new single-use run directory; never reuse an existing id."""

        store = cls.for_run(runs_root, run_id)
        store.run_dir.mkdir(parents=True)
        return store

    @property
    def run_id(self) -> str:
        return self.run_dir.name

    def path_for(self, relpath: str | PurePosixPath) -> Path:
        """Resolve a run-relative POSIX path, refusing to escape the run dir."""
        rel = PurePosixPath(str(relpath))
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe run-relative path: {relpath!r}")
        return self.run_dir.joinpath(*rel.parts)

    # -- model-backed artifacts --------------------------------------------

    def exists(self, spec: ArtifactSpec[Any] | TextArtifactSpec) -> bool:
        """Whether the artifact slot has been written."""
        return self.path_for(spec.relpath).exists()

    def write_model[ModelT: BaseModel](self, spec: ArtifactSpec[ModelT], model: ModelT) -> None:
        self._require_format(spec, ArtifactFormat.json_model)
        self._require_type(spec, model)
        json_io.write_json_model(self.path_for(spec.relpath), model)

    def read_model[ModelT: BaseModel](self, spec: ArtifactSpec[ModelT]) -> ModelT:
        self._require_format(spec, ArtifactFormat.json_model)
        return json_io.read_json_model(self._require_exists(spec), spec.model_type)

    def write_records[ModelT: BaseModel](
        self, spec: ArtifactSpec[ModelT], records: Iterable[ModelT]
    ) -> None:
        self._require_format(spec, ArtifactFormat.jsonl)
        rows = list(records)
        for record in rows:
            self._require_type(spec, record)
        jsonl_io.write_jsonl(self.path_for(spec.relpath), rows)

    def append_record[ModelT: BaseModel](self, spec: ArtifactSpec[ModelT], record: ModelT) -> None:
        self._require_format(spec, ArtifactFormat.jsonl)
        self._require_type(spec, record)
        with self._lock:
            jsonl_io.append_jsonl(self.path_for(spec.relpath), [record])

    def read_records[ModelT: BaseModel](self, spec: ArtifactSpec[ModelT]) -> list[ModelT]:
        self._require_format(spec, ArtifactFormat.jsonl)
        return jsonl_io.read_jsonl_models(self._require_exists(spec), spec.model_type)

    # -- text artifacts -----------------------------------------------------

    def write_text(self, spec: TextArtifactSpec, text: str) -> None:
        atomic_write_text(self.path_for(spec.relpath), text)

    def read_text(self, spec: TextArtifactSpec) -> str:
        path = self.path_for(spec.relpath)
        if not path.exists():
            raise FileNotFoundError(f"missing artifact {spec.relpath!r} in run {self.run_id!r}")
        return path.read_text(encoding="utf-8")

    # -- LLM raw payloads (addressed by call_id) ---------------------------

    def write_raw_payload(
        self, call_id: str, role: RawRole, payload: Mapping[str, Any] | str
    ) -> str:
        """Persist a raw provider request/response and return its run-relative path.

        The returned POSIX path is what goes into
        ``LlmCallRecord.raw_request_path`` / ``raw_response_path``. ``str``
        payloads are stored as ``.txt``; mappings as ``.json``.
        """
        self._check_role(role)
        _safe_segment(call_id, what="call_id")
        if isinstance(payload, str):
            relpath = f"{_LLM_RAW_DIR}/{call_id}.{role}.txt"
            atomic_write_text(self.path_for(relpath), payload)
        elif isinstance(payload, Mapping):
            relpath = f"{_LLM_RAW_DIR}/{call_id}.{role}.json"
            json_io.write_json(self.path_for(relpath), dict(payload))
        else:
            raise TypeError("raw payload must be a str or a mapping")
        return relpath

    def read_raw_payload(self, call_id: str, role: RawRole) -> dict[str, Any] | str:
        """Read back a raw payload written by :meth:`write_raw_payload`."""
        self._check_role(role)
        _safe_segment(call_id, what="call_id")
        json_path = self.path_for(f"{_LLM_RAW_DIR}/{call_id}.{role}.json")
        if json_path.exists():
            return json_io.read_json(json_path)
        text_path = self.path_for(f"{_LLM_RAW_DIR}/{call_id}.{role}.txt")
        if text_path.exists():
            return text_path.read_text(encoding="utf-8")
        raise FileNotFoundError(
            f"no raw {role} payload for call {call_id!r} in run {self.run_id!r}"
        )

    # -- internals ----------------------------------------------------------

    def _require_exists(self, spec: ArtifactSpec[Any]) -> Path:
        path = self.path_for(spec.relpath)
        if not path.exists():
            raise FileNotFoundError(f"missing artifact {spec.relpath!r} in run {self.run_id!r}")
        return path

    @staticmethod
    def _require_format(spec: ArtifactSpec[Any], expected: ArtifactFormat) -> None:
        if spec.format is not expected:
            raise ValueError(
                f"artifact {spec.relpath!r} is {spec.format.value!r}, not {expected.value!r}; "
                f"use the matching RunStore method"
            )

    @staticmethod
    def _require_type(spec: ArtifactSpec[Any], record: BaseModel) -> None:
        if not isinstance(record, spec.model_type):
            raise TypeError(
                f"artifact {spec.relpath!r} expects {spec.model_type.__name__}, "
                f"got {type(record).__name__}"
            )

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ("request", "response"):
            raise ValueError(f"raw payload role must be 'request' or 'response', got {role!r}")
