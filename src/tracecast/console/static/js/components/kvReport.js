// Curated stage presentation. The Console intentionally does not recursively
// expose arbitrary report keys: operators see decisions and health, while raw
// artifact/report data remains available to IDEs and scripts.

import { esc } from "../dom.js";
import { fmtCompact, fmtNumber, fmtPercent, fmtTokens } from "../format.js";
import {
  PRIMARY_METRICS, ROLE_LABELS, STAGE_META, TREATMENT_LABELS,
} from "../labels.js";

function valueAt(source, path) {
  return path.split(".").reduce((value, key) => value?.[key], source);
}

function formatValue(value, format) {
  if (value === null || value === undefined || value === "") return "—";
  if (format === "percent") return fmtPercent(value);
  if (format === "factor") return `${fmtNumber(value)}×`;
  if (format === "tokens") return fmtTokens(value);
  if (format === "compact") return fmtCompact(value);
  if (format === "status") return value ? "是" : "否";
  if (typeof value === "boolean") return value ? "达标" : "未达标";
  return fmtNumber(value);
}

function metricTone(spec, value) {
  if (spec.format === "status") return value ? "warning" : "success";
  if (!value) return "";
  if (spec.tone === "error") return "danger";
  if (spec.tone === "diagnostic") return "warning";
  return "";
}

function metricNote(stage, path, summary) {
  if (stage === "planning" && path === "summary_retention_ratio") {
    return `${fmtTokens(summary.material_tokens)} → ${fmtTokens(summary.summary_tokens)}`;
  }
  if (stage === "planning" && path === "final_nodes") {
    const target = summary.target_nodes == null ? "—" : fmtNumber(summary.target_nodes);
    return `${fmtNumber(summary.initial_nodes)} → ${fmtNumber(summary.final_nodes)} · 目标 ≤ ${target}`;
  }
  if (stage === "selection" && path === "selected") {
    return `来自 ${fmtNumber(summary.candidates)} 个候选`;
  }
  if (stage === "node_edit" && path === "output_token_total") {
    return `输入 ${fmtTokens(summary.source_token_total)}`;
  }
  if (stage === "packing" && path === "token_distribution.median") {
    return `目标 ${fmtTokens(summary.policy?.target_material_tokens)}`;
  }
  if (stage === "assembly" && path === "assembled_chunks") {
    return `期望 ${fmtNumber(summary.expected_chunks)} 段`;
  }
  return "";
}

function renderMetrics(report) {
  const specs = PRIMARY_METRICS[report.stage] || [];
  return `<div class="metric-grid">${specs.map((spec) => {
    const value = valueAt(report.summary, spec.path);
    const note = metricNote(report.stage, spec.path, report.summary);
    const tone = metricTone(spec, value);
    return `<div class="metric ${tone}">
      <span class="metric-label">${esc(spec.label)}</span>
      <strong class="metric-value">${esc(formatValue(value, spec.format))}</strong>
      ${note ? `<span class="metric-note">${esc(note)}</span>` : ""}
    </div>`;
  }).join("")}</div>`;
}

const badges = (values, labels = {}) => Object.entries(values || {}).map(([key, value]) =>
  `<span class="data-chip"><b>${esc(labels[key] || key)}</b>${esc(fmtNumber(value))}</span>`
).join("") || '<span class="muted">无</span>';

