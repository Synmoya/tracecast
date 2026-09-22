"""Independent regressions for isolated Host Builder sample fragments."""

from pathlib import Path

import pytest
from test_host_builder_pipeline import HostBuildFakeCall, _config, _manifest

from tracecast.domain import estimate_tokens
from tracecast.host_builder import run_host_build
from tracecast.host_builder.sampling import split_preference_spans
from tracecast.hosts import load_host
from tracecast.storage import LLM_CALL_RECORDS, RunStore, content_hash


@pytest.mark.parametrize(
    "text",
    [
        "短引子。\n\n" + "甲" * 540,
        "甲" * 180 + "小碎片。\n\n" + "乙" * 360,
        "甲" * 180 + "短尾巴。",
        "A.\n\n" + "context " * 160 + "Done.",
    ],
)
def test_default_samples_do_not_isolate_short_boundary_fragments(text: str) -> None:
    spans = split_preference_spans(text)

    assert "".join(text[start:end] for start, end in spans) == text
    assert all(32 <= estimate_tokens(text[start:end]) <= 180 for start, end in spans)
    assert spans == split_preference_spans(text)


@pytest.mark.parametrize("budget", [1, 7, 16, 32, 64, 180])
@pytest.mark.parametrize("body", ["甲" * 363 + "。尾", "word " * 290 + "x"])
def test_fragment_balancing_preserves_span_and_hard_budget(budget: int, body: str) -> None:
    text = "outside[" + body + "]outside"
    spans = split_preference_spans(text, start=8, end=8 + len(body), max_tokens=budget)

    assert spans[0][0] == 8
    assert spans[-1][1] == 8 + len(body)
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:], strict=False))
    assert "".join(text[start:end] for start, end in spans) == body
    assert all(0 < estimate_tokens(text[start:end]) <= budget for start, end in spans)


def test_useful_paragraph_boundary_and_short_complete_input_remain_valid() -> None:
    paragraph = "甲" * 60 + "。\n\n"
    text = paragraph + "乙" * 250

    assert split_preference_spans(text)[0] == (0, len(paragraph))
    assert split_preference_spans("短成稿。") == [(0, 4)]


def test_balanced_samples_install_with_two_offline_calls_and_literal_renders(
    tmp_path: Path,
) -> None:
    inputs = []
    originals = {}
    for index, char in enumerate("甲乙", start=1):
        path = tmp_path / f"sample-{index}.txt"
        text = char * 180 + "小碎片。\n\n" + char * 360 + "短尾巴。"
        path.write_text(text, encoding="utf-8")
        inputs.append(path)
        originals[str(path.resolve())] = text
    fake = HostBuildFakeCall()

    result = run_host_build(
        inputs=inputs,
        host_id="balanced-host",
        config=_config(),
        seed=29,
        hosts_dir=tmp_path / "hosts",
        runs_root=tmp_path / "runs",
        run_id="balanced-run",
        call=fake,
    )

    run_dir = tmp_path / "runs" / "balanced-run"
    manifest = _manifest(run_dir)
    assert result.calls == len(fake.calls) == 2
    assert manifest.status == "complete"
    assert load_host(tmp_path / "hosts", "balanced-host") == result.host
    renders = [
        example.render
        for position in ("opening", "middle", "ending")
        for example in result.host.style_examples_by_position[position]
    ]
    for selected, render in zip(manifest.selected_examples, renders, strict=True):
        assert render == originals[selected.input_path][selected.span_start:selected.span_end]
        assert selected.render_hash == content_hash(render)
        assert 32 <= estimate_tokens(render) <= 180
        assert render in fake.calls[1]["messages"][1]["content"]
    records = RunStore(run_dir).read_records(LLM_CALL_RECORDS)
    assert [row.call_id for row in records] == manifest.call_ids
    assert len(records) == 2
    assert (run_dir / records[1].raw_response_path).read_text(encoding="utf-8") == (
        fake.examples_response
    )
