// Chunk Lab（run 上下文）: 预填 7 注入块 → 编辑（改动标黄）→ 估算/确认单次真实
// 调用 → 与基线句级 diff。基线 run 固定为当前 run。

import { api, post } from "../../api.js";
import { esc, setMsg } from "../../dom.js";
import { watchJob } from "../../jobs.js";
import { navigate } from "../../router.js";
import { mergeOverrides, refreshRuns, state } from "../../state.js";
import { createConfigEditor } from "../../components/configEditor.js";
import { renderTextDiff } from "../../components/diffView.js";
import { getChunks } from "./data.js";
import { globalOverridesNote } from "./derive.js";

export async function renderChunklab(container, ctx) {
  const chunks = await getChunks(ctx.runId);
  container.innerHTML = globalOverridesNote()
    + `<div class="panel">
      <h2>选择基线 chunk</h2>
      <div class="row">
        <div class="col"><label>chunk</label>
          <select data-cl="chunk">${chunks.map((c) =>
            `<option value="${esc(c.chunk_id)}">#${c.order_index + 1} ${esc(c.chunk_id)}
              (${c.chars}字${c.origin_run_id !== ctx.runId ? ", inherited" : ""})</option>`)
            .join("")}</select></div>
        <div class="col col-fit"><label>&nbsp;</label>
          <button class="act" data-cl="load">载入注入块</button></div>
      </div>
      <div class="msg" data-cl="meta"></div>
    </div>
    <div class="panel is-hidden" data-cl="editor">
      <h2>注入块（相对当前重建基线有差异的块会标黄）</h2>
      <div data-cl="blocks"></div>
      <details><summary>高级配置（仅本次生效,叠加在全局覆盖之上）</summary>
        <div data-cl="config"></div></details>
      <label>运行名称（可选）</label><input type="text" data-cl="label">
      <button class="act ghost" data-cl="estimate">估算</button>
      <button class="act warn" data-cl="confirm" disabled>确认生成</button>
      <div class="msg" data-cl="msg"></div>
    </div>
    <div class="panel is-hidden" data-cl="result">
      <h2>结果对比（基线 ⇄ 新生成）</h2>
      <div data-cl="diff"></div>
      <h3>新文本</h3><pre data-cl="text"></pre>
      <button class="act is-hidden" data-cl="promote">
        用这套声音块做全量 Render</button>
    </div>`;

  const q = (name) => container.querySelector(`[data-cl="${name}"]`);
  let baseline = null;
  const invalidate = () => { q("confirm").disabled = true; };
  const editor = createConfigEditor(q("config"), {
    config: state.config, surface: "chunk_lab",
    baseline: state.globalOverrides, onChange: invalidate,
  });

  async function loadBlocks() {
    try {
      const chunkId = q("chunk").value;
      baseline = await api(`/api/runs/${encodeURIComponent(ctx.runId)}/chunks/`
        + `${encodeURIComponent(chunkId)}/blocks`);
      let meta = `order_index=${baseline.order_index}`
        + ` est_input_tokens=${baseline.est_input_tokens}`
        + ` prompt=${baseline.template.prompt_version}`;
      if (baseline.block_source === "manifest_snapshot") {
        meta += " | 已载入上次 Chunk Lab 的真实 7 块快照";
      } else if (baseline.block_source === "manifest_edits") {
        meta += " | 旧模板不可解析,已叠加 manifest 编辑块";
      } else {
        meta += " | 使用当前 contract/persona 重建块";
      }
      if (baseline.edited_keys?.length) {
        meta += ` | 相对重建基线有差异:${baseline.edited_keys.join(", ")}`;
      }
      let cls = "";
      if (baseline.matches_recorded_request === true) meta += " | 与录制 request 一致";
      else if (baseline.matches_recorded_request === false) {
        meta += " | 警告:重建 prompt 与当时录制不一致(模板/persona/协议已漂移)";
        cls = "warn";
      } else meta += " | 无基线渲染记录";
      setMsg(q("meta"), meta, cls);
      const comparison = baseline.reconstructed_blocks || baseline.blocks;
      q("blocks").innerHTML = Object.entries(baseline.blocks).map(([key, value]) =>
        `<div class="blocked${value !== comparison[key] ? " edited" : ""}" `
        + `data-block="${esc(key)}"><span class="bk">{{${esc(key)}}}</span>`
        + `<textarea data-block-key="${esc(key)}" rows="${Math.min(12,
          Math.max(2, value.split("\n").length))}">${esc(value)}</textarea></div>`).join("");
      q("blocks").querySelectorAll("textarea[data-block-key]").forEach((area) =>
        area.addEventListener("input", () => {
          const key = area.dataset.blockKey;
          area.closest(".blocked").classList.toggle("edited",
            baseline && area.value !== comparison[key]);
          invalidate();
        }));
      q("editor").classList.remove("is-hidden");
      q("result").classList.add("is-hidden");
      invalidate();
    } catch (e) { setMsg(q("meta"), e.message, "err"); }
  }

  const payload = (confirm) => {
    const blocks = {};
    q("blocks").querySelectorAll("textarea[data-block-key]").forEach((area) =>
      blocks[area.dataset.blockKey] = area.value);
    return {
      base_run_id: ctx.runId,
      chunk_id: q("chunk").value,
      blocks,
      config_overrides: mergeOverrides(state.globalOverrides, editor.collect()),
      run_label: q("label").value.trim() || null,
      confirm,
    };
  };

  q("load").addEventListener("click", loadBlocks);
  q("chunk").addEventListener("change", () => q("editor").classList.add("is-hidden"));
  q("label").addEventListener("input", invalidate);

  q("estimate").addEventListener("click", async () => {
    try {
      const r = await post("/api/chunklab/render", payload(false));
      setMsg(q("msg"), `估算:calls=${r.estimate.calls} est_input_tokens=`
        + `${r.estimate.est_input_tokens} model=${r.estimate.model}`
        + ` | 已改动块:${r.edited_keys.join(", ") || "(无)"}`, "ok");
      q("confirm").disabled = false;
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  });

  q("confirm").addEventListener("click", async () => {
    try {
      q("confirm").disabled = true;
      const chunkId = q("chunk").value;
      const r = await post("/api/chunklab/render", payload(true));
      setMsg(q("msg"), `已提交 job=${r.job_id} → ${r.run_id},等待完成…`, "warn");
      watchJob(r.job_id, async (job) => {
        if (job.state !== "succeeded") {
          setMsg(q("msg"), `job ${job.state}: ${job.error || ""}`, "err");
          return;
        }
        setMsg(q("msg"), `完成:${job.result.chars} 字,audit=${job.result.audit_items}`, "ok");
        q("text").textContent = job.result.text;
        const d = await api(`/api/diff/chunks?run_a=${encodeURIComponent(ctx.runId)}`
          + `&chunk_a=${encodeURIComponent(chunkId)}&run_b=${encodeURIComponent(r.run_id)}`
          + `&chunk_b=${encodeURIComponent(chunkId)}`);
        q("diff").innerHTML = renderTextDiff(d);
        q("result").classList.remove("is-hidden");
        q("promote").classList.remove("is-hidden");
        q("promote").onclick = () =>
          navigate(`/run/${encodeURIComponent(r.run_id)}/experiments/stages`);
        refreshRuns();
      });
    } catch (e) { setMsg(q("msg"), e.message, "err"); }
  });
}
