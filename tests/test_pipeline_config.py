"""Project source-config loading regressions."""

from __future__ import annotations

from pathlib import Path

from tracecast.ingestion import build_source_inputs
from tracecast.project import load_project
from tracecast.storage import content_hash


def test_build_source_inputs_preserves_explicit_intake_source_fields(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "demo"
    raw = project / "raw"
    raw.mkdir(parents=True)
    raw_text = "TERRA: Who am I?"
    (raw / "script.txt").write_text(raw_text, encoding="utf-8")
    declared_hash = f"sha256:{content_hash(raw_text)}"
    (project / "sources.json").write_text(
        f"""{{
  "project_id": "demo",
  "output_language": "zh-CN",
  "sources": [
    {{
      "source_id": "script",
      "path": "raw/script.txt",
      "profile": "script_transcript",
      "language": "en",
      "source_type": "local_text",
      "source_class": "script_transcript",
      "trust_tier": "canonical",
      "title": "Demo Script",
      "note": "manual note",
      "content_hash": "{declared_hash}",
      "metadata": {{"collection": "demo"}}
    }}
  ]
}}
""",
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles.json"
    profiles.write_text(
        """{
  "profiles": {
    "script_transcript": {
      "description": "Script.",
      "default_source_class": "fallback_class",
      "default_trust_tier": "low",
      "allowed_content_kinds": ["dialogue"],
      "allowed_tags": []
    }
  }
}
""",
        encoding="utf-8",
    )

    loaded = load_project(project, profiles)
    [source_input] = build_source_inputs(loaded, project)
    snapshot = source_input.snapshot

    assert not hasattr(loaded, "persona")
    assert snapshot.source_type == "local_text"
    assert snapshot.source_class == "script_transcript"
    assert snapshot.trust_tier == "canonical"
    assert snapshot.title == "Demo Script"
    assert snapshot.content_hash == declared_hash
    assert snapshot.metadata == {"collection": "demo", "note": "manual note"}
