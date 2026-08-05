// 2s job poller. The jobbox lives in the sidebar head; `watchJob` keeps the
// old per-view completion hook, and every running->terminal transition emits
// "job-finished" so the sidebar/run views can refresh themselves.

import { api } from "./api.js";
import { $ } from "./dom.js";
import { emit } from "./state.js";

let watchedJob = null;
let jobDoneHook = null;
const prevStates = new Map();
const ACTIVE = new Set(["running", "pending"]);

export async function pollJobs() {
  let jobs;
  try {
    jobs = await api("/api/jobs");
  } catch (e) {
    return; // server briefly unavailable
  }
  for (const job of jobs) {
    const prev = prevStates.get(job.job_id);
    if (!ACTIVE.has(job.state) && ACTIVE.has(prev)) emit("job-finished", job);
    prevStates.set(job.job_id, job.state);
  }

  const active = jobs.find((j) => ACTIVE.has(j.state));
  const shown = active || jobs.find((j) => j.job_id === watchedJob) || jobs[0];
  const box = $("jobbox");
  if (!shown) { box.textContent = "idle"; box.className = ""; return; }
  const p = shown.progress || {};
  const prog = p.total ? ` ${p.done}/${p.total}` : (p.done ? ` ${p.done}` : "");
  box.textContent = `${shown.kind} ${shown.state}${prog} ${p.message || ""} → ${shown.run_id}`;
  box.className = shown.state;
  box.title = JSON.stringify(shown, null, 2);

  if (watchedJob && shown.job_id === watchedJob && !ACTIVE.has(shown.state)) {
    const hook = jobDoneHook;
    watchedJob = null;
    jobDoneHook = null;
    if (hook) hook(shown);
  }
}

export function watchJob(jobId, onDone) {
  watchedJob = jobId;
  jobDoneHook = onDone;
  pollJobs();
}

export function startJobPolling() {
  setInterval(pollJobs, 2000);
  pollJobs();
}
