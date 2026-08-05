// Compact audit identity. Raw artifact inventories and manifest JSON belong in
// the filesystem/IDE; this view keeps only lineage and effective run context.

import { copyText, esc, setMsg } from "../../dom.js";
import { fmtTimeFull } from "../../format.js";
import { lineageRows, runDisplay } from "../../runIdentity.js";

function leafCount(value) {
  if (!value || typeof value !== "object") return 0;
  return Object.values(value).reduce((count, child) =>
    count + (child && typeof child === "object" ? leafCount(child) : 1), 0);
}

export async function renderOverview(container, ctx) {
  const manifest = ctx.manifest;
  const lineage = lineageRows(ctx.runId).reverse();
  const host = manifest?.effective?.host;
  const overrides = manifest?.request?.config_overrides;

  container.innerHTML = `
    <div class="audit-layout">
      <section class="panel audit-card"><span class="section-kicker">IDENTITY</span>
        <h2>运行标识</h2>
        <dl class="audit-fields">
          <div><dt>完整 run ID</dt><dd><code>${esc(ctx.runId)}</code>
            <button class="mini" data-copy="run">复制</button></dd></div>
          <div><dt>创建时间</dt><dd>${esc(fmtTimeFull(manifest?.created_at) || "—")}</dd></div>
          <div><dt>运行类型</dt><dd>${esc(manifest?.kind || "历史运行")}</dd></div>
          <div><dt>Job</dt><dd><code>${esc(manifest?.job_id || "—")}</code></dd></div>
        </dl><div class="msg" data-copy-msg></div>
      </section>
      <section class="panel audit-card"><span class="section-kicker">EFFECTIVE CONTEXT</span>
        <h2>本次上下文</h2>
        <dl class="audit-fields">
          <div><dt>Host</dt><dd>${esc(host?.profile?.display_name || host?.host_id || "—")}</dd></div>
          <div><dt>输出语言</dt><dd>${esc(manifest?.effective?.output_language || "—")}</dd></div>
          <div><dt>配置覆盖</dt><dd>${leafCount(overrides)} 项</dd></div>
          <div><dt>输入来源</dt><dd>${Object.keys(manifest?.artifact_sources || {}).length} 项 lineage 解析</dd></div>
        </dl>
      </section>
    </div>
    <section class="panel lineage-panel"><div class="section-heading"><div>
      <span class="section-kicker">LINEAGE</span><h2>运行路径</h2></div>
      <span class="section-count">${lineage.length} 层</span></div>
      <ol class="lineage-list">${lineage.map((run, index) => {
        const display = runDisplay(run);
        return `<li class="${run.run_id === ctx.runId ? "current" : ""}">
          <span>${index + 1}</span><div><strong>${esc(display.title)}</strong>
          <small>${esc(display.operation)} · ${esc(display.fullTime || display.time)}</small></div>
          <code title="${esc(run.run_id)}">${esc(run.run_id)}</code></li>`;
      }).join("")}</ol>
    </section>`;

  container.querySelector('[data-copy="run"]').addEventListener("click", async () => {
    try {
      await copyText(ctx.runId);
      setMsg(container.querySelector("[data-copy-msg]"), "已复制 run ID。", "ok");
    } catch (error) {
      setMsg(container.querySelector("[data-copy-msg]"), error.message, "err");
    }
  });
}
