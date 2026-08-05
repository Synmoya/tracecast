// Human-facing Console vocabulary. Stable ids, artifact paths and raw field
// names stay in the audit layer; daily navigation is intentionally semantic.

export const STAGES = [
  "ingestion", "planning", "selection", "node_edit",
  "packing", "name_resolution", "rendering", "validation", "assembly",
];

export const STAGE_META = {
  ingestion: {
    label: "材料整理", english: "Ingestion",
    description: "把原始来源切成可追溯的材料原子。",
    ready: ["ingestion/material_atoms.jsonl"],
  },
  planning: {
    label: "候选规划", english: "Planning",
    description: "把全部材料组织成高召回的候选节点。",
    ready: ["planning/candidate_graph.json"],
  },
  selection: {
    label: "路径选择", english: "Selection",
    description: "选择本期叙事路径并确定节点顺序。",
    ready: ["selection/selection_plan.json"],
  },
  node_edit: {
    label: "材料编辑", english: "Node Edit",
    description: "在已选节点内保留、压缩或省略材料。",
    ready: ["node_edit/node_edit_plan.json"],
  },
  packing: {
    label: "正文打包", english: "Packing",
    description: "按正文负载把节点确定性装入 chunks。",
    ready: ["packing/chunk_plan.json"],
  },
  name_resolution: {
    label: "专名统一", english: "Name Resolution",
    description: "统一最终正文输入中的多种专名写法。",
    ready: [
      "name_resolution/name_resolution_plan.json",
      "name_resolution/name_resolution_report.jsonl",
    ],
  },
  rendering: {
    label: "正文生成", english: "Rendering",
    description: "按 chunk 生成正文并记录调用状态。",
    ready: ["rendering/chunk_artifacts.jsonl"],
  },
  validation: {
    label: "质量审计", english: "Validation",
    description: "汇总可追溯性、格式与文本问题。",
    ready: ["validation/audit_report.jsonl"],
  },
  assembly: {
    label: "成稿装配", english: "Assembly",
    description: "按顺序拼接正文并生成最终脚本。",
    ready: ["assembly/assembly_report.json"],
  },
};

export const RUN_KIND_LABELS = {
  full: "完整运行",
  ingestion: "材料整理",
  selection: "重选路径",
  node_edit: "材料编辑",
  render: "生成正文",
  chunklab: "单段测试",
};

export const ROLE_LABELS = {
  core: "核心", bridge: "衔接", context: "背景", color: "质感",
};

export const TREATMENT_LABELS = {
  quote: "引用", paraphrase: "转述", summarize: "概述",
};

// Only fields listed here may become headline cards. This prevents backend
// metadata such as compression.* keys from leaking into the visual surface.
export const PRIMARY_METRICS = {
  ingestion: [
    {path: "sources", label: "来源"},
    {path: "material_tokens", label: "材料体量", format: "tokens"},
    {path: "atoms", label: "材料原子"},
    {path: "aggregation_factor", label: "切片聚合", format: "factor"},
  ],
  planning: [
    {path: "summary_retention_ratio", label: "SUM 体量", format: "percent"},
    {path: "summary_compression_factor", label: "压缩倍数", format: "factor"},
    {path: "final_nodes", label: "最终节点"},
    {path: "coverage.ratio", label: "材料覆盖", format: "percent"},
  ],
  selection: [
    {path: "selected", label: "入选节点"},
    {path: "selection_rate", label: "节点保留", format: "percent"},
    {path: "token_retention", label: "材料 token 保留", format: "percent"},
    {path: "diagnostic_count", label: "需要留意", tone: "diagnostic"},
  ],
  node_edit: [
    {path: "output_token_total", label: "输出材料", format: "tokens"},
    {path: "token_retention", label: "token 保留", format: "percent"},
    {path: "repair_nodes", label: "进入修复", tone: "diagnostic"},
    {path: "repair_exhausted", label: "修复耗尽", tone: "error"},
  ],
  packing: [
    {path: "chunks", label: "正文分段"},
    {path: "token_distribution.median", label: "每段中位", format: "tokens"},
    {path: "token_distribution.max", label: "最大负载", format: "tokens"},
    {path: "overloaded_chunks", label: "超载分段", tone: "error"},
  ],
  name_resolution: [
    {path: "rules", label: "统一规则"},
    {path: "aliases", label: "覆盖写法"},
    {path: "protected_surfaces", label: "保护写法"},
    {path: "diagnostic_count", label: "需要留意", tone: "diagnostic"},
  ],
  rendering: [
    {path: "chunks", label: "已生成分段"},
    {path: "body_tokens", label: "正文体量", format: "tokens"},
    {path: "body_chars", label: "正文字符", format: "compact"},
    {path: "diagnostic_count", label: "需要留意", tone: "diagnostic"},
  ],
  validation: [
    {path: "issues", label: "审计项", tone: "diagnostic"},
    {path: "by_level.error", label: "错误", tone: "error"},
    {path: "by_level.warning", label: "警告", tone: "warning"},
    {path: "traceability_rate", label: "可追溯", format: "percent"},
  ],
  assembly: [
    {path: "script_tokens", label: "成稿体量", format: "tokens"},
    {path: "assembled_chunks", label: "已装配分段"},
    {path: "missing_chunks", label: "缺失分段", tone: "error"},
    {path: "assembled_with_issues", label: "带问题装配", format: "status"},
  ],
};
