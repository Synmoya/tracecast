"""Local source decoding and exclusive project creation for Console onboarding."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tracecast.intake import estimate_intake, run_intake
from tracecast.intake.sampling import unique_source_ids
from tracecast.language import require_bcp47_language_tag
from tracecast.llm import Usage, estimate_cost, resolve_cost_profile
from tracecast.name_resolution import NameResolutionMode
from tracecast.project import validate_project_id
from tracecast.storage import content_hash

from .context import ConsoleContext
from .errors import BadRequest, Conflict, ConsoleError, NotFound
from .run_service import progress_call

DraftMode = Literal["manual", "auto"]

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".srt", ".vtt"}
MANUAL_ENCODINGS = {
    "utf-8": "utf-8",
    "utf-16-le": "utf-16-le",
    "utf-16-be": "utf-16-be",
    "gb18030": "gb18030",
    "shift-jis": "shift_jis",
    "cp949": "cp949",
    "windows-1252": "cp1252",
}
_BOMS: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xfe\x00\x00", "UTF-32 LE", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "UTF-32 BE", "utf-32-be"),
    (b"\xef\xbb\xbf", "UTF-8", "utf-8"),
    (b"\xff\xfe", "UTF-16 LE", "utf-16-le"),
    (b"\xfe\xff", "UTF-16 BE", "utf-16-be"),
)
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class UploadedProjectFile:
    name: str
    data_base64: str
    encoding: str | None = None
    profile: str | None = None
    language: str | None = None


@dataclass
class DraftFile:
    name: str
    upload_path: Path
    raw_path: Path | None
    encoding: str | None
    detection_method: str | None
    original_sha256: str
    normalized_content_hash: str | None
    transcoded: bool
    preview: str
    candidate_previews: dict[str, str] = field(default_factory=dict)
    profile: str | None = None
    language: str | None = None


@dataclass
class ProjectDraft:
    draft_id: str
    project_id: str
    output_language: str
    name_resolution_mode: NameResolutionMode
    mode: DraftMode
    root: Path
    files: list[DraftFile]
    proposal: dict[str, Any] | None = None
    intake_run_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())


def create_project_draft(
    ctx: ConsoleContext,
    *,
    project_id: str,
    output_language: str,
    name_resolution_mode: str,
    mode: DraftMode,
    files: list[UploadedProjectFile],
) -> dict[str, Any]:
    """Decode uploads into an isolated draft workspace; never touch projects/."""

    try:
        project_id = validate_project_id(project_id)
        if output_language.casefold() in {"unknown", "und"}:
            raise ValueError("output_language must be an explicit target language")
        output_language = require_bcp47_language_tag(
            output_language, field="output_language"
        )
        resolution_mode = NameResolutionMode(name_resolution_mode)
    except (TypeError, ValueError) as exc:
        raise BadRequest(str(exc)) from exc
    if mode not in {"manual", "auto"}:
        raise BadRequest("mode must be manual or auto")
    if (ctx.projects_dir / project_id).exists():
        raise Conflict(f"project {project_id!r} already exists; project creation never overwrites")
    if not files:
        raise BadRequest("at least one source file is required")

    names: set[str] = set()
    for upload in files:
        _validate_upload_name(upload.name)
        folded = upload.name.casefold()
        if folded in names:
            raise BadRequest(f"duplicate target filename: {upload.name!r}")
        names.add(folded)

    root = Path(tempfile.mkdtemp(prefix=f"tracecast-project-draft-{project_id}-"))
    (root / "uploads").mkdir()
    (root / "raw").mkdir()
    decoded_files: list[DraftFile] = []
    try:
        for index, upload in enumerate(files, start=1):
            try:
                raw = base64.b64decode(upload.data_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise BadRequest(f"file {upload.name!r} is not valid base64") from exc
            if not raw:
                raise BadRequest(f"file {upload.name!r} is empty")
            upload_path = root / "uploads" / f"{index:04d}.bin"
            upload_path.write_bytes(raw)
            decoded = _decode_upload(upload.name, raw, upload.encoding)
            raw_path: Path | None = None
            normalized_hash: str | None = None
            transcoded = False
            if decoded[0] is not None:
                text, encoding, method, candidates = decoded
                assert text is not None and encoding is not None and method is not None
                normalized = text.encode("utf-8")
                raw_path = root / "raw" / upload.name
                raw_path.write_bytes(normalized)
                normalized_hash = content_hash(text)
                transcoded = method != "strict_utf8" or normalized != raw
                preview = text[:1200]
            else:
                _text, encoding, method, candidates = decoded
                preview = next(iter(candidates.values()), "")
            decoded_files.append(
                DraftFile(
                    name=upload.name,
                    upload_path=upload_path,
                    raw_path=raw_path,
                    encoding=encoding,
                    detection_method=method,
                    original_sha256=hashlib.sha256(raw).hexdigest(),
                    normalized_content_hash=normalized_hash,
                    transcoded=transcoded,
                    preview=preview,
                    candidate_previews=candidates,
                    profile=upload.profile,
                    language=upload.language,
                )
            )
        draft = ProjectDraft(
            draft_id=f"draft-{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            output_language=output_language,
            name_resolution_mode=resolution_mode,
            mode=mode,
            root=root,
            files=decoded_files,
        )
        _write_draft_stub(draft)
        if mode == "manual" and _all_files_decoded(draft):
            draft.proposal = _manual_proposal(ctx, draft)
        ctx.project_drafts[draft.draft_id] = draft
        return draft_view(draft)
    except ConsoleError:
        shutil.rmtree(root, ignore_errors=True)
        raise
    except (OSError, UnicodeError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise BadRequest(f"cannot stage project draft: {type(exc).__name__}") from exc


def estimate_project_draft(ctx: ConsoleContext, draft_id: str) -> dict[str, Any]:
    """Return exact Intake preparation cost or a zero-call manual estimate."""

    draft = _draft(ctx, draft_id)
    _require_decoded(draft)
    if draft.mode == "manual":
        proposal = _manual_proposal(ctx, draft)
        draft.proposal = proposal
        return {
            "calls": 0,
            "estimated_input_tokens": 0,
            "models": {},
            "estimated_max_output_tokens": 0,
            "configured_cost": 0.0,
            "rate_status": "no_calls",
            "rates_source": "not_used",
            "sources": len(draft.files),
            "mode": "manual",
            "notes": ["manual source profiling makes no model calls"],
        }
    try:
        config = ctx.load_config()
        estimate = estimate_intake(
            project_dir=draft.root,
            profiles_path=ctx.profiles_path,
            config=config,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot estimate Intake draft: {exc}") from exc
    route = config.route("source_profile_probe")
    max_output_tokens = (route.max_output_tokens or 0) * estimate.calls
    profile, rates_source = resolve_cost_profile(route.cost_profile, config)
    configured_cost = (
        estimate_cost(
            Usage(
                input_tokens=estimate.estimated_input_tokens,
                uncached_input_tokens=estimate.estimated_input_tokens,
                output_tokens=max_output_tokens,
            ),
            profile,
        )
        if rates_source == "runtime_config"
        else None
    )
    return {
        "calls": estimate.calls,
        "estimated_input_tokens": estimate.estimated_input_tokens,
        "models": estimate.models,
        "estimated_max_output_tokens": max_output_tokens,
        "configured_cost": configured_cost,
        "rate_status": "configured" if configured_cost is not None else "unconfigured",
        "rates_source": rates_source,
        "sources": estimate.sources,
        "source_stats": list(estimate.source_stats),
        "mode": "auto",
        "control_prompt_decision": estimate.control_prompt_decision.model_dump(mode="json"),
        "notes": ["estimate uses exact Intake sampling and batching; no model was called"],
    }


def confirm_project_draft(ctx: ConsoleContext, draft_id: str) -> dict[str, Any]:
    """Run automatic Intake as a single-flight job, or confirm manual decisions."""

    draft = _draft(ctx, draft_id)
    _require_decoded(draft)
    if draft.mode == "manual":
        draft.proposal = _manual_proposal(ctx, draft)
        return {"draft": draft_view(draft), "proposal": draft.proposal, "calls": 0}
    estimate_payload = estimate_project_draft(ctx, draft_id)
    run_id = (
        f"{draft.project_id}-intake-draft-"
        f"{datetime.now(tz=UTC):%Y%m%dT%H%M%S%fZ}"
    )

    def target(job: Any) -> dict[str, Any]:
        result = run_intake(
            project_dir=draft.root,
            profiles_path=ctx.profiles_path,
            output_language=draft.output_language,
            config=ctx.load_config(),
            runs_root=ctx.runs_root,
            run_id=run_id,
            write_sources=False,
            call=progress_call(ctx, job, "project intake"),
        )
        proposal = json.loads((draft.root / "sources.proposed.json").read_text(encoding="utf-8"))
        proposal["name_resolution_mode"] = draft.name_resolution_mode.value
        proposal = _merge_import_metadata(draft, proposal)
        draft.proposal = proposal
        draft.intake_run_id = result.run_id
        return {
            "draft_id": draft.draft_id,
            "intake_run_id": result.run_id,
            "proposal": proposal,
            "needs_review": result.needs_review,
            "calls": result.calls,
            "control_prompt_decision": result.control_prompt_decision.model_dump(mode="json"),
        }

    job = ctx.registry.submit(
        kind="project_intake",
        run_id=run_id,
        base_run_id=None,
        target=target,
        progress_total=estimate_payload["calls"],
    )
    return {"job_id": job.job_id, "draft_id": draft_id, "estimate": estimate_payload}


def commit_project_draft(
    ctx: ConsoleContext,
    draft_id: str,
    *,
    sources: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Validate reviewed decisions and exclusive-create a complete project directory."""

    draft = _draft(ctx, draft_id)
    _require_decoded(draft)
    proposal = dict(draft.proposal or {})
    if sources is not None:
        proposal["sources"] = sources
    rows = _validated_source_rows(ctx, draft, proposal.get("sources"))
    project_payload = {
        "project_id": validate_project_id(draft.project_id),
        "output_language": require_bcp47_language_tag(
            draft.output_language, field="output_language"
        ),
        "name_resolution_mode": draft.name_resolution_mode.value,
        "sources": rows,
    }
    target = ctx.projects_dir / draft.project_id
    ctx.projects_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise Conflict(f"project {draft.project_id!r} already exists; commit never overwrites")
    staging = Path(tempfile.mkdtemp(prefix=f".{draft.project_id}-", dir=ctx.projects_dir))
    lock_path = ctx.projects_dir / f".{draft.project_id}.create.lock"
    lock_fd: int | None = None
    try:
        raw_dir = staging / "raw"
        raw_dir.mkdir()
        original_dir = staging / "imports" / "original"
        for file in draft.files:
            assert file.raw_path is not None
            (raw_dir / file.name).write_bytes(file.raw_path.read_bytes())
            if file.transcoded:
                original_dir.mkdir(parents=True, exist_ok=True)
                (original_dir / file.name).write_bytes(file.upload_path.read_bytes())
        (staging / "sources.json").write_bytes(
            (json.dumps(project_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise Conflict(f"project {draft.project_id!r} is being created") from None
        if target.exists():
            raise Conflict(f"project {draft.project_id!r} already exists; commit never overwrites")
        staging.rename(target)
    except (OSError, UnicodeError) as exc:
        raise BadRequest(f"cannot commit project files: {type(exc).__name__}") from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    ctx.project_drafts.pop(draft_id, None)
    shutil.rmtree(draft.root, ignore_errors=True)
    return {
        "project_id": draft.project_id,
        "path": str(target),
        "sources": len(rows),
        "output_language": draft.output_language,
        "name_resolution_mode": draft.name_resolution_mode.value,
    }


def draft_view(draft: ProjectDraft) -> dict[str, Any]:
    return {
        "draft_id": draft.draft_id,
        "project_id": draft.project_id,
        "output_language": draft.output_language,
        "name_resolution_mode": draft.name_resolution_mode.value,
        "mode": draft.mode,
        "created_at": draft.created_at,
        "ready": _all_files_decoded(draft),
        "files": [
            {
                "name": file.name,
                "status": "ready" if file.raw_path is not None else "needs_encoding",
                "encoding": file.encoding,
                "detection_method": file.detection_method,
                "original_sha256": file.original_sha256,
                "normalized_content_hash": file.normalized_content_hash,
                "transcoded": file.transcoded,
                "preview": file.preview,
                "candidate_previews": file.candidate_previews,
                "profile": file.profile,
                "language": file.language,
            }
            for file in draft.files
        ],
        "proposal": draft.proposal,
        "intake_run_id": draft.intake_run_id,
    }


def _decode_upload(
    name: str, raw: bytes, explicit_encoding: str | None
) -> tuple[str | None, str | None, str | None, dict[str, str]]:
    if explicit_encoding:
        label = explicit_encoding.strip().casefold().replace("_", "-")
        codec = MANUAL_ENCODINGS.get(label)
        if codec is None:
            raise BadRequest(
                f"file {name!r} encoding must be one of: " + ", ".join(MANUAL_ENCODINGS)
            )
        try:
            text = raw.decode(codec, errors="strict")
        except UnicodeDecodeError as exc:
            raise BadRequest(f"file {name!r} cannot decode as {label}: {exc}") from exc
        if text.startswith("\ufeff"):
            text = text[1:]
        _validate_decoded_text(name, text)
        return text, label, "manual", {}

    for bom, label, codec in _BOMS:
        if not raw.startswith(bom):
            continue
        try:
            text = raw[len(bom):].decode(codec, errors="strict")
        except UnicodeDecodeError as exc:
            raise BadRequest(f"file {name!r} has invalid {label} BOM content: {exc}") from exc
        _validate_decoded_text(name, text)
        return text, label, "bom", {}
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        _validate_decoded_text(name, text)
        return text, "UTF-8", "strict_utf8", {}
    try:
        text = raw.decode("gb18030", errors="strict")
    except UnicodeDecodeError:
        text = None
    if text is not None and _text_quality(text)[0]:
        candidates = _manual_candidate_previews(raw)
        shift_jis = candidates.get("shift-jis")
        cp949 = candidates.get("cp949")
        if (
            shift_jis is not None and _has_significant_script(shift_jis, "kana")
        ) or (cp949 is not None and _has_significant_script(cp949, "hangul")):
            candidates["gb18030"] = text[:1200]
            return None, None, "needs_manual_encoding", candidates
        return text, "GB18030", "gb18030_fallback", {}

    candidates = _manual_candidate_previews(raw)
    if not candidates:
        raise BadRequest(f"file {name!r} has clear binary features or no usable text decoding")
    return None, None, "needs_manual_encoding", candidates


def _manual_candidate_previews(raw: bytes) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for label, codec in MANUAL_ENCODINGS.items():
        try:
            candidate = raw.decode(codec, errors="strict")
        except UnicodeDecodeError:
            continue
        if _text_quality(candidate)[0]:
            candidates[label] = candidate[:1200]
    return candidates


def _has_significant_script(text: str, script: Literal["kana", "hangul"]) -> bool:
    visible = [char for char in text if not char.isspace() and char.isprintable()]
    if not visible:
        return False
    if script == "kana":
        count = sum(
            "\u3040" <= char <= "\u30ff" or "\uff66" <= char <= "\uff9d"
            for char in visible
        )
    else:
        count = sum(
            "\u1100" <= char <= "\u11ff"
            or "\u3130" <= char <= "\u318f"
            or "\uac00" <= char <= "\ud7af"
            for char in visible
        )
    minimum_share = 0.25 if script == "kana" else 0.70
    return count >= 3 and count / len(visible) >= minimum_share


def _validate_decoded_text(name: str, text: str) -> None:
    if not text:
        raise BadRequest(f"file {name!r} contains no text after decoding")
    acceptable, reason = _text_quality(text)
    if not acceptable:
        raise BadRequest(f"file {name!r} looks binary after decoding: {reason}")


def _text_quality(text: str) -> tuple[bool, str]:
    if not text:
        return False, "empty text"
    if "\x00" in text:
        return False, "contains NUL characters"
    controls = sum(
        1 for char in text if ord(char) < 32 and char not in {"\t", "\r", "\n"}
    )
    if controls > max(2, len(text) // 100):
        return False, "too many control characters"
    printable = sum(char.isprintable() or char in {"\t", "\r", "\n"} for char in text)
    if printable / len(text) < 0.85:
        return False, "printable-character ratio is too low"
    return True, "ok"


def _validate_upload_name(name: str) -> None:
    if not isinstance(name, str) or not name or name != Path(name).name:
        raise BadRequest(f"unsafe source filename: {name!r}")
    if len(name) > 240 or len(name.encode("utf-8")) > 240:
        raise BadRequest(f"source filename is too long: {name!r}")
    if name in {".", ".."} or name.endswith((" ", ".")):
        raise BadRequest(f"unsafe source filename: {name!r}")
    if any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name):
        raise BadRequest(f"unsafe source filename: {name!r}")
    if Path(name).suffix.casefold() not in SUPPORTED_EXTENSIONS:
        raise BadRequest(
            f"unsupported source extension for {name!r}; allowed: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS))
        )
    if name.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
        raise BadRequest(f"unsafe reserved source filename: {name!r}")


def _manual_proposal(ctx: ConsoleContext, draft: ProjectDraft) -> dict[str, Any]:
    profiles = _profiles(ctx)
    paths = [f"raw/{file.name}" for file in draft.files]
    ids = unique_source_ids(paths)
    rows: list[dict[str, Any]] = []
    for file, path in zip(draft.files, paths, strict=True):
        if not file.profile or file.profile not in profiles:
            raise BadRequest(
                f"manual mode requires a known profile for file {file.name!r}"
            )
        try:
            language = require_bcp47_language_tag(
                file.language or "", field=f"source language for {file.name}"
            )
        except (TypeError, ValueError) as exc:
            raise BadRequest(str(exc)) from exc
        if language.casefold() in {"unknown", "und"}:
            raise BadRequest(f"source {file.name!r} needs a concrete language")
        template = profiles[file.profile]
        rows.append(
            _source_row(
                draft,
                file,
                source_id=ids[path],
                profile=file.profile,
                language=language,
                title=Path(file.name).stem,
                note="",
                template=template,
            )
        )
    return {
        "project_id": draft.project_id,
        "output_language": draft.output_language,
        "name_resolution_mode": draft.name_resolution_mode.value,
        "sources": rows,
    }


def _validated_source_rows(
    ctx: ConsoleContext, draft: ProjectDraft, raw_rows: object
) -> list[dict[str, Any]]:
    if not isinstance(raw_rows, list):
        raise BadRequest("reviewed proposal must contain a sources list")
    if len(raw_rows) != len(draft.files):
        raise BadRequest("every uploaded source must have one reviewed source decision")
    profiles = _profiles(ctx)
    file_by_path = {f"raw/{file.name}": file for file in draft.files}
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for value in raw_rows:
        if not isinstance(value, dict):
            raise BadRequest("every source decision must be an object")
        if str(value.get("status") or "").casefold() in {"needs_review", "unknown"}:
            raise BadRequest("every source decision must be explicitly reviewed before commit")
        path = str(value.get("path") or "").replace("\\", "/")
        file = file_by_path.get(path)
        if file is None or path in seen_paths:
            raise BadRequest(f"source decision has unknown or duplicate path: {path!r}")
        source_id = str(value.get("source_id") or "")
        if not _SOURCE_ID_RE.fullmatch(source_id) or source_id in seen_ids:
            raise BadRequest(f"source_id must be unique and path-safe: {source_id!r}")
        profile = str(value.get("profile") or "")
        if profile == "unknown" or profile not in profiles:
            raise BadRequest(f"source {source_id!r} needs a known profile before commit")
        language_value = str(value.get("language") or "")
        if language_value.casefold() in {"unknown", "und"}:
            raise BadRequest(f"source {source_id!r} needs a language before commit")
        try:
            language = require_bcp47_language_tag(
                language_value, field=f"source language for {source_id}"
            )
        except (TypeError, ValueError) as exc:
            raise BadRequest(str(exc)) from exc
        rows.append(
            _source_row(
                draft,
                file,
                source_id=source_id,
                profile=profile,
                language=language,
                title=str(value.get("title") or Path(file.name).stem),
                note=str(value.get("note") or ""),
                template=profiles[profile],
            )
        )
        seen_paths.add(path)
        seen_ids.add(source_id)
    return rows


def _source_row(
    draft: ProjectDraft,
    file: DraftFile,
    *,
    source_id: str,
    profile: str,
    language: str,
    title: str,
    note: str,
    template: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "path": f"raw/{file.name}",
        "profile": profile,
        "language": language,
        "source_type": "local_text",
        "source_class": template["default_source_class"],
        "trust_tier": template["default_trust_tier"],
        "title": title,
        "note": note,
        "content_hash": file.normalized_content_hash,
        "metadata": {
            "import": {
                "original_filename": file.name,
                "original_byte_sha256": file.original_sha256,
                "encoding": file.encoding,
                "encoding_detection": file.detection_method,
                "original_preserved": file.transcoded,
            }
        },
    }


def _merge_import_metadata(draft: ProjectDraft, proposal: dict[str, Any]) -> dict[str, Any]:
    file_by_path = {f"raw/{file.name}": file for file in draft.files}
    merged = dict(proposal)
    rows: list[dict[str, Any]] = []
    for raw_row in proposal.get("sources", ()):
        row = dict(raw_row)
        file = file_by_path.get(str(row.get("path") or "").replace("\\", "/"))
        if file is not None:
            row["content_hash"] = file.normalized_content_hash
            metadata = dict(row.get("metadata") or {})
            metadata["import"] = _source_row(
                draft,
                file,
                source_id=str(row.get("source_id") or "source"),
                profile=str(row.get("profile") or "unknown"),
                language=str(row.get("language") or "unknown"),
                title=str(row.get("title") or ""),
                note=str(row.get("note") or ""),
                template={"default_source_class": "", "default_trust_tier": ""},
            )["metadata"]["import"]
            row["metadata"] = metadata
        rows.append(row)
    merged["sources"] = rows
    return merged


def _write_draft_stub(draft: ProjectDraft) -> None:
    payload = {
        "project_id": draft.project_id,
        "output_language": draft.output_language,
        "name_resolution_mode": draft.name_resolution_mode.value,
        "sources": [],
    }
    (draft.root / "sources.json").write_bytes(
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def _profiles(ctx: ConsoleContext) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(ctx.profiles_path.read_text(encoding="utf-8"))
        profiles = payload["profiles"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise BadRequest(f"cannot load source profile templates: {exc}") from exc
    return profiles


def _draft(ctx: ConsoleContext, draft_id: str) -> ProjectDraft:
    value = ctx.project_drafts.get(draft_id)
    if not isinstance(value, ProjectDraft):
        raise NotFound(f"unknown project draft {draft_id!r}")
    return value


def _require_decoded(draft: ProjectDraft) -> None:
    pending = [file.name for file in draft.files if file.raw_path is None]
    if pending:
        raise BadRequest(
            "explicit encoding is required before continuing: " + ", ".join(pending)
        )


def _all_files_decoded(draft: ProjectDraft) -> bool:
    return all(file.raw_path is not None for file in draft.files)


__all__ = [
    "MANUAL_ENCODINGS",
    "SUPPORTED_EXTENSIONS",
    "UploadedProjectFile",
    "commit_project_draft",
    "confirm_project_draft",
    "create_project_draft",
    "draft_view",
    "estimate_project_draft",
]
