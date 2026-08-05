"""Declarative schema for the reusable Console configuration editor."""

from __future__ import annotations

from typing import Any

from tracecast.name_resolution import DEFAULT_TABLE_MAX_INPUT_TOKENS
from tracecast.packing import DEFAULT_CHUNK_POLICY
from tracecast.planning.policy import (
    DEFAULT_MAX_FINAL_NODE_COUNT,
    DEFAULT_TARGET_MATERIAL_TOKENS_PER_NODE,
)
from tracecast.rendering import FACT_CONTEXT_ROUTE
from tracecast.storage import RuntimeConfig

FULL = "full_root"
INGEST = "ingestion_root"
SELECTION = "derive_selection"
NODE_EDIT = "derive_node_edit"
RENDER = "derive_render"
CHUNKLAB = "chunk_lab"
HOST_BUILD = "host_build"

SURFACES = (FULL, INGEST, SELECTION, NODE_EDIT, RENDER, CHUNKLAB, HOST_BUILD)


def editor_schema(config: RuntimeConfig) -> dict[str, Any]:
    """Return current values plus field types, constraints and surface routing."""
    budget_fields = [
        _budget(
            "Ingestion",
            "ingestion",
            "source_piece_max_tokens",
            "int",
            1,
            "单个 SourcePiece 的最大 token。",
            [FULL, INGEST],
        ),
        _budget(
            "Ingestion",
            "ingestion",
            "atom_window_max_input_tokens",
            "int",
            1,
            "Atom window 的输入预算。",
            [FULL, INGEST],
        ),
        _budget(
            "Ingestion",
            "ingestion",
            "pieces_per_call_soft_limit",
            "int",
            1,
            "每个 atom 调用的 piece 软上限。",
            [FULL, INGEST],
        ),
        _budget(
            "Planning",
            "planning",
            "node_window_max_input_tokens",
            "int",
            1,
            "Planning seed window 输入预算。",
            [FULL],
        ),
        _budget(
            "Planning",
            "planning",
            "target_material_tokens_per_node",
            "int",
            1,
            "每个最终 node 对应的原始材料 token 目标，用于计算默认压缩配额。",
            [FULL],
            default=DEFAULT_TARGET_MATERIAL_TOKENS_PER_NODE,
        ),
        _budget(
            "Planning",
            "planning",
            "max_final_node_count",
            "int",
            1,
            "Selection-facing 最终 node 目标配额的上限。",
            [FULL],
            default=DEFAULT_MAX_FINAL_NODE_COUNT,
        ),
        _budget(
            "Planning",
            "planning",
            "target_final_node_count",
            "int",
            1,
            "可选单次显式目标；替代 token 比例目标，但仍受目标配额上限约束。",
            [FULL],
            optional=True,
        ),
        _budget(
            "Planning",
            "planning",
            "max_compression_rounds",
            "int",
            1,
            "Planning 最大压缩轮数。",
            [FULL],
        ),
        _budget(
            "Planning",
            "planning",
            "node_compress_max_input_tokens",
            "int",
            1,
            "压缩调用输入预算；为空时继承 seed window。",
            [FULL],
            optional=True,
        ),
        _budget(
            "Selection",
            "planning",
            "selection_max_input_tokens",
            "int",
            1,
            "Selector 整图输入预算。",
            [FULL, SELECTION],
        ),
        _budget(
            "Selection",
            "selection",
            "target_output_kchars",
            "float",
            0.1,
            "可选目标正文千字数，仅作 selector 软提示。",
            [SELECTION],
            optional=True,
        ),
        _budget(
            "Node Edit",
            "node_edit",
            "batch_max_input_tokens",
            "int",
            1,
            "K/C/O batch 输入预算。",
            [FULL, NODE_EDIT],
        ),
        _budget(
            "Node Edit",
            "node_edit",
            "max_nodes_per_batch",
            "int",
            1,
            "每个 K/C/O batch 最大 node 数。",
            [FULL, NODE_EDIT],
        ),
        _budget(
            "Packing",
            "rendering",
            "target_material_tokens",
            "int",
            1,
            "每 chunk 目标材料 token。",
            [FULL, RENDER],
        ),
        _budget(
            "Packing",
            "rendering",
            "max_material_tokens",
            "int",
            1,
            "每 chunk 最大材料 token。",
            [FULL, RENDER],
        ),
        _budget(
            "Packing",
            "rendering",
            "max_materials_per_chunk",
            "int",
            1,
            "每 chunk 最大材料数。",
            [FULL, RENDER],
        ),
        _budget(
            "Packing",
            "rendering",
            "max_nodes_per_chunk",
            "int",
            1,
            "每 chunk 最大 node 数。",
            [FULL, RENDER],
            optional=True,
            default=DEFAULT_CHUNK_POLICY.max_nodes_per_chunk,
        ),
        _budget(
            "Packing",
            "rendering",
            "prefer_node_boundary",
            "bool",
            None,
            "v1 必须按 node 边界打包。",
            [FULL, RENDER],
            readonly=True,
            default=True,
        ),
        _budget(
            "Name Resolution",
            "name_resolution",
            "table_max_input_tokens",
            "int",
            1,
            "单次完整 selected-material Name Table 输入预算；超限时只编译手工表。",
            [FULL, RENDER],
            default=DEFAULT_TABLE_MAX_INPUT_TOKENS,
        ),
        _budget(
            "Name Resolution",
            "name_resolution",
            "max_rules",
            "int",
            1,
            "单次 Name Table 最多接受的 replacement/protection 项数。",
            [FULL, RENDER],
            default=256,
        ),
        _budget(
            "Rendering",
            "rendering",
            "render_max_input_tokens",
            "int",
            1,
            "正文 renderer 输入预算。",
            [FULL, RENDER, CHUNKLAB],
        ),
        _budget(
            "Rendering",
            "rendering",
            "fact_context_ratio",
            "float",
            0.001,
            "fact context 占材料 token 的比例。",
            [FULL, RENDER],
            maximum=1,
        ),
        _budget(
            "Rendering",
            "rendering",
            "fact_context_min_tokens",
            "int",
            1,
            "fact context 最小 token。",
            [FULL, RENDER],
        ),
        _budget(
            "Rendering",
            "rendering",
            "fact_context_max_tokens",
            "int",
            1,
            "fact context 最大 token。",
            [FULL, RENDER],
        ),
    ]
    for field in budget_fields:
        bucket = config.stage_budgets.get(field["bucket"], {})
        field["value"] = bucket.get(field["key"], field.get("default"))

    route_surfaces = {
        "atom_boundary_dsl": [FULL, INGEST],
        "planner_graph_dsl": [FULL],
        "planner_node_compress_dsl": [FULL],
        "selector_path_dsl": [FULL, SELECTION],
        "selector_directive_dsl": [FULL, SELECTION],
        "node_edit": [FULL, NODE_EDIT],
        "node_edit_block_repair": [FULL, NODE_EDIT],
        "name_table": [FULL, RENDER],
        "render_fact_context_dsl": [FULL, RENDER],
        "render_chunk": [FULL, RENDER, CHUNKLAB],
        "host_profile_distill": [HOST_BUILD],
        "host_style_example_reverse": [HOST_BUILD],
        # Intake is a separate pre-pipeline surface and is not run by root/derive.
        "source_profile_probe": [],
    }
    route_fields = [
        {"key": "model", "type": "text", "help": "模型名称。"},
        {
            "key": "extra_body",
            "type": "json",
            "help": "网关特有的额外 JSON 字段；核心请求字段不可覆盖。",
        },
        {"key": "temperature", "type": "float", "min": 0, "max": 2, "help": "采样温度。"},
        {"key": "max_output_tokens", "type": "int", "min": 1, "help": "最大输出 token。"},
        {
            "key": "timeout_seconds",
            "type": "int",
            "min": 1,
            "help": "HTTP connect/read/write/pool 各阶段超时秒数，不是总时限。",
        },
        {"key": "max_concurrency", "type": "int", "min": 1, "help": "route 最大并发。"},
        {
            "key": "transport_max_attempts",
            "type": "int",
            "min": 1,
            "help": "一次模型调用的 transport 总尝试次数。",
        },
        {
            "key": "transport_backoff_base_seconds",
            "type": "float",
            "min": 0.001,
            "help": "transport retry 指数退避起始秒数。",
        },
        {
            "key": "transport_backoff_max_seconds",
            "type": "float",
            "min": 0.001,
            "help": "transport retry 与 Retry-After 的等待上限秒数。",
        },
        {
            "key": "transport_jitter_ratio",
            "type": "float",
            "min": 0,
            "max": 1,
            "help": "指数退避 jitter 比例，范围 0 到 1。",
        },
        {"key": "system_prompt", "type": "text", "help": "system prompt 路径。"},
        {"key": "user_prompt", "type": "text", "help": "user prompt 路径。"},
        {"key": "repair_prompt", "type": "text", "help": "repair prompt 路径。"},
    ]
    routes: list[dict[str, Any]] = []
    for name, route in config.routes.items():
        # Unknown routes remain visible in the global editor (surface=None), but
        # must never leak into a purpose-specific run form.
        surfaces = route_surfaces.get(name, [])
        values = route.model_dump(exclude_none=False)
        fields = [dict(field, value=values.get(field["key"])) for field in route_fields]
        if name == FACT_CONTEXT_ROUTE:
            temperature = next(field for field in fields if field["key"] == "temperature")
            temperature.update(
                {
                    "min": 0,
                    "max": 0,
                    "readonly": True,
                    "help": "Fact Context 稀疏 DSL 固定使用 temperature=0。",
                }
            )
        routes.append(
            {
                "name": name,
                "stage": route.stage,
                "surfaces": surfaces,
                "fields": fields,
                "readonly": {
                    "provider": route.provider,
                    "cost_profile": route.cost_profile,
                },
            }
        )
    return {
        "surfaces": list(SURFACES),
        "budget_fields": budget_fields,
        "routes": routes,
        "merge_order": "extra_json_then_visual_fields",
        "submit_semantics": "changed_from_runtime_defaults_only",
        "relationships": [
            "target_material_tokens <= max_material_tokens",
            "fact_context_min_tokens <= fact_context_max_tokens",
            "0 < fact_context_ratio <= 1",
            "prefer_node_boundary = true",
            "transport_backoff_base_seconds <= transport_backoff_max_seconds",
        ],
    }


def _budget(
    group: str,
    bucket: str,
    key: str,
    field_type: str,
    minimum: float | None,
    help_text: str,
    surfaces: list[str],
    *,
    maximum: float | None = None,
    optional: bool = False,
    readonly: bool = False,
    default: Any = None,
) -> dict[str, Any]:
    return {
        "id": f"stage_budgets.{bucket}.{key}",
        "group": group,
        "bucket": bucket,
        "key": key,
        "override_path": f"stage_budgets.{bucket}.{key}",
        "type": field_type,
        "min": minimum,
        "max": maximum,
        "optional": optional,
        "readonly": readonly,
        "help": help_text,
        "surfaces": surfaces,
        "default": default,
    }