function detailRows(report) {
  const s = report.summary || {};
  switch (report.stage) {
    case "ingestion":
      return [
        ["处理路径", `${fmtNumber(s.pieces)} pieces → ${fmtNumber(s.atoms)} atoms`],
        ["模型输入批次", fmtNumber(s.windows)],
        ["内容类型", badges(s.content_kinds)],
        ["窗口平铺", fmtPercent(s.tiling_coverage)],
      ];
    case "planning":
      return [
        ["规划调用", `初始构图 ${fmtNumber(s.seed_batches)} · 压缩 ${fmtNumber(s.compression_calls)}`],
        ["节点材料密度", `中位 ${fmtNumber(s.node_material_density?.median)} · P90 ${fmtNumber(s.node_material_density?.p90)}`],
        ["材料引用", `${fmtNumber(s.coverage?.referenced_material_count)} / ${fmtNumber(s.coverage?.total_material_count)}`],
        ["目标状态", s.target_met === false ? "未达到压缩目标" : "已达到压缩目标"],
      ];
    case "selection":
      return [
        ["角色", badges(s.roles, ROLE_LABELS)],
        ["处理方式", badges(s.treatments, TREATMENT_LABELS)],
        ["材料数量保留", fmtPercent(s.material_retention)],
        ["来源覆盖", badges(s.sources)],
      ];
    case "node_edit":
      return [
        ["材料操作", badges(s.operations, {keep: "保留", condense: "压缩", omit: "省略"})],
        ["压缩材料", s.condense_compression_ratio == null
          ? "未执行 condense" : `保留 ${fmtPercent(s.condense_compression_ratio)}`],
        ["修复结果", `${fmtNumber(s.repair_recovered)} 成功 · ${fmtNumber(s.repair_exhausted)} 耗尽`],
        ["Lineage", `${fmtPercent(s.condensed_lineage_rate)} 可追溯`],
      ];
    case "packing":
      return [
        ["节点与材料", `${fmtNumber(s.nodes)} nodes · ${fmtNumber(s.materials)} materials`],
        ["每段节点", `中位 ${fmtNumber(s.node_distribution?.median)} · 最大 ${fmtNumber(s.node_distribution?.max)}`],
        ["每段材料", `中位 ${fmtNumber(s.material_distribution?.median)} · 最大 ${fmtNumber(s.material_distribution?.max)}`],
      ];
    case "name_resolution":
      return [
        ["Name Table", `${fmtNumber(s.proposal_entries)} 项提案 · ${fmtNumber(s.accepted_model_rules)} 项接受`],
        ["模型调用", fmtNumber(s.table_calls)],
        ["输出语言", s.output_language || "—"],
      ];
    case "rendering":
      return [
        ["Fact context", `${fmtNumber(s.fact_context_used)} / ${fmtNumber(s.fact_context_cards)} 段使用`],
        ["跨段提示", fmtNumber(s.fact_context_hints)],
        ["长度警告", fmtNumber(s.fact_context_length_warnings)],
        ["预算问题", fmtNumber(s.budget_diagnostics)],
      ];
    case "validation":
      return [
        ["严重度", badges(s.by_level, {error: "错误", warning: "警告", info: "信息"})],
        ["涉及阶段", badges(s.by_stage)],
        ["仍可使用", fmtNumber(s.by_usable?.true)],
      ];
    case "assembly":
      return [
        ["正文字符", fmtNumber(s.script_chars)],
        ["装配状态", s.assembled_with_issues ? "正文已生成，但审计存在问题" : "正文完整装配"],
        ["重复分段", fmtNumber(s.duplicate_chunks)],
      ];
    default:
      return [];
  }
}

function renderDetails(report) {
  const rows = detailRows(report);
  if (!rows.length) return "";
  return `<section class="report-section">
    <div class="section-heading"><div><span class="section-kicker">DETAILS</span>
      <h3>阶段概况</h3></div></div>
    <dl class="summary-list">${rows.map(([label, value]) =>
      `<div><dt>${esc(label)}</dt><dd>${typeof value === "string" && value.includes("data-chip")
        ? value : esc(value)}</dd></div>`).join("")}</dl>
  </section>`;
}

function renderSelectionPath(items) {
  if (!items.length) return "";
  return `<section class="report-section">
    <div class="section-heading"><div><span class="section-kicker">EDITORIAL PATH</span>
      <h3>完整路径大纲</h3></div><span class="section-count">${items.length} 节点</span></div>
    <ol class="outline-list">${items.map((item) => `<li>
      <div class="outline-meta"><span>${esc(ROLE_LABELS[item.role] || item.role || "未分类")}</span>
        <span>${esc(TREATMENT_LABELS[item.treatment] || item.treatment || "默认")}</span></div>
      <p>${esc(item.summary)}</p>
      ${item.directive ? `<blockquote>${esc(item.directive)}</blockquote>` : ""}
    </li>`).join("")}</ol>
  </section>`;
}

