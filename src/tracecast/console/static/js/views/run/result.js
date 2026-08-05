// Result workspace: the assembled script is the primary run output. Raw
// artifacts stay off-screen; stage and validation context remain one click away.

import { copyText, esc, setMsg } from "../../dom.js";
import { fmtNumber, fmtTokens } from "../../format.js";
import { navigate } from "../../router.js";
import { getChunks, getScript, getStageReports } from "./data.js";

function saveScript(runId, text) {
  const blob = new Blob([text], {type: "text/markdown;charset=utf-8"});
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href;
  link.download = `${runId}.md`;
  link.click();
  URL.revokeObjectURL(href);
}

export async function renderResult(container, ctx) {
  let script;
  try {
    script = await getScript(ctx.runId);
  } catch (error) {
    container.innerHTML = `<div class="empty-state-large result-empty">
      <span>◌</span><h2>这个运行还没有成稿</h2>
      <p>${esc(ctx.manifest?.error || "流水线尚未完成到成稿装配阶段。")}</p>
      <div class="empty-actions"><button class="act" data-go="pipeline">查看流水线</button>
        <button class="act ghost" data-go="experiments">打开实验</button></div></div>`;
    container.querySelector('[data-go="pipeline"]').addEventListener("click", () =>
      navigate(`/run/${encodeURIComponent(ctx.runId)}/pipeline`));
    container.querySelector('[data-go="experiments"]').addEventListener("click", () =>
      navigate(`/run/${encodeURIComponent(ctx.runId)}/experiments`));
    return;
  }

  const [chunks, reports] = await Promise.all([
    getChunks(ctx.runId), getStageReports(ctx.runId),
  ]);
  const assembly = reports.assembly?.summary || {};
  const validation = reports.validation?.summary || {};
  const issueCount = validation.issues || 0;
  const text = script.text || "";

  container.innerHTML = `
    <section class="result-hero">
      <div><span class="section-kicker">FINAL SCRIPT</span><h2>成稿</h2>
        <p>${issueCount
          ? `正文已装配，另有 ${esc(fmtNumber(issueCount))} 项审计记录。`
          : "正文已完整装配，没有记录审计问题。"}</p></div>
      <div class="result-actions"><button class="act" data-result="copy">复制全文</button>
        <button class="act ghost" data-result="download">下载 .md</button></div>
    </section>
    <div class="result-stats">
      <div><strong>${esc(fmtTokens(assembly.script_tokens))}</strong><span>正文体量</span></div>
      <div><strong>${esc(fmtNumber(assembly.script_chars))}</strong><span>字符</span></div>
      <div><strong>${esc(fmtNumber(chunks.length))}</strong><span>分段</span></div>
      <button class="${issueCount ? "warning" : "clear"}" data-result="validation">
        <strong>${esc(fmtNumber(issueCount))}</strong><span>审计项</span></button>
    </div>
    <article class="script-viewer" aria-label="最终成稿">
      <div class="measure">${esc(text)}</div></article>
    <div class="msg" data-result="msg"></div>`;

  container.querySelector('[data-result="copy"]').addEventListener("click", async () => {
    try {
      await copyText(text);
      setMsg(container.querySelector('[data-result="msg"]'), "已复制完整正文。", "ok");
    } catch (error) {
      setMsg(container.querySelector('[data-result="msg"]'), error.message, "err");
    }
  });
  container.querySelector('[data-result="download"]').addEventListener("click", () =>
    saveScript(ctx.runId, text));
  container.querySelector('[data-result="validation"]').addEventListener("click", () =>
    navigate(`/run/${encodeURIComponent(ctx.runId)}/pipeline/validation`));
}
