// LLM call ledger + 原始 request/response 查看。

import { api } from "../../api.js";
import { esc } from "../../dom.js";
import { navigate } from "../../router.js";
import { getCalls } from "./data.js";

export async function renderLlm(container, ctx) {
  const calls = await getCalls(ctx.runId);
  const current = ctx.rest && calls.some((c) => c.call_id === ctx.rest) ? ctx.rest : null;
  const rows = calls.map((c) =>
    `<tr class="click ${c.call_id === current ? "sel" : ""}" data-call="${esc(c.call_id)}">
      <td class="id" title="${esc(c.call_id)}">${esc(c.call_id)}</td>
      <td>${esc(c.operation_route)}</td>
      <td>${esc(c.provider_status ?? "")}</td>
      <td>${c.total_tokens ?? ""}</td></tr>`).join("");

  container.innerHTML = `
    <div class="panel">
      <h2>模型调用 <span class="badge">${calls.length}</span></h2>
      <p class="kv">选择一行查看原始请求与响应。</p>
      <div class="table-wrap"><table>
        <thead><tr><th>调用 ID</th><th>路由</th><th>状态</th><th>tokens</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>
    <div class="panel ${current ? "" : "is-hidden"}" id="raw-panel">
      <h2 id="raw-title">原始请求与响应</h2><pre id="raw-pre"></pre>
    </div>`;

  container.querySelectorAll("tr[data-call]").forEach((row) =>
    row.addEventListener("click", () =>
      navigate(`/run/${encodeURIComponent(ctx.runId)}/diagnostics/calls/`
        + encodeURIComponent(row.dataset.call))));

  if (!current) return;
  const text = (payload) =>
    payload.format === "text" ? payload.text : JSON.stringify(payload.data, null, 2);
  const base = `/api/runs/${encodeURIComponent(ctx.runId)}/llm/calls/`
    + `${encodeURIComponent(current)}/raw`;
  container.querySelector("#raw-title").textContent = `原始请求与响应 · ${current}`;
  try {
    const req = await api(`${base}?role=request`);
    let resText = "（无响应内容）";
    try {
      resText = text(await api(`${base}?role=response`));
    } catch (e) { /* missing response is fine */ }
    container.querySelector("#raw-pre").textContent =
      "=== request ===\n" + text(req) + "\n\n=== response ===\n" + resText;
  } catch (e) {
    container.querySelector("#raw-pre").textContent = e.message;
  }
}
