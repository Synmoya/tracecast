// 派生重跑（run 上下文）: selection / node-edit / render,base 固定为当前 run。
// 配置 = 全局 overrides（自动附带）+ 本表单的单次增量,两段确认照旧。
// Render 表单仍只覆盖两个跨 chunk 不变的 host 块；位置、对应位置的风格示例、事实联系、材料和禁写项
// 继续逐 chunk 动态生成。字段文案见 deriveFields.js。

import { api, post } from "../../api.js";
import { esc, lines, setMsg } from "../../dom.js";
import { watchJob } from "../../jobs.js";
import { navigate } from "../../router.js";
import { mergeOverrides, overridesLeafCount, refreshRuns, state } from "../../state.js";
import { createConfigEditor } from "../../components/configEditor.js";
import { getChunks } from "./data.js";
import {
  DERIVE_KINDS, PORTABLE_RENDER_BLOCKS, personaFields, renderBlockFields,
} from "./deriveFields.js";

export function globalOverridesNote() {
  const count = overridesLeafCount();
  return `<div class="kv">全局覆盖 ${count} 项生效（自动附带到本次请求）
    · <a class="id-link" href="#/config">查看/修改</a></div>`;
}

async function renderDerive(container, ctx, {kinds, banner}) {
  const entries = DERIVE_KINDS.filter(({kind}) => kinds.includes(kind));
  container.innerHTML = banner
    + globalOverridesNote()
    + `<div class="row">${entries.map(({kind, title, note}) => `
      <div class="col"><div class="panel" data-kind="${kind}">
        <h2>${esc(title)}</h2>
        <div class="kv">${esc(note)}</div>
        ${kind === "selection" ? personaFields(state.hosts) : ""}
        ${kind === "render" ? renderBlockFields() : ""}
        <details><summary>高级配置（仅本次生效,叠加在全局覆盖之上）</summary>
          <div data-f="config"></div></details>
        <label>运行名称（可选）</label><input type="text" data-f="label">
        <button class="act ghost" data-f="estimate">估算(不花钱)</button>
        <button class="act warn" data-f="confirm">确认重跑(真实调用)</button>
        <div class="msg" data-f="msg"></div>
      </div></div>`).join("")}</div>`;

  for (const {kind, surface} of entries) {
    const panel = container.querySelector(`[data-kind="${kind}"]`);
    const q = (name) => panel.querySelector(`[data-f="${name}"]`);
    const editor = createConfigEditor(q("config"), {
      config: state.config, surface,
      baseline: state.globalOverrides,
    });

    async function loadRenderBlocks() {
      const message = q("render-block-msg");
      try {
        const chunks = await getChunks(ctx.runId);
        if (!chunks.length) throw new Error("当前 run lineage 没有可载入的 chunk");
        const chunk = [...chunks].sort((a, b) => a.order_index - b.order_index)[0];
        const snapshot = await api(`/api/runs/${encodeURIComponent(ctx.runId)}/chunks/`
          + `${encodeURIComponent(chunk.chunk_id)}/blocks`);
        const allowed = new Set(snapshot.portable_block_keys || []);
        for (const [key] of PORTABLE_RENDER_BLOCKS) {
          if (!allowed.has(key)) continue;
          const area = panel.querySelector(`textarea[data-render-block="${key}"]`);
          const reconstructed = String(snapshot.reconstructed_blocks?.[key] ?? "");
          area.value = String(snapshot.blocks?.[key] ?? reconstructed);
          area.dataset.baseline = reconstructed;
          area.dataset.loaded = "true";
          area.closest(".blocked").classList.toggle("edited", area.value !== reconstructed);
        }
        const transferred = (snapshot.edited_keys || [])
          .filter((key) => allowed.has(key));
        const localOnly = (snapshot.edited_keys || [])
          .filter((key) => !allowed.has(key));
        let text = `已从 #${chunk.order_index + 1} ${chunk.chunk_id} 载入`;
        if (transferred.length) text += `；将带入：${transferred.join(", ")}`;
        else text += "；当前两个块与动态重建值一致";
        if (localOnly.length) {
          text += `；仅单 chunk 的改动不会带入：${localOnly.join(", ")}`;
        }
        setMsg(message, text, localOnly.length ? "warn" : "ok");
      } catch (e) {
        setMsg(message, `${e.message}。仍可手工填写；空白表示不覆盖。`, "warn");
      }
    }

    if (kind === "render") {
      q("load-render-blocks").addEventListener("click", loadRenderBlocks);
      panel.querySelectorAll("textarea[data-render-block]").forEach((area) => {
        area.addEventListener("input", () => {
          const baseline = area.dataset.loaded === "true"
            ? area.dataset.baseline : "";
          area.closest(".blocked").classList.toggle("edited", area.value !== baseline);
        });
      });
      await loadRenderBlocks();
    }

    const payload = (confirm) => {
      const p = {
        base_run_id: ctx.runId,
        config_overrides: mergeOverrides(state.globalOverrides, editor.collect()),
        run_label: q("label").value.trim() || null,
        confirm,
      };
      if (kind === "selection") {
        p.host_id = q("host").value;
        const patch = {};
        if (q("viewpoint").value.trim()) patch.viewpoint = q("viewpoint").value.trim();
        const profile = {};
        if (lines(q("prefer")).length) profile.prefer = lines(q("prefer"));
        if (lines(q("avoid")).length) profile.avoid = lines(q("avoid"));
        if (lines(q("directive")).length) profile.human_directive = lines(q("directive"));
        if (Object.keys(profile).length) patch.viewpoint_profile = profile;
        if (Object.keys(patch).length) p.persona_patch = patch;
      }
      if (kind === "render") {
        p.name_resolution_mode = q("name-mode").value || null;
        const blockOverrides = {};
        panel.querySelectorAll("textarea[data-render-block]").forEach((area) => {
          const baseline = area.dataset.loaded === "true"
            ? area.dataset.baseline : "";
          if (area.value !== baseline) blockOverrides[area.dataset.renderBlock] = area.value;
        });
        if (Object.keys(blockOverrides).length) {
          p.render_block_overrides = blockOverrides;
        }
      }
      return p;
    };

    q("estimate").addEventListener("click", async () => {
      try {
        const r = await post(`/api/derive/${kind}`, payload(false));
        const e = r.estimate;
        setMsg(q("msg"), `估算:calls=${e.calls ?? "视批次而定"} est_input_tokens=`
          + `${e.est_input_tokens} model=${e.model}`
          + `${e.max_calls_with_repair !== undefined
            ? ` 含修复上限=${e.max_calls_with_repair}` : ""}`
          + `\n${(e.notes || []).join("; ")}`, "ok");
      } catch (e) { setMsg(q("msg"), e.message, "err"); }
    });

    q("confirm").addEventListener("click", async () => {
      try {
        const r = await post(`/api/derive/${kind}`, payload(true));
        setMsg(q("msg"), `已提交 job=${r.job_id} → ${r.run_id},等待完成…`, "warn");
        watchJob(r.job_id, async (job) => {
          if (job.state !== "succeeded") {
            setMsg(q("msg"), `job ${job.state}: ${job.error || ""}`, "err");
            return;
          }
          setMsg(q("msg"), `完成:${JSON.stringify(job.result)}\n→ 派生 run:${r.run_id}`, "ok");
          await refreshRuns();
          navigate(`/run/${encodeURIComponent(r.run_id)}`);
        });
      } catch (e) { setMsg(q("msg"), e.message, "err"); }
    });
  }
}

export async function renderPathAdjustment(container, ctx) {
  return renderDerive(container, ctx, {
    kinds: ["selection"],
    banner: `<div class="banner neutral"><strong>路径调整</strong>
      输入临时取舍要求或更换 Host，基于当前候选图生成一个新的路径版本；不会自动生成正文。</div>`,
  });
}

export async function renderStageTests(container, ctx) {
  return renderDerive(container, ctx, {
    kinds: ["node-edit", "render"],
    banner: `<div class="banner neutral"><strong>高级阶段测试</strong>
      这里的操作只重跑指定阶段，用于隔离检查材料编辑或正文生成。</div>`,
  });
}
