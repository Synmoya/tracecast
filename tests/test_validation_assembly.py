"""Validation and Assembly keep renderer text immutable."""

from __future__ import annotations

from datetime import UTC, datetime

from tracecast.assembly import assemble_script
from tracecast.domain import ChunkArtifact, ChunkPlan, ChunkPlanChunk
from tracecast.validation import ValidationContext, run_validation


def _chunk(chunk_id: str, order: int, text: str) -> ChunkArtifact:
    return ChunkArtifact(
        chunk_id=chunk_id,
        order_index=order,
        contract_id=f"contract_{order + 1}",
        text=text,
        input_hash=f"sha256:input-{order}",
        output_hash=f"sha256:output-{order}",
        generated_at=datetime(2026, 7, 27, tzinfo=UTC),
    )


def _plan() -> ChunkPlan:
    return ChunkPlan(
        plan_id="chunk-plan",
        project_id="proj",
        selection_plan_id="selection",
        node_edit_plan_id="node-edit",
        chunks=[
            ChunkPlanChunk(
                chunk_id="chunk_1",
                order_index=0,
                node_ids=["node_1"],
                material_ids=[],
                estimated_material_tokens=1,
            ),
            ChunkPlanChunk(
                chunk_id="chunk_2",
                order_index=1,
                node_ids=["node_2"],
                material_ids=[],
                estimated_material_tokens=1,
            ),
        ],
    )


def test_validation_reports_text_drift_but_never_normalizes_or_mutates_chunk() -> None:
    chunk = _chunk("chunk_1", 0, "Renderer returned Terra verbatim.")
    raw = chunk.model_dump(mode="json")

    report = run_validation(
        ValidationContext(
            chunks=[chunk],
            raw_chunk_text={"chunk_1": "Renderer returned a different body."},
        )
    )

    assert [item.kind for item in report.items] == ["text_changed_after_render"]
    assert not hasattr(report, "name_normalization")
    assert chunk.model_dump(mode="json") == raw
    assert chunk.text == "Renderer returned Terra verbatim."


def test_assembly_concatenates_chunk_text_verbatim_without_replacement_path() -> None:
    first = _chunk("chunk_1", 0, "Terra remains exactly Terra.\n")
    second = _chunk("chunk_2", 1, "\n泰拉也保持原样。")

    script, report = assemble_script(
        chunk_plan=_plan(),
        chunks=[second, first],
        validation_report_ref="validation/audit_report.jsonl",
    )

    assert script == first.text + "\n\n" + second.text
    assert report.assembled_chunk_ids == ["chunk_1", "chunk_2"]
    assert report.assembled_with_issues is False
    assert not hasattr(report, "name_normalization_report_ref")
