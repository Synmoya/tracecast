// Host catalog plus an attached, optional Host Builder tool. Browser-selected
// reference text is held only for the current form and sent as JSON to localhost.

import { api, post } from "../api.js";
import { createConfigEditor } from "../components/configEditor.js";
import { esc, setMsg } from "../dom.js";
import { watchJob } from "../jobs.js";
import { navigate } from "../router.js";
import { mergeOverrides, refreshHosts, state } from "../state.js";

let selectedHostId = null;
const HOST_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;
const WINDOWS_RESERVED_HOST_ID = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/;

function bullets(items) {
  return items?.length ? `<ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`
    : `<div class="kv">无</div>`;
}

function shortHash(value) {
  if (!value) return "—";
  return value.length > 20 ? `${value.slice(0, 18)}…` : value;
}

function hostIdFromDisplayName(displayName) {
  const source = displayName.trim().normalize("NFKC");
  if (!source) return "";
  const ascii = source.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const slug = ascii.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const needsHash = /[^\x00-\x7f]/.test(source) || !slug;
  if (!needsHash) {
    const candidate = slug.slice(0, 64).replace(/-+$/g, "");
    return WINDOWS_RESERVED_HOST_ID.test(candidate) ? `host-${candidate}` : candidate;
  }
  let hash = 2166136261;
  for (const character of source) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  const suffix = `-${(hash >>> 0).toString(16).padStart(8, "0")}`;
  const prefix = (slug || "host").slice(0, 64 - suffix.length).replace(/-+$/g, "") || "host";
  return `${prefix}${suffix}`;
}

function detailHtml(snapshot) {
  const h = snapshot.profile;
  const examples = Object.entries(h.style_examples_by_position).map(([position, rows]) => `
    <details><summary>${esc(position)} · ${rows.length} 组</summary>
      ${rows.map((row) => `<div class="host-example"><div class="kv">原始材料</div>
        <p>${esc(row.source)}</p><div class="kv">Host 成文</div><p>${esc(row.render)}</p>
        ${row.lesson ? `<div class="kv">要点：${esc(row.lesson)}</div>` : ""}</div>`).join("")}
    </details>`).join("");
  return `<div class="host-detail-head">
      <div><div class="eyebrow">HOST PROFILE</div>
      <h2>${esc(h.display_name)}</h2></div>
      <div class="host-meta">${snapshot.read_only ? '<span class="badge">内置</span>' : ""}
        <span class="badge">${esc(h.host_id)}</span>
        <span class="badge">${esc(h.version)}</span>
        <span class="badge">${esc(h.output_language)}</span></div></div>
    <div class="host-snapshot kv">
      <span>schema ${esc(h.schema_version)}</span>
      <span title="${esc(snapshot.content_hash)}">hash ${esc(shortHash(snapshot.content_hash))}</span>
    </div>
    <div class="panel"><h3>思考方式</h3><p>${esc(h.host_thinking.viewpoint)}</p>
      <div class="kv">偏好</div>${bullets(h.host_thinking.prefer)}
      <div class="kv">规避</div>${bullets(h.host_thinking.avoid)}
      <div class="kv">稳定规则</div>${bullets(h.host_thinking.rules)}</div>
    <div class="panel"><h3>表达方式</h3><p>${esc(h.host_expression.description)}</p>
      ${bullets(h.host_expression.rules)}</div>
    <div class="panel"><h3>位置化风格示例</h3>${examples}</div>`;
}

function hostRow(host, selected) {
  return `<button type="button" class="host-row ${selected ? "selected" : ""}"
      data-host-id="${esc(host.host_id)}">
    <span class="host-row-title">${esc(host.display_name)}${
      host.read_only ? ' <span class="badge">内置</span>' : ""}</span>
    <span class="host-row-meta"><span>${esc(host.version)}</span><span>${esc(host.output_language)}</span>
      <span>${esc(host.schema_version)}</span></span>
    <span class="host-row-hash">${esc(shortHash(host.content_hash))}</span>
  </button>`;
}

