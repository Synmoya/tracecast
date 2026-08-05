// Local text import -> encoding preview -> Intake/manual review -> exclusive project commit.

import { post } from "../api.js";
import { esc, setMsg } from "../dom.js";
import { navigate } from "../router.js";
import { outputLanguageChoices, refreshProjects, state } from "../state.js";
import { watchJob } from "../jobs.js";

const encodings = ["", "utf-8", "utf-16-le", "utf-16-be", "gb18030",
  "shift_jis", "cp949", "windows-1252"];

export async function renderNewProject(container) {
  const profiles = state.setup?.source_profiles ?? [];
  const outputLanguages = outputLanguageChoices();
  container.innerHTML = `
    <div class="page-head"><div class="eyebrow">NEW PROJECT</div>
      <div class="page-title">创建材料项目</div>
      <p class="page-desc">支持 TXT、Markdown、SRT 和 VTT。本地解码确认后统一保存为无 BOM UTF-8。</p>
    </div>
    <section class="panel">
      <div class="form-grid">
        <label>项目 ID<input data-pd="id" type="text" required maxlength="64"
          pattern="[a-z0-9][a-z0-9_-]{0,63}" autocomplete="off" spellcheck="false"
          placeholder="my-project"></label>
        <label>目标输出语言<select data-pd="output">${outputLanguages.map((language) =>
          `<option value="${esc(language)}" ${language === "zh-CN" ? "selected" : ""}>${
            esc(language)}</option>`).join("")}</select></label>
        <label>专名方式<select data-pd="names">
          <option value="overrides_only">仅使用手工专名表</option>
          <option value="name_table">完整材料专名表</option>
        </select></label>
        <label>配置方式<select data-pd="mode">
          <option value="auto">自动建议 profile / language（需确认费用）</option>
          <option value="manual">逐文件手动配置（零模型调用）</option>
        </select></label>
      </div>
      <div class="kv form-note">Starter Host 只覆盖 zh-CN / en-US；其他目标语言需先导入或构建精确匹配的 Host。</div>
      <label>素材文件</label><input data-pd="files" type="file" multiple
        accept=".txt,.md,.markdown,.srt,.vtt,text/plain,text/markdown">
      <div data-pd="file-rows"></div>
      <button class="act ghost" data-pd="prepare">解码并预览</button>
      <button class="act ghost" data-pd="estimate" disabled>估算 Intake</button>
      <button class="act warn" data-pd="confirm" disabled>确认并生成配置建议</button>
      <div class="msg" data-pd="msg"></div>
      <div data-pd="preview"></div>
    </section>
    <section class="panel is-hidden" data-pd="review-panel">
      <h2>确认 source profile 与语言</h2>
      <p class="kv">自动建议必须逐项人工确认；unknown 或 needs_review 不能提交。</p>
      <div data-pd="review"></div>
      <button class="act" data-pd="commit">创建项目</button>
      <div class="msg" data-pd="commit-msg"></div>
    </section>`;
  const q = (name) => container.querySelector(`[data-pd="${name}"]`);
  let files = [];
  let draft = null;
  let proposal = null;
  let estimateValid = false;

  const invalidate = () => {
    estimateValid = false; q("estimate").disabled = true; q("confirm").disabled = true;
  };
  q("files").addEventListener("change", () => {
    files = [...q("files").files];
    renderFileRows(); invalidate();
  });
  q("file-rows").addEventListener("input", invalidate);
  q("file-rows").addEventListener("change", invalidate);
  q("mode").addEventListener("change", () => { renderFileRows(); invalidate(); });
  q("id").addEventListener("input", invalidate);
  ["output", "names"].forEach((name) => q(name).addEventListener("change", invalidate));

  function renderFileRows() {
    const manual = q("mode").value === "manual";
    q("file-rows").innerHTML = files.map((file, index) => `<article class="panel raised import-row"
      data-file-row="${index}"><strong>${esc(file.name)}</strong>
      <div class="kv">${Number(file.size).toLocaleString()} bytes</div>
      <div class="form-grid"><label>编码<select data-file-encoding>
        ${encodings.map((value) => `<option value="${esc(value)}">${esc(value || "自动判断")}</option>`).join("")}
      </select></label>${manual ? `<label>Profile<select data-file-profile>
        <option value="">请选择</option>${profiles.map((profile) =>
          `<option value="${esc(profile.id)}">${esc(profile.id)}</option>`).join("")}</select></label>
        <label>来源语言<input data-file-language type="text" placeholder="例如 zh-CN"></label>` : ""}</div>
      </article>`).join("") || `<div class="kv empty-state">尚未选择文件</div>`;
  }

  async function uploadPayload() {
    if (!files.length) throw new Error("请选择至少一个文件");
    const rows = [...q("file-rows").querySelectorAll("[data-file-row]")];
    return Promise.all(files.map(async (file, index) => ({
      name: file.name,
      data_base64: await fileBase64(file),
      encoding: rows[index].querySelector("[data-file-encoding]").value || null,
      profile: rows[index].querySelector("[data-file-profile]")?.value || null,
      language: rows[index].querySelector("[data-file-language]")?.value.trim() || null,
    })));
  }

  q("prepare").addEventListener("click", async () => {
    invalidate();
    try {
      draft = await post("/api/project-drafts", {
        project_id: q("id").value.trim(), output_language: q("output").value.trim(),
        name_resolution_mode: q("names").value, mode: q("mode").value,
        files: await uploadPayload(),
      });
      renderPreviews(draft);
      if (!draft.ready) {
        setMsg(q("msg"), "有文件无法可靠自动判断编码。请查看候选预览，选择编码后重新解码。", "warn");
        return;
      }
      q("estimate").disabled = false;
      setMsg(q("msg"), "解码完成。请核对预览，再估算配置建议。", "ok");
    } catch (error) { setMsg(q("msg"), error.message, "err"); }
  });

  q("estimate").addEventListener("click", async () => {
    try {
      const estimate = await post(`/api/project-drafts/${encodeURIComponent(draft.draft_id)}/estimate`, {});
      estimateValid = true; q("confirm").disabled = false;
      const cost = estimate.rate_status === "no_calls"
        ? "手动模式，零模型调用"
        : estimate.rate_status === "unconfigured"
          ? "未配置费率" : `配置估算 ${estimate.configured_cost}`;
      setMsg(q("msg"), `预计 ${estimate.calls} 次调用，输入约 ${
        Number(estimate.estimated_input_tokens).toLocaleString()} tokens；${cost}。`, "ok");
    } catch (error) { setMsg(q("msg"), error.message, "err"); }
  });

  q("confirm").addEventListener("click", async () => {
    if (!estimateValid) return;
    try {
      q("confirm").disabled = true;
      const response = await post(`/api/project-drafts/${encodeURIComponent(draft.draft_id)}/confirm`, {});
      if (response.proposal) {
        proposal = response.proposal; showReview(); return;
      }
      setMsg(q("msg"), "Intake 已提交，等待 profile 建议…", "warn");
      watchJob(response.job_id, (job) => {
        if (job.state !== "succeeded") {
          setMsg(q("msg"), `Intake ${job.state}：${job.error || "未知错误"}`, "err"); return;
        }
        proposal = job.result.proposal; showReview();
      });
    } catch (error) { setMsg(q("msg"), error.message, "err"); }
  });

  function showReview() {
    const byPath = new Map((proposal?.sources ?? []).map((row) => [row.path, row]));
    const rows = draft.files.map((file, index) => {
      const path = `raw/${file.name}`;
      const row = byPath.get(path) ?? {path, source_id: sourceSlug(file.name), profile: "", language: "", title: file.name.replace(/\.[^.]+$/, ""), note: ""};
      return `<article class="panel raised" data-review-row="${index}" data-path="${esc(path)}">
        <strong>${esc(file.name)}</strong><div class="form-grid">
        <label>Source ID<input data-r="id" type="text" value="${esc(row.source_id || "")}"></label>
        <label>Profile<select data-r="profile"><option value="">请选择</option>${profiles.map((profile) =>
          `<option value="${esc(profile.id)}" ${row.profile === profile.id ? "selected" : ""}>${esc(profile.id)}</option>`).join("")}</select></label>
        <label>来源语言<input data-r="language" type="text" value="${esc(row.language || "")}"></label>
        <label>标题<input data-r="title" type="text" value="${esc(row.title || "")}"></label></div>
        <label>备注<input data-r="note" type="text" value="${esc(row.note || "")}"></label></article>`;
    });
    q("review").innerHTML = rows.join("");
    q("review-panel").classList.remove("is-hidden");
    setMsg(q("msg"), "请确认每个来源的 profile 与语言。", "ok");
    q("review-panel").scrollIntoView({behavior: "smooth", block: "start"});
  }

  q("commit").addEventListener("click", async () => {
    try {
      const sources = [...q("review").querySelectorAll("[data-review-row]")].map((row) => ({
        path: row.dataset.path, source_id: row.querySelector('[data-r="id"]').value.trim(),
        profile: row.querySelector('[data-r="profile"]').value,
        language: row.querySelector('[data-r="language"]').value.trim(),
        title: row.querySelector('[data-r="title"]').value.trim(),
        note: row.querySelector('[data-r="note"]').value,
      }));
      const result = await post(`/api/project-drafts/${encodeURIComponent(draft.draft_id)}/commit`, {sources});
      await refreshProjects();
      setMsg(q("commit-msg"), `项目 ${result.project_id} 已创建。`, "ok");
      navigate("/new");
    } catch (error) { setMsg(q("commit-msg"), error.message, "err"); }
  });
  renderFileRows();
}

function renderPreviews(draft) {
  const target = document.querySelector('[data-pd="preview"]');
  target.innerHTML = draft.files.map((file) => `<details class="panel raised" open>
    <summary>${esc(file.name)} · ${esc(file.encoding || "需手动选择编码")}</summary>
    <div class="kv">${esc(file.detection_method)} · 原始 SHA-256 ${esc(file.original_sha256)}</div>
    <pre class="artifact-pre">${esc(file.preview || "")}</pre>
    ${Object.keys(file.candidate_previews || {}).length ? `<div class="candidate-previews">${
      Object.entries(file.candidate_previews).map(([encoding, text]) =>
        `<h3>${esc(encoding)}</h3><pre class="artifact-pre">${esc(text)}</pre>`).join("")}</div>` : ""}</details>`).join("");
}

async function fileBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  return btoa(binary);
}

function sourceSlug(name) {
  const stem = name.replace(/\.[^.]+$/, "").toLowerCase();
  return stem.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "source";
}
