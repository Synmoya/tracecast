// Derive-form field definitions and markup. Change parameter presentation here;
// request construction and job flow stay in derive.js.

import { esc } from "../../dom.js";

export const DERIVE_KINDS = [
  {
    kind: "selection", surface: "derive_selection",
    title: "重选路径",
    note: "基于现有候选图调整路径并生成导演手记；也可用于比较不同 Host 的取舍。",
  },
  {
    kind: "node-edit", surface: "derive_node_edit",
    title: "仅重做材料编辑",
    note: "基于当前路径重新执行 keep / condense / omit；不会继续生成正文。",
  },
  {
    kind: "render", surface: "derive_render",
    title: "重新生成正文",
    note: "沿用当前路径与材料编辑结果，重新打包并生成全部正文。",
  },
];

export const PORTABLE_RENDER_BLOCKS = [
  ["host_thinking_block", "Host thinking（叙事思路）"],
  ["host_expression_block", "Host expression（表达方式）"],
];

export function personaFields(hosts) {
  return `
    <label>Host</label><select data-f="host">${hosts.map((host) =>
      `<option value="${esc(host.host_id)}">${esc(host.display_name)} · ${esc(host.version)}</option>`
    ).join("")}</select>
    <label>viewpoint 临时覆写（可空 = 沿用所选 Host）</label>
    <textarea data-f="viewpoint" rows="2"></textarea>
    <label>prefer（每行一条）</label><textarea data-f="prefer" rows="2"></textarea>
    <label>avoid（每行一条）</label><textarea data-f="avoid" rows="2"></textarea>
    <label>human_directive（每行一条）</label>
    <textarea data-f="directive" rows="2"></textarea>`;
}

export function renderBlockFields() {
  return `
    <label>专名方式<select data-f="name-mode">
      <option value="">继承 lineage</option>
      <option value="overrides_only">仅使用手工专名表</option>
      <option value="name_table">完整材料专名表</option>
    </select></label>
    <details data-f="render-blocks">
      <summary>全量声音注入块（实验）</summary>
      <div class="kv">只覆盖两个跨 chunk 不变的 host 块。位置、对应位置的风格示例、事实联系、材料和禁写项会继续按每个 chunk 动态生成。</div>
      <button class="act ghost" type="button" data-f="load-render-blocks">
        从当前 run 载入</button>
      <div class="msg" data-f="render-block-msg"></div>
      <div data-f="render-block-fields">
        ${PORTABLE_RENDER_BLOCKS.map(([key, label]) => `
          <div class="blocked" data-render-block-wrap="${esc(key)}">
            <label>${esc(label)} · <span class="bk">{{${esc(key)}}}</span></label>
            <textarea data-render-block="${esc(key)}" rows="7"
              placeholder="留空 = 使用本次 render 动态重建值"></textarea>
          </div>`).join("")}
      </div>
    </details>`;
}