export async function renderHosts(container, {mode = "browse"} = {}) {
  if (mode === "build") {
    renderHostBuilder(container);
    return;
  }
  const hosts = state.hosts;
  if (!hosts.some((host) => host.host_id === selectedHostId)) {
    selectedHostId = hosts[0]?.host_id ?? null;
  }
  container.innerHTML = `<div class="page-head page-head-actions"><div>
      <div class="eyebrow">LIBRARY</div><div class="page-title">Host 库</div>
      <p class="page-desc">浏览可复用的主持人视角、表达规则与位置化示例。</p></div>
      <a class="link-button" href="#/hosts/build">构建个人 Host</a></div>
    <div class="host-mobile-control panel"><label for="host-mobile-picker">选择 Host</label>
      <select id="host-mobile-picker">${hosts.map((host) => `<option value="${esc(host.host_id)}"
        ${host.host_id === selectedHostId ? "selected" : ""}>${esc(host.display_name)} · ${esc(host.version)}${
          host.read_only ? " · 内置" : ""}</option>`).join("")}</select></div>
    <div class="host-library-layout">
      <aside class="host-browser panel"><label for="host-search">搜索 Host</label>
        <input id="host-search" type="search" placeholder="名称、ID、版本或语言…" autocomplete="off">
        <div class="host-count kv" data-host-count></div><div class="host-list" data-host-list></div></aside>
      <section class="host-detail" data-host-detail></section>
    </div>`;

  const search = container.querySelector("#host-search");
  const picker = container.querySelector("#host-mobile-picker");
  const list = container.querySelector("[data-host-list]");
  const count = container.querySelector("[data-host-count]");
  const detail = container.querySelector("[data-host-detail]");

  function renderList() {
    const needle = search.value.trim().toLocaleLowerCase();
    const visible = hosts.filter((host) => [host.host_id, host.display_name, host.version,
       host.output_language, host.schema_version, host.read_only ? "内置" : "个人"].some((value) =>
      String(value ?? "").toLocaleLowerCase().includes(needle)));
    count.textContent = `${visible.length} / ${hosts.length}`;
    list.innerHTML = visible.length
      ? visible.map((host) => hostRow(host, host.host_id === selectedHostId)).join("")
      : `<div class="empty-state kv">没有匹配的 Host。</div>`;
  }

  async function load(hostId) {
    selectedHostId = hostId || null;
    if (picker && picker.value !== selectedHostId) picker.value = selectedHostId ?? "";
    renderList();
    if (!selectedHostId) {
      detail.innerHTML = `<div class="panel empty-state">hosts/ 中没有可用 Host。</div>`;
      return;
    }
    try {
      detail.innerHTML = `<div class="panel kv">载入中…</div>`;
      detail.innerHTML = detailHtml(await api(`/api/hosts/${encodeURIComponent(selectedHostId)}`));
    } catch (error) {
      detail.innerHTML = `<div class="msg err">${esc(error.message)}</div>`;
    }
  }

  search.addEventListener("input", renderList);
  list.addEventListener("click", (event) => {
    const row = event.target.closest("[data-host-id]");
    if (row) load(row.dataset.hostId);
  });
  picker?.addEventListener("change", () => load(picker.value));
  renderList();
  await load(selectedHostId);
}

