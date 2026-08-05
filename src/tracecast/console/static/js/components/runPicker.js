// run-id text input backed by the shared <datalist id="run-list">.

import { esc } from "../dom.js";
import { runById } from "../state.js";

export function runPickerHtml(id, value = "") {
  return `<input type="text" id="${esc(id)}" list="run-list" value="${esc(value)}"`
    + ` placeholder="run_id" autocomplete="off">`;
}

// Read the picked run id; warn=true tolerates ids the cache does not know
// (e.g. a run created seconds ago) — the API is the final validator anyway.
export function pickedRun(input) {
  const runId = input.value.trim();
  if (!runId) throw new Error("请输入 run_id");
  if (!runById(runId)) console.warn(`run ${runId} 不在缓存列表中(可能刚创建)`);
  return runId;
}
