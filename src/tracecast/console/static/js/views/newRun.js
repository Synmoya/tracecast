// 新建运行：项目 → 输出语言/Host → 来源 → 范围 → 运行策略 → 可选 Selection 取舍 →
// 估算 → 确认。runtime 配置 = 全局覆盖 + 本表单“高级配置”单次增量。

import { api, post } from "../api.js";
import { esc, lines, setMsg } from "../dom.js";
import { watchJob } from "../jobs.js";
import { navigate } from "../router.js";
import {
  mergeOverrides, on, outputLanguageChoices, overridesLeafCount, refreshRuns, state,
} from "../state.js";
import { createConfigEditor } from "../components/configEditor.js";
import { fmtNumber } from "../format.js";

function estimateHtml(e) {
  // Same markup contract as kvReport.js — one metric card shape app-wide.
  const metric = (value, label) =>
    `<div class="metric"><span class="metric-label">${esc(label)}</span>`
    + `<strong class="metric-value">${esc(value)}</strong></div>`;
  const rows = (e.source_stats || []).map((row) => `<tr>
    <td class="id" title="${esc(row.source_id)}">${esc(row.source_id)}</td>
    <td>${fmtNumber(row.chars)}</td><td>${row.pieces}</td><td>${row.windows}</td>
    <td>${fmtNumber(row.est_input_tokens)}</td></tr>`).join("");
  const models = e.models
    ? Object.entries(e.models).map(([route, model]) =>
        `<span class="badge">${esc(route)} · ${esc(model)}</span>`).join(" ")
    : `<span class="badge">${esc(e.route)} · ${esc(e.model)}</span>`;
  const rateStatus = e.rate_status === "configured"
    ? "已配置费率；实际成本将在模型返回 usage 后记录"
    : "未配置费率";
  return `<div class="metric-grid">
      ${metric(e.sources, "来源")}${metric(fmtNumber(e.source_chars), "字符")}
      ${metric(e.pieces, "切片")}${metric(e.windows, "输入批次")}
      ${metric(e.calls ?? "未知", "预计调用")}
      ${metric(fmtNumber(e.est_input_tokens), "预计输入 tokens")}
      ${e.max_calls_with_repair ? metric(e.max_calls_with_repair, "含修复上限") : ""}
    </div>
    <div class="kv">${esc(rateStatus)}</div>
    <div class="estimate-routes">${models}</div>
    <h3>来源明细</h3>
    <div class="table-wrap"><table><thead><tr><th>来源</th><th>字符</th>
      <th>切片</th><th>输入批次</th><th>预计 tokens</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="kv">${(e.notes || []).map(esc).join("<br>")}</div>`;
}