function renderPacking(items, summary) {
  if (!items.length) return "";
  const max = Math.max(...items.map((item) => item.estimated_material_tokens || 0), 1);
  return `<section class="report-section">
    <div class="section-heading"><div><span class="section-kicker">CHUNK LOAD</span>
      <h3>正文分段负载</h3></div><span class="section-count">${items.length} 段</span></div>
    <div class="load-list">${items.map((item) => {
      const overloaded = (item.overloaded_caps || []).length > 0;
      return `<div class="load-row ${overloaded ? "danger" : ""}">
        <span class="load-index">${Number(item.order_index) + 1}</span>
        <div class="load-track"><i style="width:${Math.round((item.estimated_material_tokens || 0) / max * 100)}%"></i></div>
        <strong>${esc(fmtTokens(item.estimated_material_tokens))}</strong>
        <span>${esc(`${item.nodes} nodes · ${item.materials} materials`)}</span>
      </div>`;
    }).join("")}</div>
    <p class="section-note">目标 ${esc(fmtTokens(summary.policy?.target_material_tokens))}，硬上限 ${esc(fmtTokens(summary.policy?.max_material_tokens))}。</p>
  </section>`;
}

function renderNames(items) {
  if (!items.length) return "";
  return `<section class="report-section">
    <div class="section-heading"><div><span class="section-kicker">NAME MAP</span>
      <h3>采用的专名统一</h3></div><span class="section-count">${items.length} 条</span></div>
    <div class="name-map">${items.map((item) => `<div>
      <span>${item.aliases.map(esc).join(" · ")}</span><b>→</b><strong>${esc(item.stable_name)}</strong>
    </div>`).join("")}</div>
  </section>`;
}

function diagnosticText(item) {
  return item.message || item.error || item.kind || String(item);
}

function diagnosticItems(report) {
  if (Array.isArray(report.diagnostics)) return report.diagnostics;
  if (report.stage === "assembly") {
    const items = [];
    const missing = report.diagnostics?.missing_chunk_ids || [];
    const duplicate = report.diagnostics?.duplicate_chunk_ids || [];
    if (missing.length) items.push({kind: "missing_chunks", level: "error", message: missing.join(", ")});
    if (duplicate.length) items.push({kind: "duplicate_chunks", level: "error", message: duplicate.join(", ")});
    return items;
  }
  return Object.entries(report.diagnostics || {}).flatMap(([kind, value]) => {
    if (value === null || value === undefined || value === false || value === "") return [];
    if (Array.isArray(value) && !value.length) return [];
    return [{kind, message: Array.isArray(value) ? value.join(", ") : String(value)}];
  });
}

function renderDiagnostics(report) {
  const diagnostics = diagnosticItems(report);
  if (!diagnostics.length) {
    return `<section class="report-section report-clear">
      <span class="status-icon">✓</span><div><h3>没有阶段异常</h3>
      <p>本阶段未记录需要人工处理的诊断。</p></div></section>`;
  }
  return `<section class="report-section">
    <div class="section-heading"><div><span class="section-kicker">ATTACHED REPORT</span>
      <h3>附带报告</h3></div><span class="section-count warning">${diagnostics.length} 项</span></div>
    <div class="issue-list">${diagnostics.map((item) => {
      const level = item.level || (item.kind?.includes("error") ? "error" : "warning");
      return `<article class="issue ${esc(level)}"><span>${esc(level)}</span>
        <div><strong>${esc(item.kind || "diagnostic")}</strong><p>${esc(diagnosticText(item))}</p></div>
      </article>`;
    }).join("")}</div>
  </section>`;
}

function renderStageContent(report) {
  const items = report.samples || [];
  if (report.stage === "selection") return renderSelectionPath(items);
  if (report.stage === "packing") return renderPacking(items, report.summary);
  if (report.stage === "name_resolution") return renderNames(items);
  return "";
}

export function renderReport(report) {
  if (!report.available) {
    return `<div class="empty-state-large"><span>○</span><h3>阶段尚不可用</h3>
      <p>当前运行的 lineage 还没有生成该阶段结果。</p></div>`;
  }
  return renderMetrics(report)
    + renderDetails(report)
    + renderStageContent(report)
    + renderDiagnostics(report);
}

export function stageHealth(report) {
  if (!report?.available) return "pending";
  const diagnostics = diagnosticItems(report);
  const hasError = diagnostics.some((item) => item.level === "error");
  if (hasError) return "error";
  if (report.stage === "validation" && (report.summary?.issues || 0) > 0) return "warning";
  if (report.stage === "assembly" && report.summary?.assembled_with_issues) return "warning";
  return diagnostics.length ? "warning" : "complete";
}

export function stageTitle(stage) {
  return STAGE_META[stage]?.label || stage;
}
