"""Independent spec-conformance tests for ``tracecast.domain``.

Assertions exercise the public record validators, enums, stable-id functions and
the vocabulary in ``configs/source_profile_templates.example.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracecast import domain
from tracecast.domain import (
    CANDIDATE_ALIASES,
    MATERIAL_ALIASES,
    NODE_ALIASES,
    PIECE_ALIASES,
    RENDER_ALIASES,
    AliasManifest,
    CandidateNode,
    ContentKind,
    CostKind,
    MaterialAtom,
    MaterialEdit,
    MaterialOmission,
    NameOverride,
    NameResolutionPlan,
    NameResolutionRule,
    NodeEditOperation,
    Provenance,
    ProviderStatus,
    RenderContractMaterial,
    Role,
    SelectionDecision,
    SelectionStatus,
    SourcePiece,
    SourceSpan,
    Treatment,
    TrustTier,
    atom_id,
    candidate_id,
    estimate_tokens,
    name_resolution_plan_id,
    name_resolution_rule_id,
    new_call_id,
    piece_id,
    window_id,
)

_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "source_profile_templates.example.json"
)


def _profiles() -> dict:
    return json.loads(_CONFIG.read_text(encoding="utf-8"))["profiles"]


# --- builders --------------------------------------------------------------


def make_provenance(**kw) -> Provenance:
    base = dict(
        source_ref="src",
        source_type="script_transcript",
        source_class="script_transcript",
        source_content_hash="hash0",
    )
    base.update(kw)
    return Provenance(**base)


def make_atom(**kw) -> MaterialAtom:
    base = dict(
        id="atom_x",
        content="content",
        language="zh",
        content_kind=ContentKind.assertion,
        source_order=0,
        trust_tier=TrustTier.canonical,
        provenance=make_provenance(),
    )
    base.update(kw)
    return MaterialAtom(**base)


def make_render_material(**kw) -> RenderContractMaterial:
    base = dict(
        material_ref="R1",
        source_material_id="atom_x",
        node_ref="N1",
        operation=NodeEditOperation.keep,
        content="c",
        language="zh",
        content_kind=ContentKind.assertion,
    )
    base.update(kw)
    return RenderContractMaterial(**base)


def test_material_atom_rejects_removed_attribution_field() -> None:
    with pytest.raises(ValidationError, match="attribution"):
        make_atom(attribution="Legacy Author")


def test_render_contract_material_rejects_removed_attribution_field() -> None:
    with pytest.raises(ValidationError, match="attribution"):
        make_render_material(attribution="Legacy Author")


# === 1. Enums ==============================================================


def test_content_kind_matches_config_union() -> None:
    expected: set[str] = set()
    for profile in _profiles().values():
        expected.update(profile["allowed_content_kinds"])
    assert {e.value for e in ContentKind} == expected


def test_trust_tier_matches_config_and_spec() -> None:
    config_defaults = {p["default_trust_tier"] for p in _profiles().values()}
    values = {e.value for e in TrustTier}
    assert config_defaults <= values
    assert values == {"canonical", "high", "low"}


@pytest.mark.parametrize(
    ("enum_cls", "expected"),
    [
        (Role, {"core", "bridge", "context", "color"}),
        (Treatment, {"quote", "paraphrase", "summarize"}),
        (NodeEditOperation, {"keep", "condense", "omit"}),
        (SelectionStatus, {"accepted", "not_selected"}),
    ],
)
def test_enum_value_sets(enum_cls, expected: set[str]) -> None:
    assert {e.value for e in enum_cls} == expected


def test_cost_kind_includes_configured_estimate() -> None:
    # Configured estimates are distinct from provider invoices; the set can evolve.
    assert "configured_estimate" in {e.value for e in CostKind}
    assert CostKind.configured_estimate == "configured_estimate"


def test_provider_status_is_string_enum() -> None:
    members = list(ProviderStatus)
    assert members
    assert all(isinstance(m, str) for m in members)


def test_enums_behave_as_strings() -> None:
    assert ContentKind.dialogue == "dialogue"
    assert isinstance(ContentKind.dialogue, str)
    assert Role.core == "core"
    assert TrustTier.canonical == "canonical"
    assert NodeEditOperation.condense == "condense"


# === 2. Intra-record constraints ==========================================


def test_source_span_rejects_end_before_start() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(start=5, end=3)


def test_source_span_allows_empty_span() -> None:
    span = SourceSpan(start=4, end=4)
    assert span.start == span.end == 4


def test_source_span_rejects_negative_start() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(start=-1, end=0)


def test_source_piece_rejects_negative_order_index() -> None:
    with pytest.raises(ValidationError):
        SourcePiece(
            piece_id="src:000000",
            source_id="src",
            order_index=-1,
            text="x",
            char_count=1,
        )


def test_selection_decision_accepted_requires_role_and_treatment() -> None:
    with pytest.raises(ValidationError):
        SelectionDecision(candidate_id="c1", status=SelectionStatus.accepted)
    with pytest.raises(ValidationError):
        SelectionDecision(
            candidate_id="c1",
            status=SelectionStatus.accepted,
            treatment=Treatment.quote,
        )
    with pytest.raises(ValidationError):
        SelectionDecision(
            candidate_id="c1",
            status=SelectionStatus.accepted,
            role=Role.core,
        )


def test_selection_decision_accepted_allows_null_or_nonblank_directive() -> None:
    without_directive = SelectionDecision(
        candidate_id="c1",
        status=SelectionStatus.accepted,
        role=Role.core,
        treatment=Treatment.quote,
    )
    decision = SelectionDecision(
        candidate_id="c2",
        status=SelectionStatus.accepted,
        role=Role.core,
        treatment=Treatment.quote,
        directive="让这个状态变化成为本段的中心。",
    )
    assert without_directive.directive is None
    assert decision.role is Role.core
    assert decision.treatment is Treatment.quote
    assert decision.directive == "让这个状态变化成为本段的中心。"
    assert "reason" not in decision.model_dump()


@pytest.mark.parametrize("directive", ["", " ", "\t\r\n"])
def test_selection_decision_rejects_blank_directive(directive: str) -> None:
    with pytest.raises(ValidationError, match="non-blank or null"):
        SelectionDecision(
            candidate_id="c1",
            status=SelectionStatus.accepted,
            role=Role.core,
            treatment=Treatment.quote,
            directive=directive,
        )


def test_selection_decision_not_selected_needs_no_role_or_treatment() -> None:
    decision = SelectionDecision(candidate_id="c1", status=SelectionStatus.not_selected)
    assert decision.role is None
    assert decision.treatment is None


def test_material_edit_keep_valid() -> None:
    edit = MaterialEdit(operation=NodeEditOperation.keep, source_material_ids=["m1"])
    assert edit.result_material is None


def test_material_edit_keep_rejects_result_material() -> None:
    with pytest.raises(ValidationError):
        MaterialEdit(
            operation=NodeEditOperation.keep,
            source_material_ids=["m1"],
            result_material=make_atom(),
        )


def test_material_edit_keep_rejects_multiple_sources() -> None:
    with pytest.raises(ValidationError):
        MaterialEdit(operation=NodeEditOperation.keep, source_material_ids=["m1", "m2"])


def test_material_edit_condense_valid_with_lineage() -> None:
    edit = MaterialEdit(
        operation=NodeEditOperation.condense,
        source_material_ids=["m1"],
        result_material=make_atom(id="atom_c", source_material_ids=["m1"]),
    )
    assert edit.result_material is not None
    assert edit.result_material.source_material_ids == ["m1"]


def test_material_edit_condense_requires_result_material() -> None:
    with pytest.raises(ValidationError):
        MaterialEdit(operation=NodeEditOperation.condense, source_material_ids=["m1"])


def test_material_edit_condense_requires_result_lineage() -> None:
    with pytest.raises(ValidationError):
        MaterialEdit(
            operation=NodeEditOperation.condense,
            source_material_ids=["m1"],
            result_material=make_atom(id="atom_c"),  # source_material_ids empty
        )


def test_material_edit_rejects_omit() -> None:
    with pytest.raises(ValidationError):
        MaterialEdit(operation=NodeEditOperation.omit, source_material_ids=["m1"])


def test_render_material_keep_valid() -> None:
    mat = make_render_material()
    assert mat.operation is NodeEditOperation.keep


def test_render_material_rejects_omit() -> None:
    with pytest.raises(ValidationError):
        make_render_material(operation=NodeEditOperation.omit)


def test_render_material_condense_requires_source_refs() -> None:
    with pytest.raises(ValidationError):
        make_render_material(operation=NodeEditOperation.condense)


def test_render_material_condense_valid_with_refs() -> None:
    mat = make_render_material(
        operation=NodeEditOperation.condense,
        source_material_refs=["R0"],
    )
    assert mat.source_material_refs == ["R0"]


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(start=0, end=1, surprise=2)
    with pytest.raises(ValidationError):
        MaterialOmission(material_id="m1", reason="r", surprise=1)


def test_validate_assignment_reruns_validation() -> None:
    span = SourceSpan(start=0, end=5)
    with pytest.raises(ValidationError):
        span.start = 10  # would make end < start
    with pytest.raises(ValidationError):
        span.end = -1  # ge=0


# === 3. Stable-id determinism + volatile-label exclusion ===================


def test_piece_id_format_exact() -> None:
    assert piece_id("src", 123) == "src:000123"
    assert piece_id("src", 0) == "src:000000"


def test_piece_id_pads_to_floor_not_ceiling() -> None:
    # 6-digit minimum, but larger indices are emitted in full.
    assert piece_id("abc", 1234567) == "abc:1234567"


def test_atom_id_is_deterministic() -> None:
    assert atom_id("s", ["p0", "p1"]) == atom_id("s", ["p0", "p1"])


def test_atom_id_depends_on_source_and_piece_refs() -> None:
    assert atom_id("s1", ["p0"]) != atom_id("s2", ["p0"])
    assert atom_id("s", ["p0"]) != atom_id("s", ["p1"])


def test_atom_id_is_piece_ref_order_sensitive() -> None:
    assert atom_id("s", ["p0", "p1"]) != atom_id("s", ["p1", "p0"])


def test_atom_id_lineage_changes_id_but_empty_is_default() -> None:
    without = atom_id("s", ["p0"])
    with_lineage = atom_id("s", ["p0"], ["m1"])
    assert without != with_lineage
    assert atom_id("s", ["p0"], ()) == without


def test_atom_id_no_separator_collision() -> None:
    # Distinct ref sequences that share a naive concatenation must not collide.
    assert atom_id("s", ["ab", "c"]) != atom_id("s", ["a", "bc"])


def test_atom_id_signature_excludes_content_kind() -> None:
    # A volatile label cannot enter the stable id even by accident.
    with pytest.raises(TypeError):
        atom_id("s", ["p0"], content_kind="dialogue")


def test_material_atom_id_independent_of_volatile_labels() -> None:
    src, refs = "src", ["src:000000", "src:000001"]
    a = make_atom(id=atom_id(src, refs), content_kind=ContentKind.dialogue, tags=["x"])
    b = make_atom(
        id=atom_id(src, refs),
        content_kind=ContentKind.assertion,
        tags=["y", "z"],
    )
    assert a.id == b.id == atom_id(src, refs)


def test_name_override_is_optional_target_language_seed() -> None:
    entry = NameOverride(
        aliases=["Terra", "Tina", "ティナ"],
        target_surface="蒂娜",
        output_language="zh-CN",
    )

    assert entry.model_dump() == {
        "aliases": ["Terra", "Tina", "ティナ"],
        "target_surface": "蒂娜",
        "output_language": "zh-CN",
    }
    # The redesigned NameOverride dropped `type` entirely -- identity is
    # source-language surface, not a discovered type -- so passing it is now
    # an unknown-field rejection (DomainModel is extra="forbid"), not a value
    # validation error.
    with pytest.raises(ValidationError):
        NameOverride(
            aliases=["Terra"],
            target_surface="蒂娜",
            output_language="zh-CN",
            type="person",
        )
    with pytest.raises(ValidationError):
        NameOverride(
            aliases=["Terra", "Terra"],
            target_surface="蒂娜",
            output_language="zh-CN",
        )


def test_name_resolution_rule_is_only_relation_and_final_surface() -> None:
    rule = NameResolutionRule(
        rule_id="rule_1",
        aliases=["Terra", "泰拉"],
        stable_name="蒂娜",
        evidence_chunk_ids=["chunk_1", "chunk_2"],
    )

    assert rule.model_dump() == {
        "rule_id": "rule_1",
        "aliases": ["Terra", "泰拉"],
        "stable_name": "蒂娜",
        "evidence_chunk_ids": ["chunk_1", "chunk_2"],
        "metadata": {},
    }
    for update in (
        {"aliases": ["Terra", "Terra"]},
        {"aliases": [""]},
        {"stable_name": " "},
        {"evidence_chunk_ids": ["chunk_1", "chunk_1"]},
    ):
        with pytest.raises(ValidationError):
            NameResolutionRule(**{**rule.model_dump(), **update})


def test_name_resolution_ids_track_relations_and_resolved_plan() -> None:
    rule_a = name_resolution_rule_id("proj", ["Terra", "泰拉"], ["chunk_1"])
    rule_b = name_resolution_rule_id("proj", ["泰拉", "Terra"], ["chunk_1"])
    assert rule_a == rule_b
    assert rule_a != name_resolution_rule_id(
        "proj", ["Terra", "泰拉"], ["chunk_2"]
    )

    base = name_resolution_plan_id(
        "proj",
        "zh-CN",
        "sha256:input",
        ["contract_1"],
        [(rule_a, ["Terra", "泰拉"], "蒂娜")],
    )
    assert base != name_resolution_plan_id(
        "proj",
        "zh-CN",
        "sha256:input",
        ["contract_1"],
        [(rule_a, ["Terra", "泰拉"], "泰拉")],
    )


def test_name_resolution_plan_requires_unique_contract_rule_and_alias_ownership() -> None:
    first = NameResolutionRule(
        rule_id="rule_1",
        aliases=["Terra", "泰拉"],
        stable_name="蒂娜",
        evidence_chunk_ids=["chunk_1"],
    )
    second = NameResolutionRule(
        rule_id="rule_2",
        aliases=["Terra", "Tina"],
        stable_name="蒂娜",
        evidence_chunk_ids=["chunk_2"],
    )
    base = {
        "plan_id": "plan_1",
        "project_id": "proj",
        "output_language": "zh-CN",
        "input_hash": "sha256:x",
        "contract_ids": ["contract_1"],
    }
    with pytest.raises(ValidationError):
        NameResolutionPlan(**{**base, "contract_ids": ["contract_1", "contract_1"]})
    with pytest.raises(ValidationError):
        NameResolutionPlan(**base, rules=[first, first])
    with pytest.raises(ValidationError):
        NameResolutionPlan(**base, rules=[first, second])


def test_candidate_id_deterministic_and_order_sensitive() -> None:
    assert candidate_id("proj", ["m1", "m2"]) == candidate_id("proj", ["m1", "m2"])
    assert candidate_id("proj", ["m1", "m2"]) != candidate_id("proj", ["m2", "m1"])


def test_candidate_id_lineage_changes_id() -> None:
    assert candidate_id("proj", ["m1"]) != candidate_id("proj", ["m1"], ["c1"])


def test_candidate_id_signature_excludes_narrative_function() -> None:
    with pytest.raises(TypeError):
        candidate_id("proj", ["m1"], narrative_function="setup")


def test_candidate_node_id_independent_of_summary_and_function() -> None:
    proj, mats = "proj", ["m1", "m2"]
    n1 = CandidateNode(
        candidate_id=candidate_id(proj, mats),
        source_window="w",
        summary="one summary",
        material_ids=mats,
        narrative_function="setup",
    )
    n2 = CandidateNode(
        candidate_id=candidate_id(proj, mats),
        source_window="w",
        summary="a completely different summary",
        material_ids=mats,
        narrative_function="payoff",
    )
    assert n1.candidate_id == n2.candidate_id


def test_window_id_tracks_grouping_not_membership() -> None:
    assert window_id("s", ["p0", "p1"]) == window_id("s", ["p0", "p1"])
    assert window_id("s", ["p0", "p1"]) != window_id("s", ["p0", "p2"])


def test_new_call_id_is_fresh_per_call() -> None:
    a, b = new_call_id(), new_call_id()
    assert a != b
    assert a.startswith("call_")


# === 4. Alias manifest =====================================================


@pytest.mark.parametrize(
    ("scheme", "number", "expected"),
    [
        (PIECE_ALIASES, 1, "p0001"),
        (MATERIAL_ALIASES, 1, "M001"),
        (CANDIDATE_ALIASES, 1, "C001"),
        (NODE_ALIASES, 1, "N1"),
        (RENDER_ALIASES, 1, "R001"),
    ],
)
def test_preset_formats(scheme, number: int, expected: str) -> None:
    assert scheme.format(number) == expected
    assert scheme.parse(expected) == number


def test_alias_width_is_a_floor() -> None:
    assert PIECE_ALIASES.format(12345) == "p12345"
    assert NODE_ALIASES.format(10) == "N10"


def test_alias_format_rejects_negative() -> None:
    with pytest.raises(ValueError):
        PIECE_ALIASES.format(-1)


def test_alias_format_parse_roundtrip() -> None:
    for scheme in (PIECE_ALIASES, MATERIAL_ALIASES, NODE_ALIASES, RENDER_ALIASES):
        for n in (1, 7, 42, 999):
            assert scheme.parse(scheme.format(n)) == n


def test_alias_parse_rejects_wrong_prefix() -> None:
    with pytest.raises(ValueError):
        PIECE_ALIASES.parse("x0001")


def test_alias_parse_rejects_non_numeric_body() -> None:
    with pytest.raises(ValueError):
        PIECE_ALIASES.parse("pabc")


def test_manifest_mint_is_idempotent_per_global_id() -> None:
    manifest = AliasManifest(MATERIAL_ALIASES)
    first = manifest.mint("g")
    assert manifest.mint("g") == first == "M001"
    assert manifest.mint("h") == "M002"


def test_manifest_number_assigns_sequential_aliases() -> None:
    manifest = AliasManifest.number(PIECE_ALIASES, ["g1", "g2", "g3"])
    assert manifest.items() == [
        ("p0001", "g1"),
        ("p0002", "g2"),
        ("p0003", "g3"),
    ]
    assert manifest.alias_for("g2") == "p0002"
    assert manifest.resolve("p0003") == "g3"


def test_manifest_start_parameter_is_honored() -> None:
    manifest = AliasManifest(PIECE_ALIASES, start=5)
    assert manifest.mint("g") == "p0005"


def test_manifest_resolve_roundtrips_with_mint() -> None:
    manifest = AliasManifest(NODE_ALIASES)
    alias = manifest.mint("global-1")
    assert manifest.resolve(alias) == "global-1"


def test_manifest_resolve_raises_on_unminted_alias() -> None:
    manifest = AliasManifest(PIECE_ALIASES)
    manifest.mint("g")  # p0001 only
    with pytest.raises(KeyError):
        manifest.resolve("p0002")


def test_manifest_resolve_raises_on_malformed_alias() -> None:
    manifest = AliasManifest(PIECE_ALIASES)
    manifest.mint("g")
    with pytest.raises(ValueError):
        manifest.resolve("x0001")
    with pytest.raises(ValueError):
        manifest.resolve("pxyz")


def test_manifest_alias_for_raises_on_unminted_global_id() -> None:
    manifest = AliasManifest(PIECE_ALIASES)
    with pytest.raises(KeyError):
        manifest.alias_for("never-minted")


# === 5. estimate_tokens ====================================================


def test_estimate_tokens_empty_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_returns_non_negative_int() -> None:
    for text in ("", "a", "你好", "   ", "mixed 混合 text"):
        value = estimate_tokens(text)
        assert type(value) is int
        assert value >= 0


def test_estimate_tokens_cjk_heavier_per_char_than_latin() -> None:
    cjk = "你好世界你好世界"  # 8 CJK chars
    latin = "abcdefgh"  # 8 latin chars
    assert len(cjk) == len(latin)
    assert estimate_tokens(cjk) > estimate_tokens(latin)


def test_estimate_tokens_is_monotonic_non_decreasing() -> None:
    assert estimate_tokens("hello") <= estimate_tokens("hello world")
    assert estimate_tokens("你好") <= estimate_tokens("你好世界")
    base = "测试abc"
    assert estimate_tokens(base) <= estimate_tokens(base + "更多内容")


# === public surface ========================================================


def test_public_api_is_importable() -> None:
    for name in domain.__all__:
        assert hasattr(domain, name), f"missing export: {name}"