export async function renderNewRun(container) {
  const projects = state.projects;
  const hosts = state.hosts;
  container.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">NEW RUN</div>
      <div class="page-title">新建运行</div>
      <p class="page-desc">选择材料、成稿语言与运行策略。估算不会调用模型。
        <a class="id-link" href="#/new/project">创建材料项目</a></p>
    </div>
    <div class="panel">
      <label>项目</label>
      <select data-nr="project">${projects.map((p) =>
        `<option value="${esc(p.dir)}">${esc(p.project_id)}</option>`).join("")}</select>
      <div data-nr="language-wrap"><label>目标输出语言</label>
        <select data-nr="output-language">${outputLanguageChoices().map((language) =>
          `<option value="${esc(language)}">${esc(language)}</option>`).join("")}</select></div>
      <div data-nr="host-wrap"><label>兼容 Host</label>
        <select data-nr="host">${hosts.map((h) =>
          `<option value="${esc(h.host_id)}">${esc(h.display_name)} · ${esc(h.version)}${
            h.read_only ? " · 内置" : ""}</option>`
        ).join("")}</select></div>
      <label>来源
        <button class="mini" data-nr="all">全选</button>
        <button class="mini" data-nr="none">清空</button></label>
      <div data-nr="sources"><div class="kv">载入中…</div></div>
      <label>运行范围</label>
      <select data-nr="mode">
        <option value="full">完整流水线（材料整理 → 成稿）</option>
        <option value="ingestion">仅整理材料（调试）</option>
      </select>
      <div data-nr="strategy-wrap"><h3>运行策略</h3><div class="form-grid">
        <label>目标成稿长度（千字，可选）
          <input data-nr="target-kchars" type="number" min="0.1" step="0.1"></label>
        <label>专名方式<select data-nr="name-mode">
          <option value="project_default">项目默认</option>
          <option value="overrides_only">仅使用手工专名表</option>
          <option value="name_table">完整材料专名表</option>
        </select></label></div>
      </div>
      <label>运行名称（可选）</label><input type="text" data-nr="label"
        placeholder="例如：北极城市 · 第二版">
      <details data-nr="directive-wrap"><summary>本次编辑偏好（可选）</summary>
        <label>路径取舍要求（每行一条）</label>
        <textarea data-nr="directive" rows="3"></textarea>
        <div class="kv form-note">只影响 Selection 的入选与排序；不能补充事实，也不是正文措辞指令。</div>
      </details>
      <div class="kv form-note">默认设置中有 ${overridesLeafCount()} 项覆盖生效
        · <a class="id-link" href="#/config">查看/修改</a></div>
      <details><summary>高级设置</summary>
        <div data-nr="config"></div></details>
      <button class="act ghost" data-nr="estimate">估算运行</button>
      <button class="act warn" data-nr="confirm" disabled>开始运行</button>
      <div class="msg" data-nr="msg"></div>
      <div data-nr="estimate-detail"></div>
    </div>`;

  const q = (name) => container.querySelector(`[data-nr="${name}"]`);
  let sources = [];
  let editor = null;
  let projectDefaultLanguage = "";
  const invalidate = () => { q("confirm").disabled = true; };
  // stale listeners from earlier renders bail out once their DOM is detached
  on("overrides-changed", () => { if (container.isConnected) invalidate(); });

  function buildEditor() {
    editor = createConfigEditor(q("config"), {
      config: state.config,
      surface: q("mode").value === "full" ? "full_root" : "ingestion_root",
      baseline: state.globalOverrides,
      onChange: invalidate,
    });
  }

  function loadCompatibleHosts(resetLanguage = false) {
    const project = projects.find((item) => item.dir === q("project").value);
    if (resetLanguage) {
      projectDefaultLanguage = project?.output_language || "";
      q("output-language").innerHTML = outputLanguageChoices([projectDefaultLanguage])
        .map((language) => `<option value="${esc(language)}">${esc(language)}</option>`)
        .join("");
      q("output-language").value = projectDefaultLanguage;
    }
    const outputLanguage = q("output-language").value.trim();
    const compatible = hosts.filter((host) =>
      !outputLanguage || host.output_language === outputLanguage);
    q("host").innerHTML = compatible.length ? compatible.map((host) =>
      `<option value="${esc(host.host_id)}">${esc(host.display_name)} · ${esc(host.version)}${
        host.read_only ? " · 内置" : ""}</option>`
    ).join("") : `<option value="">没有匹配 ${esc(outputLanguage)} 的 Host</option>`;
    q("host").disabled = compatible.length === 0;
    invalidate();
  }

  async function loadSources() {
    q("sources").innerHTML = `<div class="kv">载入中…</div>`;
    try {
      sources = await api(`/api/projects/${encodeURIComponent(q("project").value)}/sources`);
      q("sources").innerHTML = sources.map((s) => `
        <div class="check-row">
          <input type="checkbox" data-source="${esc(s.source_id)}"
            ${s.exists ? "checked" : "disabled"}>
          <span>${esc(s.title)}${s.title !== s.source_id
            ? ` <span class="kv">(${esc(s.source_id)})</span>` : ""}
            ${s.exists ? "" : ' <span class="badge err">文件缺失</span>'}</span>
          <span class="kv">${esc(s.profile ?? "")}${s.chars !== null
            ? ` · ${fmtNumber(s.chars)} 字` : ""}</span>
        </div>`).join("") || `<div class="kv">该项目未声明来源</div>`;
      q("sources").querySelectorAll("input[data-source]").forEach((box) =>
        box.addEventListener("change", invalidate));
    } catch (e) {
      q("sources").innerHTML = `<div class="msg err">${esc(e.message)}</div>`;
      sources = [];
    }
    invalidate();
  }

  function selectedSourceIds() {
    const checked = [...q("sources").querySelectorAll("input[data-source]:checked")]
      .map((box) => box.dataset.source);
    if (!checked.length) throw new Error("请至少勾选一个来源");
    // 全选 = 不发 source_ids,语义与“全部来源”完全一致
    return checked.length === sources.length ? null : checked;
  }

  function payload(confirm) {
    const mode = q("mode").value;
    const directives = mode === "full" ? lines(q("directive")) : [];
    return {
      project_id: q("project").value,
      host_id: mode === "full" ? q("host").value : null,
      source_ids: selectedSourceIds(),
      output_language: mode === "full" && q("output-language").value.trim() !== projectDefaultLanguage
        ? q("output-language").value.trim() : null,
      name_resolution_mode: mode === "full" && q("name-mode").value !== "project_default"
        ? q("name-mode").value : null,
      target_output_kchars: mode === "full" && q("target-kchars").value
        ? Number(q("target-kchars").value) : null,
      selection_human_directive: directives.length ? directives : null,
      persona_patch: null,
      config_overrides: mergeOverrides(state.globalOverrides, editor.collect()),
      run_label: q("label").value.trim() || null,
      mode,
      confirm,
    };
  }

  q("project").addEventListener("change", () => {
    loadCompatibleHosts(true);
    loadSources();
  });
  q("mode").addEventListener("change", () => {
    q("host-wrap").classList.toggle("is-hidden", q("mode").value !== "full");
    q("language-wrap").classList.toggle("is-hidden", q("mode").value !== "full");
    q("strategy-wrap").classList.toggle("is-hidden", q("mode").value !== "full");
    q("directive-wrap").classList.toggle("is-hidden", q("mode").value !== "full");
    buildEditor();
    invalidate();
  });
  q("all").addEventListener("click", () => {
    q("sources").querySelectorAll("input[data-source]:not(:disabled)")
      .forEach((box) => box.checked = true);
    invalidate();
  });
  q("none").addEventListener("click", () => {
    q("sources").querySelectorAll("input[data-source]").forEach((box) => box.checked = false);
    invalidate();
  });
  ["directive", "label", "target-kchars"].forEach((name) =>
    q(name).addEventListener("input", invalidate));
  q("output-language").addEventListener("change", () => loadCompatibleHosts(false));
  q("name-mode").addEventListener("change", invalidate);
  q("host").addEventListener("change", invalidate);

  q("estimate").addEventListener("click", async () => {
    invalidate();
    try {
      const r = await post("/api/runs/ingestion", payload(false));
      const e = r.estimate;
      const scope = e.mode === "full"
        ? `已估算材料整理阶段：${e.ingestion_initial_calls} 次调用。后续阶段随产物动态变化。`
        : `预计最多 ${e.max_calls_with_repair} 次调用（含局部修复）。`;
      setMsg(q("msg"), scope, "ok");
      q("estimate-detail").innerHTML = estimateHtml(e);
      q("confirm").disabled = false;
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  });

  q("confirm").addEventListener("click", async () => {
    try {
      q("confirm").disabled = true;
      const r = await post("/api/runs/ingestion", payload(true));
      setMsg(q("msg"), "运行已提交，等待完成…", "warn");
      watchJob(r.job_id, async (job) => {
        if (job.state !== "succeeded") {
          setMsg(q("msg"), `运行${job.state === "failed" ? "失败" : "未完成"}：${job.error || "未知错误"}`, "err");
          return;
        }
        await refreshRuns();
        const sub = job.result?.mode === "ingestion" ? "/pipeline/ingestion" : "";
        navigate(`/run/${encodeURIComponent(r.run_id)}${sub}`);
      });
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  });

  buildEditor();
  if (projects.length) {
    loadCompatibleHosts(true);
    await loadSources();
  }
  else q("sources").innerHTML = `<div class="empty-state">没有可用项目。
    <a class="act ghost" href="#/new/project">创建材料项目</a></div>`;
}
