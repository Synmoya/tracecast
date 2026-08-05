// Diagnostics workspace: contextual trace/call tooling plus compact run audit
// identity. Raw artifacts are deliberately not a Console destination.

import { navigate } from "../../router.js";
import { renderLlm } from "./llm.js";
import { renderOverview } from "./overview.js";
import { renderTrace } from "./trace.js";

const TOOLS = {
  trace: ["阶段追踪", renderTrace],
  calls: ["模型调用", renderLlm],
  info: ["运行信息", renderOverview],
};

export async function renderDiagnostics(container, ctx) {
  const parts = (ctx.rest || "").split("/").filter(Boolean);
  const tool = TOOLS[parts[0]] ? parts[0] : "trace";
  container.innerHTML = `<section class="workspace-heading"><div>
      <span class="section-kicker">DIAGNOSTICS</span><h2>诊断</h2>
      <p>按阶段追查输入、模型调用和运行 lineage。</p></div></section>
    <nav class="subnav">${Object.entries(TOOLS).map(([key, [label]]) =>
      `<a href="#" data-tool="${key}" class="${tool === key ? "active" : ""}">${label}</a>`
    ).join("")}</nav><div id="diagnostic-tool"></div>`;
  container.querySelectorAll("[data-tool]").forEach((link) =>
    link.addEventListener("click", (event) => {
      event.preventDefault();
      navigate(`/run/${encodeURIComponent(ctx.runId)}/diagnostics/${link.dataset.tool}`);
    }));
  await TOOLS[tool][1](container.querySelector("#diagnostic-tool"), {
    ...ctx, rest: parts.slice(1).join("/") || null,
  });
}
