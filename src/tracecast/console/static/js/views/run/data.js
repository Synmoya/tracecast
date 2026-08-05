// Per-run data cache shared by the run sub-views (single-run cache: switching
// runs or a finished job invalidates it).

import { api } from "../../api.js";
import { STAGES } from "../../labels.js";

let cache = {runId: null};

export function invalidateRun(runId = null) {
  if (runId === null || cache.runId === runId) cache = {runId: null};
}

function forRun(runId) {
  if (cache.runId !== runId) cache = {runId};
  return cache;
}

export async function getInventory(runId) {
  const c = forRun(runId);
  if (!c.inventory) c.inventory = await api(`/api/runs/${encodeURIComponent(runId)}`);
  return c.inventory;
}

export async function getCalls(runId) {
  const c = forRun(runId);
  if (!c.calls) c.calls = await api(`/api/runs/${encodeURIComponent(runId)}/llm/calls`);
  return c.calls;
}

export async function getChunks(runId) {
  const c = forRun(runId);
  if (!c.chunks) c.chunks = await api(`/api/runs/${encodeURIComponent(runId)}/chunks`);
  return c.chunks;
}

export async function getStageReports(runId) {
  const c = forRun(runId);
  if (!c.reports) {
    c.reports = Object.fromEntries(await Promise.all(STAGES.map(async (stage) => {
      const report = await api(
        `/api/runs/${encodeURIComponent(runId)}/reports/${encodeURIComponent(stage)}`);
      return [stage, report];
    })));
  }
  return c.reports;
}

export async function getScript(runId) {
  const c = forRun(runId);
  if (!c.script) {
    c.script = await api(`/api/runs/${encodeURIComponent(runId)}/artifact`
      + `?relpath=${encodeURIComponent("assembly/script.md")}`);
  }
  return c.script;
}
