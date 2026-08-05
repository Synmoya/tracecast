// Hash router: deep-linkable views (#/run/<id>/pipeline/planning). Dynamic
// segments are encodeURIComponent-ed by link builders and decoded here.

import { $, esc } from "./dom.js";

const routes = [];
let currentPathValue = "";

export function register(pattern, handler) {
  routes.push({pattern, handler});
}

export function navigate(path) {
  if (location.hash === "#" + path) dispatch();
  else location.hash = path;
}

export const currentPath = () => currentPathValue;

export function redispatch() { dispatch(); }

export function start() {
  window.addEventListener("hashchange", dispatch);
  dispatch();
}

async function dispatch() {
  const path = location.hash.replace(/^#/, "") || "/new";
  currentPathValue = path;
  for (const {pattern, handler} of routes) {
    const m = pattern.exec(path);
    if (!m) continue;
    const args = m.slice(1).map((v) => (v === undefined ? v : decodeURIComponent(v)));
    try {
      await handler(...args);
    } catch (e) {
      console.error(e);
      $("view").innerHTML =
        `<div class="panel"><h2>加载失败</h2><div class="msg err">${esc(e.message)}</div></div>`;
    }
    window.dispatchEvent(new CustomEvent("route-changed", {detail: {path}}));
    return;
  }
  location.hash = "#/new";
}
