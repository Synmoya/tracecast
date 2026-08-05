// Entry: route table + sidebar + job polling + initial data loads.

import { $ } from "./dom.js";
import {
  on, refreshConfig, refreshHosts, refreshProjects, refreshRuns, refreshSetup, state,
} from "./state.js";
import { currentPath, redispatch, register, start } from "./router.js";
import { startJobPolling } from "./jobs.js";
import { initSidebar } from "./views/sidebar.js";
import { initTheme } from "./theme.js";
import { renderConfig } from "./views/config.js";
import { renderNewRun } from "./views/newRun.js";
import { renderHosts } from "./views/hosts.js";
import { renderRunShell } from "./views/run/shell.js";
import { renderSetup } from "./views/setup.js";
import { renderNewProject } from "./views/newProject.js";
import { invalidateRun } from "./views/run/data.js";

register(/^\/config$/, () => renderConfig($("view")));
register(/^\/setup$/, () => renderSetup($("view")));
register(/^\/hosts$/, () => renderHosts($("view")));
register(/^\/hosts\/build$/, () => renderHosts($("view"), {mode: "build"}));
register(/^\/new$/, () => renderNewRun($("view")));
register(/^\/new\/project$/, () => renderNewProject($("view")));
register(/^\/run\/([^/]+)$/, (runId) => renderRunShell(runId));
register(/^\/run\/([^/]+)\/([a-z-]+)(?:\/(.+))?$/,
  (runId, sub, rest) => renderRunShell(runId, sub, rest));

// A finished job means new/changed artifacts: refresh the sidebar, and if the
// affected run is on screen, drop its cache and re-render in place.
on("job-finished", (job) => {
  refreshRuns().catch(console.error);
  const match = /^\/run\/([^/]+)/.exec(currentPath());
  if (match && decodeURIComponent(match[1]) === job.run_id) {
    invalidateRun(job.run_id);
    redispatch();
  }
});

async function initialize() {
  initTheme();
  initSidebar();
  startJobPolling();
  await refreshSetup();
  await Promise.all([refreshProjects(), refreshHosts(), refreshRuns()]);
  if (state.setup.runtime.configured) await refreshConfig();
  if (!state.setup.runtime.configured
      && location.hash.replace(/^#/, "") !== "/setup") location.hash = "#/setup";
  start();
}

initialize().catch((error) => {
  console.error(error);
  $("view").innerHTML = `<div class="panel"><h2>Console 启动失败</h2>`
    + `<div class="msg err">${error.message}</div></div>`;
});
