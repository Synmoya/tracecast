// Stage Trace（run 上下文）: 从正文反查每个 work unit 的实际输入块、可读编译
// 结果与原始调用；run 由路由固定,只选 chunk / stage / unit。

import { api } from "../../api.js";
import { esc, setMsg } from "../../dom.js";
import { getChunks } from "./data.js";

export async function renderTrace(container, ctx) {
  const chunks = await getChunks(ctx.runId);
  container.innerHTML = `
    <div class="panel">
      <h2>追查入口</h2>
      <div class="row">
        <div class="col"><label>正文分段（不选则列出全部调用单元）</label>
          <select data-tr="chunk"><option value="">全部调用单元</option>
            ${chunks.map((c) =>
              `<option value="${esc(c.chunk_id)}">#${c.order_index + 1} ${esc(c.chunk_id)}
                (${c.chars}字${c.origin_run_id !== ctx.runId ? ", inherited" : ""})</option>`)
              .join("")}</select></div>
        <div class="col col-fit"><label>&nbsp;</label>
          <button class="act" data-tr="load">载入追查</button></div>
      </div>
      <div class="msg" data-tr="msg"></div>
      <div class="stage-buttons" data-tr="stages"></div>
      <div class="row is-hidden" data-tr="unit-row">
        <div class="col"><label>调用单元</label><select data-tr="unit"></select></div>
      </div>
    </div>
    <div class="row">
      <div class="col"><div class="panel is-hidden" data-tr="input">
        <h2>实际输入块（只读）</h2><div data-tr="blocks"></div>
      </div></div>
      <div class="col"><div class="panel is-hidden" data-tr="output">
        <h2>可读编译结果</h2><pre data-tr="compiled"></pre>
        <details><summary>技术详情、stable refs、诊断与原始调用</summary>
          <div data-tr="technical"></div>
        </details>
      </div></div>
    </div>`;

  const q = (name) => container.querySelector(`[data-tr="${name}"]`);
  let traceIndex = null;
  let stageName = null;

  async function load() {
    try {
      const chunk = q("chunk").value;
      traceIndex = await api(`/api/runs/${encodeURIComponent(ctx.runId)}/trace`
        + (chunk ? `?chunk_id=${encodeURIComponent(chunk)}` : ""));
      setMsg(q("msg"),
        traceIndex.message
          || `artifact origins: ${JSON.stringify(traceIndex.artifact_origins)}`,
        traceIndex.exact_trace_available ? "ok" : "warn");
      q("stages").innerHTML = traceIndex.stages.map((s) =>
        `<button class="act ghost" data-stage="${esc(s.stage)}"
          ${s.available ? "" : "disabled"}>${esc(s.stage)} (${s.units.length})</button>`).join("");
      q("stages").querySelectorAll("[data-stage]").forEach((btn) =>
        btn.addEventListener("click", () => chooseStage(btn.dataset.stage)));
      const first = traceIndex.stages.find((s) => s.available);
      if (first) await chooseStage(first.stage);
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  }

  async function chooseStage(stage) {
    stageName = stage;
    q("stages").querySelectorAll("[data-stage]").forEach((btn) =>
      btn.classList.toggle("sel", btn.dataset.stage === stage));
    const entry = traceIndex.stages.find((s) => s.stage === stage);
    q("unit").innerHTML = entry.units.map((unit) =>
      `<option value="${esc(unit.unit_id)}">${esc(unit.kind)} · ${esc(unit.unit_id)}`
      + `${unit.origin_run_id !== traceIndex.run_id
        ? ` · origin=${esc(unit.origin_run_id)}` : ""}</option>`).join("");
    q("unit-row").classList.remove("is-hidden");
    await loadUnit();
  }

  async function loadUnit() {
    if (!stageName || !q("unit").value) return;
    try {
      const unitId = q("unit").value;
      const d = await api(`/api/runs/${encodeURIComponent(ctx.runId)}/trace/`
        + `${encodeURIComponent(stageName)}/${encodeURIComponent(unitId)}`);
      q("blocks").innerHTML = Object.entries(d.input_blocks || {}).map(([key, value]) =>
        `<div class="blocked"><span class="bk">{{${esc(key)}}}</span>`
        + `<textarea readonly rows="${Math.min(16, Math.max(2, String(value).split("\n").length))}">`
        + `${esc(value)}</textarea></div>`).join("") || '<div class="kv">无输入块</div>';
      q("compiled").textContent = JSON.stringify(d.compiled_output, null, 2);
      const calls = (d.call_links || []).map((call) =>
        `<button class="act ghost" data-call="${esc(call.call_id)}">`
        + `${esc(call.call_id)} · ${esc(call.status)} · attempt ${esc(call.attempt ?? "?")}</button>`)
        .join("");
      q("technical").innerHTML = `<div class="kv">origin_run_id=${esc(d.origin_run_id)}</div>`
        + `<pre>${esc(JSON.stringify(d.technical || {}, null, 2))}</pre>` + calls
        + `<h3>diagnostics</h3><pre>${esc(JSON.stringify(d.diagnostics || [], null, 2))}</pre>`;
      q("technical").querySelectorAll("[data-call]").forEach((btn) =>
        btn.addEventListener("click", () => showRaw(btn.dataset.call)));
      q("input").classList.remove("is-hidden");
      q("output").classList.remove("is-hidden");
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  }

  async function showRaw(callId) {
    try {
      const base = `/api/runs/${encodeURIComponent(ctx.runId)}/llm/calls/`
        + `${encodeURIComponent(callId)}/raw`;
      const [request, response] = await Promise.all([
        api(`${base}?role=request`), api(`${base}?role=response`),
      ]);
      const text = (payload) =>
        payload.format === "text" ? payload.text : JSON.stringify(payload.data, null, 2);
      q("technical").insertAdjacentHTML("beforeend",
        `<details open><summary>${esc(callId)} raw</summary>`
        + `<pre>${esc("=== request ===\n" + text(request)
          + "\n\n=== response ===\n" + text(response))}</pre></details>`);
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  }

  q("load").addEventListener("click", load);
  q("unit").addEventListener("change", loadUnit);
}
