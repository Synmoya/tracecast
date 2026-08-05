// Pipeline workspace: the nine stages are the primary structure. Each stage
// uses a curated presentation with its report attached beneath the outcome.

import { esc } from "../../dom.js";
import { STAGE_META, STAGES } from "../../labels.js";
import { navigate } from "../../router.js";
import { renderReport, stageHealth } from "../../components/kvReport.js";
import { getStageReports } from "./data.js";

function defaultStage(reports) {
  const noteworthy = [...STAGES].reverse().find((stage) =>
    ["error", "warning"].includes(stageHealth(reports[stage])));
  return noteworthy || [...STAGES].reverse().find((stage) => reports[stage]?.available)
    || STAGES[0];
}

const healthLabel = {
  complete: "完成", warning: "需留意", error: "有错误", pending: "未生成",
};

function stageRail(reports, active) {
  return `<nav class="stage-rail" aria-label="流水线阶段">${STAGES.map((stage, index) => {
    const meta = STAGE_META[stage];
    const health = stageHealth(reports[stage]);
    return `<a class="stage-step ${health} ${stage === active ? "active" : ""}"
      href="#" data-stage="${stage}" aria-current="${stage === active ? "step" : "false"}">
      <span class="stage-index">${index + 1}</span><span class="stage-copy">
        <strong>${esc(meta.label)}</strong><small>${esc(healthLabel[health])}</small>
      </span></a>`;
  }).join("")}</nav>`;
}

export async function renderReports(container, ctx) {
  const reports = await getStageReports(ctx.runId);
  const requested = STAGES.includes(ctx.rest) ? ctx.rest : null;
  const stage = requested || defaultStage(reports);
  const report = reports[stage];
  const meta = STAGE_META[stage];
  const origins = new Set(Object.values(report.origins || {}));
  const inherited = report.available && (origins.size > 1 || !origins.has(ctx.runId));

  container.innerHTML = `
    <section class="pipeline-overview">
      <div class="workspace-heading"><div><span class="section-kicker">PIPELINE</span>
        <h2>流水线</h2><p>从材料进入到最终成稿，逐层查看结果与附带报告。</p></div></div>
      ${stageRail(reports, stage)}
    </section>
    <section class="stage-workspace">
      <header class="stage-header"><div>
        <span class="section-kicker">${esc(meta.english)}</span>
        <h2>${esc(meta.label)}</h2><p>${esc(meta.description)}</p>
      </div><div class="stage-status ${stageHealth(report)}">
        ${esc(healthLabel[stageHealth(report)])}${inherited ? " · 沿 lineage 继承" : ""}
      </div></header>
      <div id="stage-report">${renderReport(report)}</div>
    </section>`;

  container.querySelectorAll("[data-stage]").forEach((link) =>
    link.addEventListener("click", (event) => {
      event.preventDefault();
      navigate(`/run/${encodeURIComponent(ctx.runId)}/pipeline/${link.dataset.stage}`);
    }));
}
