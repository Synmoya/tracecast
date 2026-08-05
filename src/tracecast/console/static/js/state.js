// Shared caches + a tiny pub-sub + the localStorage-backed global config
// overrides. Global overrides are a ConfigOverrides-shaped diff vs the runtime
// defaults; they are attached to every spend POST and are never written back to
// configs/runtime.json (the server-side in-memory-merge invariant holds).

import { api } from "./api.js";

const LS_KEY = "tracecast.console.global_overrides.v1";

export const state = {
  setup: null,
  projects: [],
  hosts: [],
  runs: [],
  config: null,
  globalOverrides: loadGlobalOverrides(),
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, []);
  listeners.get(event).push(fn);
}

export function emit(event, payload) {
  for (const fn of listeners.get(event) ?? []) {
    try { fn(payload); } catch (e) { console.error(e); }
  }
}

export async function refreshProjects() {
  state.projects = await api("/api/projects");
  emit("projects-changed");
}

export async function refreshSetup() {
  state.setup = await api("/api/setup");
  emit("setup-changed");
}

export async function refreshHosts() {
  state.hosts = await api("/api/hosts");
  emit("hosts-changed");
}

export async function refreshRuns() {
  state.runs = await api("/api/runs");
  emit("runs-changed");
}

export async function refreshConfig() {
  state.config = await api("/api/config");
  const removedRoutes = dropStaleOverrideRoutes();
  if (removedRoutes.length) {
    console.info(`已移除失效的全局 route overrides: ${removedRoutes.join(", ")}`);
  }
  emit("config-changed");
}

export const runById = (runId) => state.runs.find((r) => r.run_id === runId) ?? null;

// --- global overrides ---------------------------------------------------------

function normalizeOverrides(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {stage_budgets: {}, routes: {}};
  return {
    stage_budgets: raw.stage_budgets && typeof raw.stage_budgets === "object" ? raw.stage_budgets : {},
    routes: raw.routes && typeof raw.routes === "object" ? raw.routes : {},
  };
}

function loadGlobalOverrides() {
  try {
    return normalizeOverrides(JSON.parse(localStorage.getItem(LS_KEY) || "null"));
  } catch (e) {
    console.warn("丢弃损坏的全局 overrides", e);
    return {stage_budgets: {}, routes: {}};
  }
}

export function saveGlobalOverrides(overrides) {
  state.globalOverrides = normalizeOverrides(overrides);
  localStorage.setItem(LS_KEY, JSON.stringify(state.globalOverrides));
  emit("overrides-changed");
}

export function overridesLeafCount(overrides = state.globalOverrides) {
  let count = 0;
  for (const bucket of Object.values(overrides?.stage_budgets ?? {})) count += Object.keys(bucket).length;
  for (const route of Object.values(overrides?.routes ?? {})) count += Object.keys(route).length;
  return count;
}

export function outputLanguageChoices(preferred = []) {
  const values = [
    "zh-CN",
    "en-US",
    ...preferred,
    ...state.projects.map((item) => item.output_language),
    ...state.hosts.map((item) => item.output_language),
  ];
  return [...new Set(values.filter((value) => typeof value === "string" && value.trim()))];
}

// Deep-merge two ConfigOverrides payloads (bucket / route granularity, local
// leaf wins). Returns null when the merge is empty so spend payloads stay clean.
export function mergeOverrides(globalO, localO) {
  const merged = {stage_budgets: {}, routes: {}};
  for (const src of [globalO, localO]) {
    if (!src) continue;
    for (const [bucket, values] of Object.entries(src.stage_budgets ?? {}))
      merged.stage_budgets[bucket] = {...(merged.stage_budgets[bucket] ?? {}), ...values};
    for (const [route, values] of Object.entries(src.routes ?? {}))
      merged.routes[route] = {...(merged.routes[route] ?? {}), ...values};
  }
  if (!Object.keys(merged.stage_budgets).length) delete merged.stage_budgets;
  if (!Object.keys(merged.routes).length) delete merged.routes;
  return merged.stage_budgets || merged.routes ? merged : null;
}

// Overrides naming a route the runtime no longer declares would 400 every spend
// request server-side. refreshConfig removes them before any page can submit;
// the 根配置 page also keeps a defensive manual cleanup action.
export function staleOverrideRoutes() {
  if (!state.config) return [];
  const known = state.config.routes ?? {};
  return Object.keys(state.globalOverrides.routes ?? {}).filter((name) => !(name in known));
}

export function dropStaleOverrideRoutes() {
  const stale = staleOverrideRoutes();
  if (!stale.length) return [];
  const routes = {...state.globalOverrides.routes};
  for (const name of stale) delete routes[name];
  saveGlobalOverrides({...state.globalOverrides, routes});
  return stale;
}
