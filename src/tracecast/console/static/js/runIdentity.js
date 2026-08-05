// Readable run identity for navigation. The stable run_id remains searchable
// and copyable, but never has to carry the visual hierarchy by itself.

import { RUN_KIND_LABELS } from "./labels.js";
import { fmtTimeClock, fmtTimeFull, fmtTimeShort } from "./format.js";
import { runById } from "./state.js";

const legacyName = (runId) => {
  if (runId.length <= 36) return runId;
  return `${runId.slice(0, 24)}…${runId.slice(-8)}`;
};

export function lineageRows(runId) {
  const rows = [];
  const seen = new Set();
  let current = runById(runId);
  while (current && !seen.has(current.run_id)) {
    seen.add(current.run_id);
    rows.push(current);
    current = current.console_manifest?.base_run_id
      ? runById(current.console_manifest.base_run_id) : null;
  }
  return rows;
}

export function rootRun(runId) {
  const rows = lineageRows(runId);
  return rows.length ? rows[rows.length - 1] : runById(runId);
}

export function projectIdForRun(runId, manifest = null) {
  const root = rootRun(runId);
  return root?.console_manifest?.project_id
    || manifest?.request?.project_id
    || manifest?.effective?.project_id
    || null;
}

export function runKindLabel(kind) {
  return RUN_KIND_LABELS[kind] || kind || "历史运行";
}

export function runDisplay(run) {
  const manifest = run?.console_manifest;
  if (!manifest) {
    return {
      title: legacyName(run.run_id),
      context: "历史运行",
      time: fmtTimeShort(run.modified_at),
    };
  }
  const isRoot = !manifest.base_run_id;
  const operation = runKindLabel(manifest.kind);
  const project = manifest.project_id || projectIdForRun(run.run_id);
  const title = manifest.run_label || (isRoot && project ? project : operation);
  return {
    title,
    context: isRoot && project ? operation : project,
    time: fmtTimeShort(manifest.created_at || run.modified_at),
    clock: fmtTimeClock(manifest.created_at || run.modified_at),
    fullTime: fmtTimeFull(manifest.created_at || run.modified_at),
    operation,
    project,
  };
}
