// 根配置: 与具体 run 无关的配置面。providers 只读;各阶段 routes / stage
// budgets 作为"全局默认 overrides"编辑,保存在浏览器 localStorage,自动附带到
// 每个花钱 POST。永不写回 configs/runtime.json（服务端内存合并不变式保持）。

import { esc, setMsg } from "../dom.js";
import {
  overridesLeafCount, saveGlobalOverrides, staleOverrideRoutes, state,
} from "../state.js";
import { createConfigEditor } from "../components/configEditor.js";

export function renderConfig(container) {
  const config = state.config;
  if (!config) {
    container.innerHTML = `<div class="panel"><div class="msg">配置尚未加载,稍后重试。</div></div>`;
    return;
  }
  const stale = staleOverrideRoutes();
  const providers = Object.entries(config.providers || {}).map(([name, p]) =>
    `<tr><td>${esc(name)}</td><td>${esc(p.base_url)}</td>
      <td>${esc((p.key_ids || []).join(", ") || "—")}</td></tr>`).join("");

  container.innerHTML = `
    <div class="page-head">
      <div class="eyebrow">SETTINGS</div>
      <div class="page-title">运行设置</div>
      <p class="page-desc">保存为当前浏览器的默认覆盖，不会修改 runtime.json。单次实验可在运行表单中另行调整。</p>
    </div>
    <div class="settings-overview">
      <div><span>Runtime 来源</span><strong class="id" title="${esc(config.config_path)}">${esc(config.config_path)}</strong></div>
      <div><span>浏览器覆盖</span><strong><b data-cfg="override-count">${overridesLeafCount()}</b> 项</strong></div>
      <div><span>Provider</span><strong>${Object.keys(config.providers || {}).length} 个 <em>只读</em></strong></div>
    </div>
    ${stale.length ? `<div class="banner">全局覆盖指向已不存在的 route:
      ${stale.map(esc).join(", ")} —— 花钱请求会被服务器拒绝。
      <button class="mini" data-cfg="drop-stale">移除失效 route</button></div>` : ""}
    <details class="panel settings-providers">
      <summary>Provider 摘要 <span class="badge">只读</span></summary>
      <div class="table-wrap"><table>
        <thead><tr><th>服务</th><th>地址</th><th>密钥标识</th></tr></thead>
        <tbody>${providers}</tbody></table></div>
    </details>
    <div class="panel">
      <h2>默认覆盖
        <span>
          <button class="act ghost button-flush" data-cfg="clear">全部清空</button>
          <button class="act button-flush" data-cfg="save">保存</button>
        </span></h2>
      <div data-cfg="editor"></div>
      <div class="msg" data-cfg="msg"></div>
    </div>`;

  const q = (name) => container.querySelector(`[data-cfg="${name}"]`);
  const editor = createConfigEditor(q("editor"), {
    config,
    surface: null,               // 根配置看全部字段与 routes
    baseline: null,              // diff 基准 = runtime 默认值
    values: state.globalOverrides,
    extraBox: false,
  });

  q("save").addEventListener("click", () => {
    try {
      const collected = editor.collect() || {};
      saveGlobalOverrides(collected);
      q("override-count").textContent = overridesLeafCount();
      setMsg(q("msg"), `已保存,全局覆盖 ${overridesLeafCount()} 项。`, "ok");
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  });
  q("clear").addEventListener("click", () => {
    saveGlobalOverrides({});
    renderConfig(container);
  });
  q("drop-stale")?.addEventListener("click", () => {
    const routes = {...state.globalOverrides.routes};
    for (const name of stale) delete routes[name];
    saveGlobalOverrides({...state.globalOverrides, routes});
    renderConfig(container);
  });
}