function renderHostBuilder(container) {
  const config = state.config;
  if (!config) {
    container.innerHTML = `<div class="panel"><div class="msg">配置尚未加载，稍后重试。</div></div>`;
    return;
  }
  container.innerHTML = `<div class="page-head page-head-actions"><div>
      <div class="eyebrow">HOST LIBRARY / BUILDER</div><div class="page-title">构建个人 Host</div>
      <p class="page-desc">用偏好的成稿提炼一个只新增、不覆盖的 Host。估算不调用模型；确认后固定执行两次串行调用。</p></div>
      <a class="link-button" href="#/hosts">返回 Host 库</a></div>
    <div class="builder-layout">
      <form class="panel builder-form" data-builder-form novalidate>
        <h2>参考稿与名称</h2>
        <label for="builder-name">Host 名称</label><input id="builder-name" type="text" required
          autocomplete="off" placeholder="例如：我的游戏解说">
        <label for="builder-files">UTF-8 .txt 参考稿</label>
        <input id="builder-files" type="file" accept=".txt,text/plain" multiple required>
        <div class="builder-file-list kv" data-builder-files>尚未选择文件。</div>
        <details class="builder-advanced"><summary>高级设置</summary>
          <div class="form-grid form-grid-2 builder-advanced-fields">
            <div><label for="builder-host-id">唯一标识</label>
              <input id="builder-host-id" type="text" required maxlength="64"
                pattern="[a-z0-9][a-z0-9-]{0,63}" autocomplete="off" spellcheck="false">
              <div class="builder-field-action"><span class="kv">用于文件名和 Run 引用，创建后保持稳定。</span>
                <button class="mini" type="button" data-builder-reset-id>根据名称重置</button></div></div>
            <div><label for="builder-version">内容版本</label><input id="builder-version" type="text"
              placeholder="留空则使用 &lt;host_id&gt;@v1"></div>
            <div><label for="builder-piece-tokens">piece token 上限</label><input id="builder-piece-tokens"
              type="number" min="1" step="1" value="180" required></div>
            <div><label for="builder-seed">seed</label><input id="builder-seed" type="number"
              step="1" placeholder="留空则按输入稳定派生"></div>
          </div>
          <h3>模型与 Route</h3><div data-builder-config></div></details>
        <div class="builder-actions">
          <button class="act" type="submit" data-builder-estimate>预检并估算</button>
          <button class="act" type="button" data-builder-confirm disabled>确认构建</button>
          <button class="act ghost is-hidden" type="button" data-builder-cancel>取消任务</button>
        </div>
        <div class="msg" data-builder-msg></div>
      </form>
      <section data-builder-estimate-view>
        <div class="panel builder-placeholder"><div class="eyebrow">DRY RUN</div>
          <h2>先确认输入，再调用模型</h2><p>预检会完成确定性抽样与 prompt 组装，展示各位置可用样本和输入 token 估算；不会创建 run。</p></div>
      </section>
    </div>`;

  const q = (name) => container.querySelector(`[data-builder-${name}]`);
  const form = q("form");
  const fileInput = container.querySelector("#builder-files");
  const displayNameInput = container.querySelector("#builder-name");
  const hostIdInput = container.querySelector("#builder-host-id");
  const versionInput = container.querySelector("#builder-version");
  const advanced = container.querySelector(".builder-advanced");
  const confirmButton = q("confirm");
  const cancelButton = q("cancel");
  let files = [];
  let estimateValid = false;
  let runningJobId = null;
  let hostIdManuallyEdited = false;
  const editor = createConfigEditor(q("config"), {
    config,
    surface: "host_build",
    baseline: state.globalOverrides,
    values: state.globalOverrides,
    extraBox: true,
    onChange: invalidateEstimate,
  });

  function invalidateEstimate() {
    estimateValid = false;
    confirmButton.disabled = true;
    q("estimate-view").classList.add("estimate-stale");
  }

  function syncHostIdentity({reset = false} = {}) {
    const generated = hostIdFromDisplayName(displayNameInput.value);
    if (reset || !hostIdManuallyEdited) hostIdInput.value = generated;
    versionInput.placeholder = hostIdInput.value
      ? `留空则使用 ${hostIdInput.value}@v1` : "留空则使用 <host_id>@v1";
  }

  async function readFiles(selected) {
    const decoder = new TextDecoder("utf-8", {fatal: true});
    const rows = [];
    for (const file of selected) {
      if (!file.name.toLocaleLowerCase().endsWith(".txt")) {
        throw new Error(`只支持 .txt：${file.name}`);
      }
      let content;
      try { content = decoder.decode(await file.arrayBuffer()); }
      catch (_error) { throw new Error(`不是有效 UTF-8：${file.name}`); }
      rows.push({name: file.name, content, size: file.size});
    }
    return rows;
  }

  function renderFiles() {
    q("files").innerHTML = files.length
      ? `<ul class="plain-list">${files.map((file, index) => `<li><span>${index + 1}. ${esc(file.name)}</span>
        <span>${Number(file.size).toLocaleString()} bytes</span></li>`).join("")}</ul>`
      : "尚未选择文件。";
  }

  function payload(confirm) {
    const seedRaw = container.querySelector("#builder-seed").value.trim();
    return {
      files: files.map(({name, content}) => ({name, content})),
      host_id: hostIdInput.value.trim(),
      display_name: displayNameInput.value.trim(),
      version: versionInput.value.trim() || null,
      piece_max_tokens: Number(container.querySelector("#builder-piece-tokens").value),
      seed: seedRaw ? Number(seedRaw) : null,
      config_overrides: mergeOverrides(state.globalOverrides, editor.collect()),
      confirm,
    };
  }

  function validateInputs() {
    const displayName = displayNameInput.value.trim();
    const hostId = hostIdInput.value.trim();
    const pieceMaxTokens = Number(container.querySelector("#builder-piece-tokens").value);
    const seedRaw = container.querySelector("#builder-seed").value.trim();
    if (!displayName) {
      setMsg(q("msg"), "请填写 Host 名称。", "err");
      displayNameInput.focus();
      return false;
    }
    if (!files.length) {
      setMsg(q("msg"), "请选择至少一个 UTF-8 .txt 文件。", "err");
      fileInput.focus();
      return false;
    }
    if (!HOST_ID_PATTERN.test(hostId) || WINDOWS_RESERVED_HOST_ID.test(hostId)) {
      advanced.open = true;
      setMsg(q("msg"), "唯一标识须为 1–64 位小写字母、数字或连字符，且不能使用系统保留名。", "err");
      hostIdInput.focus();
      return false;
    }
    if (!Number.isInteger(pieceMaxTokens) || pieceMaxTokens < 1) {
      advanced.open = true;
      setMsg(q("msg"), "piece token 上限须为正整数。", "err");
      container.querySelector("#builder-piece-tokens").focus();
      return false;
    }
    if (seedRaw && !Number.isInteger(Number(seedRaw))) {
      advanced.open = true;
      setMsg(q("msg"), "seed 须为整数，或留空使用稳定派生值。", "err");
      container.querySelector("#builder-seed").focus();
      return false;
    }
    return true;
  }

  fileInput.addEventListener("change", async () => {
    try {
      files = await readFiles([...fileInput.files]);
      renderFiles();
      setMsg(q("msg"), files.length ? `已读取 ${files.length} 个 UTF-8 文件。` : "", "");
    } catch (error) {
      files = [];
      fileInput.value = "";
      renderFiles();
      setMsg(q("msg"), error.message, "err");
    }
    invalidateEstimate();
  });
  displayNameInput.addEventListener("input", () => syncHostIdentity());
  hostIdInput.addEventListener("input", () => {
    hostIdManuallyEdited = hostIdInput.value !== hostIdFromDisplayName(displayNameInput.value);
    syncHostIdentity();
  });
  q("reset-id").addEventListener("click", () => {
    hostIdManuallyEdited = false;
    syncHostIdentity({reset: true});
    invalidateEstimate();
  });
  form.querySelectorAll("input:not([type=file])").forEach((input) =>
    input.addEventListener("input", invalidateEstimate));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!validateInputs()) return;
    try {
      q("estimate").disabled = true;
      setMsg(q("msg"), "正在执行确定性准备与 prompt 预检…", "");
      const response = await post("/api/hosts/build", payload(false));
      renderEstimate(q("estimate-view"), response.estimate);
      estimateValid = true;
      confirmButton.disabled = false;
      setMsg(q("msg"), "估算完成。确认构建前请检查位置样本与 route。", "ok");
    } catch (error) {
      invalidateEstimate();
      setMsg(q("msg"), error.message, "err");
    } finally { q("estimate").disabled = false; }
  });

  confirmButton.addEventListener("click", async () => {
    if (!estimateValid) return;
    try {
      confirmButton.disabled = true;
      q("estimate").disabled = true;
      setMsg(q("msg"), "正在重新验证并提交后台任务…", "");
      const response = await post("/api/hosts/build", payload(true));
      runningJobId = response.job_id;
      cancelButton.classList.remove("is-hidden");
      setMsg(q("msg"), `任务 ${response.job_id} 已提交；固定两次串行调用。`, "ok");
      watchJob(response.job_id, async (job) => {
        runningJobId = null;
        cancelButton.classList.add("is-hidden");
        if (job.state === "succeeded") {
          selectedHostId = job.result.host_id;
          await refreshHosts();
          navigate("/hosts");
          return;
        }
        q("estimate").disabled = false;
        const report = `runs/${job.run_id}/host_builder/report.md`;
        setMsg(q("msg"), `${job.error || `任务 ${job.state}`}\nBuilder run: ${job.run_id}\n报告：${report}`, "err");
      });
    } catch (error) {
      q("estimate").disabled = false;
      confirmButton.disabled = !estimateValid;
      setMsg(q("msg"), error.message, "err");
    }
  });
  cancelButton.addEventListener("click", async () => {
    if (!runningJobId) return;
    cancelButton.disabled = true;
    try {
      await post(`/api/jobs/${encodeURIComponent(runningJobId)}/cancel`, {});
      setMsg(q("msg"), "已请求取消；若调用尚未开始，将在下一调用前停止。", "warn");
    } catch (error) { setMsg(q("msg"), error.message, "err"); }
    finally { cancelButton.disabled = false; }
  });
}

