// Sidebar: tool links (根配置 / 新建 run) + searchable run history grouped as a
// lineage tree via console_manifest.base_run_id (missing/broken parents degrade
// to roots — never throw). Also feeds the shared <datalist id="run-list">.

import { $, esc } from "../dom.js";
import { runDisplay } from "../runIdentity.js";
import { on, refreshRuns, state } from "../state.js";

let search = "";

function statusDot(manifest) {
  const status = manifest?.status;
  if (!status) return "";
  const cls = status === "complete" ? "ok"
    : (status === "failed" || status === "cancelled") ? "err" : "warn";
  return `<span class="dot ${cls}" title="${esc(status)}"></span>`;
}

// runs (newest first) -> [{run, depth}] rows: roots keep API order, children
// chronological under their base, indentation capped at depth 3.
function treeRows(runs) {
  const byId = new Map(runs.map((r) => [r.run_id, r]));
  const children = new Map();
  const roots = [];
  for (const run of runs) {
    const parent = run.console_manifest?.base_run_id;
    if (parent && byId.has(parent)) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(run);
    } else roots.push(run);
  }
  for (const list of children.values()) {
    list.sort((a, b) => a.modified_at.localeCompare(b.modified_at));
  }
  const rows = [];
  const visit = (run, depth) => {
    rows.push({run, depth: Math.min(depth, 3)});
    for (const child of children.get(run.run_id) ?? []) visit(child, depth + 1);
  };
  for (const root of roots) visit(root, 0);
  return rows;
}

function rowHtml({run, depth}) {
  const manifest = run.console_manifest;
  const display = runDisplay(run);
  const time = manifest?.base_run_id ? display.clock : display.time;
  return `<a class="run-row" href="#/run/${encodeURIComponent(run.run_id)}"`
    + ` data-run="${esc(run.run_id)}" style="--run-depth:${depth}">`
    + `<span class="run-name">${esc(display.title)}</span>`
    + `<span class="meta">${statusDot(manifest)}`
    + `<span>${esc(display.operation || display.context)}</span>`
    + `<span>${esc(time)}</span></span></a>`;
}

function render() {
  const needle = search.toLowerCase();
  const runs = needle
    ? state.runs.filter((r) => r.run_id.toLowerCase().includes(needle)
      || (r.console_manifest?.kind ?? "").includes(needle)
      || (r.console_manifest?.run_label ?? "").toLowerCase().includes(needle)
      || (r.console_manifest?.project_id ?? "").toLowerCase().includes(needle))
    : state.runs;
  const rows = needle
    ? runs.map((run) => ({run, depth: 0}))  // filtered view is flat
    : treeRows(runs);
  $("run-list-nav").innerHTML = rows.map(rowHtml).join("")
    || `<div class="kv empty-state">无匹配 run</div>`;
  $("run-list").innerHTML = state.runs.map((r) =>
    `<option value="${esc(r.run_id)}">`).join("");
  markActive();
}

function markActive() {
  const path = location.hash.replace(/^#/, "") || "/new";
  const runMatch = /^\/run\/([^/]+)/.exec(path);
  const currentRun = runMatch ? decodeURIComponent(runMatch[1]) : null;
  document.querySelectorAll("#run-list-nav .run-row").forEach((row) =>
    row.classList.toggle("active", row.dataset.run === currentRun));
  document.querySelectorAll(".side-tools a").forEach((link) =>
    link.classList.toggle("active", link.getAttribute("href") === "#" + path));
}

export function initSidebar() {
  on("runs-changed", render);
  window.addEventListener("route-changed", markActive);
  $("runs-search").addEventListener("input", (ev) => {
    search = ev.target.value.trim();
    render();
  });
  $("runs-refresh").addEventListener("click", () =>
    refreshRuns().catch((e) => console.error(e)));
  render();
}
