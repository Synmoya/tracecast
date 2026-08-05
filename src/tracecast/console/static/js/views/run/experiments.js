// Adjustment workspace separates the normal path-editing flow from isolated
// stage tests, single-chunk tests and result comparison.

import { navigate } from "../../router.js";
import { renderChunklab } from "./chunklab.js";
import { renderPathAdjustment, renderStageTests } from "./derive.js";
import { renderDiff } from "./diff.js";

const TOOLS = {
  path: ["路径调整", renderPathAdjustment],
  stages: ["阶段测试", renderStageTests],
  chunklab: ["单段测试", renderChunklab],
  compare: ["运行对比", renderDiff],
};

const LEGACY_TOOLS = {rerun: "path"};

export async function renderExperiments(container, ctx) {
  const requested = (ctx.rest || "").split("/")[0];
  const canonical = LEGACY_TOOLS[requested] || requested;
  const tool = TOOLS[canonical] ? canonical : "path";
  container.innerHTML = `<section class="workspace-heading"><div>
      <span class="section-kicker">ADJUST</span><h2>调整</h2>
      <p>从正常路径调整到隔离测试和结果对比，按操作目的选择入口。</p></div></section>
    <nav class="subnav">${Object.entries(TOOLS).map(([key, [label]]) =>
      `<a href="#" data-tool="${key}" class="${tool === key ? "active" : ""}">${label}</a>`
    ).join("")}</nav><div id="experiment-tool"></div>`;
  container.querySelectorAll("[data-tool]").forEach((link) =>
    link.addEventListener("click", (event) => {
      event.preventDefault();
      navigate(`/run/${encodeURIComponent(ctx.runId)}/experiments/${link.dataset.tool}`);
    }));
  await TOOLS[tool][1](container.querySelector("#experiment-tool"), {
    ...ctx, rest: null,
  });
}
