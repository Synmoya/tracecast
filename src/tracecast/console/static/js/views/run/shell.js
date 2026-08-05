// Run workspace shell. Stable ids move to Diagnostics > Run info; the daily
// surface is organized as Result / Pipeline / Adjust / Diagnostics.

import { $, esc } from "../../dom.js";
import { navigate } from "../../router.js";
import { runDisplay } from "../../runIdentity.js";
import { runById } from "../../state.js";
import { getInventory } from "./data.js";
import { renderDiagnostics } from "./diagnostics.js";
import { renderExperiments } from "./experiments.js";
import { renderReports } from "./reports.js";
import { renderResult } from "./result.js";

const SUBVIEWS = [
  ["result", "结果", renderResult],
  ["pipeline", "流程", renderReports],
  ["experiments", "调整", renderExperiments],
  ["diagnostics", "诊断", renderDiagnostics],
];

const LEGACY_ROUTES = {
  reports: ["pipeline", (rest) => rest],
  overview: ["diagnostics", () => "info"],
  artifacts: ["pipeline", () => ""],
  llm: ["diagnostics", (rest) => `calls${rest ? `/${rest}` : ""}`],
  trace: ["diagnostics", () => "trace"],
  audit: ["pipeline", () => "ingestion"],
  derive: ["experiments", () => "path"],
  chunklab: ["experiments", () => "chunklab"],
  diff: ["experiments", () => "compare"],
};

const statusLabel = {
  pending: "等待中", running: "运行中", complete: "完成",
  failed: "失败", cancelled: "已取消",
};

function hasScript(inventory) {
  return inventory.slots.some((slot) =>
    slot.relpath === "assembly/script.md" && slot.available);
}

export async function renderRunShell(runId, sub = null, rest = undefined) {
  if (sub && LEGACY_ROUTES[sub]) {
    const [canonical, mapRest] = LEGACY_ROUTES[sub];
    const canonicalRest = mapRest(rest);
    navigate(`/run/${encodeURIComponent(runId)}/${canonical}`
      + (canonicalRest ? `/${canonicalRest}` : ""));
    return;
  }

  const inventory = await getInventory(runId);
  const selectedSub = sub || (hasScript(inventory) ? "result" : "pipeline");
  const target = SUBVIEWS.find(([key]) => key === selectedSub);
  if (!target) {
    navigate(`/run/${encodeURIComponent(runId)}`);
    return;
  }
  const manifest = inventory.console_manifest;
  const run = runById(runId) || {run_id: runId, modified_at: manifest?.created_at, console_manifest: manifest};
  const display = runDisplay(run);
  const base = manifest?.base_run_id;
  const baseRun = base ? runById(base) : null;
  const baseDisplay = baseRun ? runDisplay(baseRun) : null;
  const baseLabel = baseDisplay && baseDisplay.title === display.project
    ? baseDisplay.operation : baseDisplay?.title;
  const tabs = SUBVIEWS.map(([key, label]) =>
    `<a class="run-tab ${key === selectedSub ? "active" : ""}"
      href="#/run/${encodeURIComponent(runId)}/${key}">${esc(label)}</a>`).join("");
  const status = manifest?.status;

  $("view").innerHTML = `
    <header class="run-head">
      <div class="run-breadcrumb"><a href="#/run/${encodeURIComponent(runId)}/diagnostics/info">
        ${esc(display.project || "运行")}</a>${baseDisplay
          ? `<span>›</span><a href="#/run/${encodeURIComponent(base)}/result">${esc(baseLabel)}</a>`
          : ""}</div>
      <div class="run-title-row"><div><h1>${esc(display.title)}</h1>
        <p>${esc(display.operation)} · ${esc(display.fullTime || display.time || "时间未知")}</p></div>
        <div class="run-state ${esc(status || "legacy")}"><i></i>${esc(statusLabel[status] || "历史运行")}</div>
      </div>
      ${manifest?.error ? `<div class="banner danger">${esc(manifest.error)}</div>` : ""}
      <nav class="run-tabs">${tabs}</nav>
    </header>
    <div id="run-sub" class="run-workspace"></div>`;
  await target[2]($("run-sub"), {runId, inventory, manifest, rest});
}