function renderEstimate(target, estimate) {
  const positions = ["opening", "middle", "ending"];
  const examples = positions.map((position) => {
    const rows = (estimate.selected_examples || []).filter((row) => row.position === position);
    return `<section class="sample-position"><h3>${esc(position)} <span>${rows.length} / 2</span></h3>
      ${rows.map((row) => `<article><div class="kv">${esc(row.file_name)} · ~${esc(row.estimated_tokens)} tokens</div>
        <p>${esc(row.text)}</p></article>`).join("")}</section>`;
  }).join("");
  const excluded = estimate.excluded_files?.length
    ? `<details><summary>排除文件 ${estimate.excluded_files.length}</summary><ul>
      ${estimate.excluded_files.map((row) => `<li>${esc(row.file_name)}：${esc(row.reason)}</li>`).join("")}</ul></details>` : "";
  target.classList.remove("estimate-stale");
  target.innerHTML = `<div class="panel estimate-panel"><div class="eyebrow">ESTIMATE</div>
      <div class="estimate-metrics">
        <div><strong>${esc(estimate.calls)}</strong><span>固定调用</span></div>
        <div><strong>~${Number(estimate.est_input_tokens).toLocaleString()}</strong><span>输入 tokens</span></div>
        <div><strong>${esc(estimate.eligible_files)} / ${esc(estimate.files)}</strong><span>有效文件</span></div>
        <div><strong>${esc(estimate.dominant_script)}</strong><span>主导脚本</span></div>
      </div>
      <dl class="summary-list builder-effective">
        <div><dt>Host 名称</dt><dd>${esc(estimate.display_name)}</dd></div>
        <div><dt>Host ID</dt><dd><code>${esc(estimate.host_id)}</code></dd></div>
        <div><dt>内容版本</dt><dd>${esc(estimate.version)}</dd></div>
        <div><dt>采样设置</dt><dd>piece ${esc(estimate.piece_max_tokens)} · seed ${esc(estimate.seed)}</dd></div>
      </dl>
      <div class="kv estimate-routes">Profile: ${esc(estimate.models?.host_profile_distill)}<br>
        Examples: ${esc(estimate.models?.host_style_example_reverse)}</div>
      ${excluded}<h2>位置样本预览</h2><div class="sample-grid">${examples}</div>
      <div class="banner neutral">参考稿全文只用于本次本地准备和模型调用；run、catalog 与 job result 不保存上传正文。</div>
    </div>`;
}
