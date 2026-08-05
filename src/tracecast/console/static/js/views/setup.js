// First-run and day-to-day model connection setup. Secrets are write-only.

import { put } from "../api.js";
import { esc, setMsg } from "../dom.js";
import { refreshConfig, refreshSetup, state } from "../state.js";

function currentConnection(config) {
  const routes = config?.routes ?? {};
  const standardRoute = routes.render_chunk ?? Object.values(routes)[0] ?? {};
  const selectionRoute = routes.selector_path_dsl ?? {};
  const providers = config?.providers ?? {};
  const providerName = standardRoute.provider ?? Object.keys(providers)[0] ?? "";
  const provider = providers[providerName] ?? {};
  const standardModel = standardRoute.model ?? "";
  const selectionModel = selectionRoute.model && selectionRoute.model !== standardModel
    ? selectionRoute.model : "";
  return {
    providerName,
    gatewayUrl: provider.base_url ?? "",
    standardModel,
    selectionModel,
    completionPath: provider.chat_completions_path ?? "/chat/completions",
    tokenField: provider.max_output_tokens_field ?? "max_tokens",
  };
}

export async function renderSetup(container) {
  const setup = state.setup;
  const configured = Boolean(setup?.runtime?.configured);
  const connection = currentConnection(state.config);
  container.innerHTML = `
    <div class="page-head"><div class="eyebrow">${configured ? "CONNECTION" : "FIRST RUN"}</div>
      <div class="page-title">快速配置</div>
      <p class="page-desc">连接一个 OpenAI-compatible Chat Completions 网关，集中管理本机 API key
        与默认模型。</p></div>
    <section class="panel">
      <h2>${configured ? "模型连接" : "1 · 模型连接"}</h2>
      <div class="form-grid">
        <label>Gateway URL<input data-setup="url" type="url"
          value="${esc(connection.gatewayUrl)}" placeholder="https://gateway.example/v1"></label>
        <label>API key<input data-setup="key" type="password" autocomplete="new-password"
          placeholder="${configured && setup?.api_key_configured
            ? "已配置；更新时请重新输入" : "输入 API key"}">
          <span class="kv">仅保存在本机，不会回显。</span></label>
        <label>标准模型<input data-setup="model" type="text"
          value="${esc(connection.standardModel)}" placeholder="model-name"></label>
        <label>Selection 模型（可选）<input data-setup="selection" type="text"
          value="${esc(connection.selectionModel)}" placeholder="留空则继承标准模型"></label>
      </div>
      <details><summary>网关兼容设置</summary><div class="form-grid">
        <label>Completion path<input data-setup="path" type="text"
          value="${esc(connection.completionPath)}"></label>
        <label>输出 token 字段<select data-setup="token-field">
          <option value="max_tokens" ${connection.tokenField === "max_tokens" ? "selected" : ""}>
            max_tokens</option>
          <option value="max_completion_tokens" ${
            connection.tokenField === "max_completion_tokens" ? "selected" : ""}>
            max_completion_tokens</option>
        </select></label>
      </div><label>额外请求字段（JSON object）</label>
        <textarea data-setup="extra" rows="3" placeholder="${
          configured ? "留空则保留当前 Route 设置" : "{}"}"></textarea></details>
      <label class="check-row"><input data-setup="confirm" type="checkbox">
        <span>确认更新本机运行配置，并把 API key 仅保存到本机 .env</span></label>
      <button class="act" data-setup="save">${configured ? "更新连接" : "保存并继续"}</button>
      <div class="msg" data-setup="msg"></div>
      <div class="kv" data-setup="status">当前连接：${
        configured ? esc(connection.providerName || "runtime.json") : "尚未配置"} · API key ${
        setup?.api_key_configured ? "已配置" : "未配置"}</div>
    </section>`;

  const q = (name) => container.querySelector(`[data-setup="${name}"]`);
  q("save").addEventListener("click", async () => {
    try {
      let extraBody;
      const extraRaw = q("extra").value.trim();
      try { extraBody = extraRaw ? JSON.parse(extraRaw) : null; }
      catch { throw new Error("额外请求字段必须是 JSON object"); }
      if (extraBody !== null
          && (Array.isArray(extraBody) || typeof extraBody !== "object"))
        throw new Error("额外请求字段必须是 JSON object");
      const result = await put("/api/setup/runtime", {
        gateway_url: q("url").value.trim(), api_key: q("key").value,
        standard_model: q("model").value.trim(),
        selection_model: q("selection").value.trim() || null,
        completion_path: q("path").value.trim(),
        max_output_tokens_field: q("token-field").value,
        extra_body: extraBody, confirm: q("confirm").checked,
      });
      q("key").value = "";
      await refreshSetup();
      await refreshConfig();
      setMsg(q("msg"), "网关配置已保存；密钥不会在界面或 API 中回显。", "ok");
      q("status").textContent = `当前连接：${result.provider} · API key ${
        state.setup.api_key_configured ? "已配置" : "未配置"}`;
    } catch (error) { setMsg(q("msg"), error.message, "err"); }
  });
}
