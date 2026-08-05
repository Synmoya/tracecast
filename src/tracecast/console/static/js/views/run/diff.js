// 对比（run 上下文）: run A 固定为当前 run,run B 默认取 base_run_id。
// 三种可读 diff: selection 路径 / chunk 正文 / renderer 注入块。
// 原始 llm/raw 请求留给 IDE 与模型调用诊断，不占用运行对比界面。

import { api } from "../../api.js";
import { esc } from "../../dom.js";
import {
  renderBlocksDiff, renderSelectionDiff, renderTextDiff,
} from "../../components/diffView.js";
import { runPickerHtml } from "../../components/runPicker.js";
import { getChunks } from "./data.js";

export async function renderDiff(container, ctx) {
  const defaultB = ctx.manifest?.base_run_id ?? "";
  container.innerHTML = `
    <div class="panel">
      <h2>对比对象</h2>
      <div class="row">
        <div class="col"><label>run A（当前）</label>
          <input type="text" value="${esc(ctx.runId)}" readonly></div>
        <div class="col"><label>run B（默认 base run）</label>
          ${runPickerHtml("df-run-b", defaultB)}</div>
      </div>
    </div>
    <div class="panel">
      <h2>Selection 路径对比（按 candidate_id 对齐）</h2>
      <button class="act" data-df="sel-go">对比路径</button>
      <div data-df="sel-out"></div>
    </div>
    <div class="panel">
      <h2>Chunk 对比</h2>
      <p class="kv">正文结果按句比较；渲染注入块按 7 个业务块分别比较。</p>
      <div class="row">
        <div class="col"><label>chunk A</label><select data-df="chunk-a"></select></div>
        <div class="col"><label>chunk B</label><select data-df="chunk-b"></select></div>
        <div class="col col-fit"><label>&nbsp;</label>
          <button class="act ghost" data-df="chunks-load">列 B 的 chunk</button>
          <button class="act" data-df="text-go">对比正文结果</button>
          <button class="act ghost" data-df="blocks-go">对比渲染注入块</button></div>
      </div>
      <div data-df="chunk-out"></div>
    </div>`;

  const q = (name) => container.querySelector(`[data-df="${name}"]`);
  const runB = () => {
    const value = container.querySelector("#df-run-b").value.trim();
    if (!value) throw new Error("请输入 run B");
    return value;
  };
  const chunkOptions = (chunks, runId) => chunks.map((c) =>
    `<option value="${esc(c.chunk_id)}">#${c.order_index + 1} ${esc(c.chunk_id)}
      (${c.chars}字${c.origin_run_id !== runId ? ", inherited" : ""})</option>`).join("");

  q("chunk-a").innerHTML = chunkOptions(await getChunks(ctx.runId), ctx.runId);

  q("chunks-load").addEventListener("click", async () => {
    try {
      const b = runB();
      const chunks = await api(`/api/runs/${encodeURIComponent(b)}/chunks`);
      q("chunk-b").innerHTML = chunkOptions(chunks, b);
    } catch (e) {
      q("chunk-out").innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
    }
  });

  q("sel-go").addEventListener("click", async () => {
    const out = q("sel-out");
    try {
      const d = await api(`/api/diff/selection?run_a=${encodeURIComponent(ctx.runId)}`
        + `&run_b=${encodeURIComponent(runB())}`);
      out.innerHTML = renderSelectionDiff(d);
    } catch (e) { out.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
  });

  q("text-go").addEventListener("click", async () => {
    const out = q("chunk-out");
    try {
      const d = await api(`/api/diff/chunks?run_a=${encodeURIComponent(ctx.runId)}`
        + `&chunk_a=${encodeURIComponent(q("chunk-a").value)}`
        + `&run_b=${encodeURIComponent(runB())}`
        + `&chunk_b=${encodeURIComponent(q("chunk-b").value)}`);
      out.innerHTML = renderTextDiff(d);
    } catch (e) { out.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
  });

  q("blocks-go").addEventListener("click", async () => {
    const out = q("chunk-out");
    try {
      const d = await api(`/api/diff/blocks?run_a=${encodeURIComponent(ctx.runId)}`
        + `&chunk_a=${encodeURIComponent(q("chunk-a").value)}`
        + `&run_b=${encodeURIComponent(runB())}`
        + `&chunk_b=${encodeURIComponent(q("chunk-b").value)}`);
      out.innerHTML = renderBlocksDiff(d);
    } catch (e) { out.innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
  });
}
