// DOM helpers. Views render HTML strings and bind listeners afterwards —
// no inline on* attributes (ES modules do not create globals).

export const $ = (id) => document.getElementById(id);

export const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

export function setMsg(target, text, cls) {
  const el = typeof target === "string" ? $(target) : target;
  if (!el) return;
  el.textContent = text;
  el.className = "msg " + (cls || "");
}

// Event delegation: survives innerHTML re-renders inside `root`.
export function delegate(root, event, selector, handler) {
  root.addEventListener(event, (ev) => {
    const match = ev.target.closest(selector);
    if (match && root.contains(match)) handler(match, ev);
  });
}

export const lines = (textarea) =>
  textarea.value.split("\n").map((s) => s.trim()).filter(Boolean);

export async function copyText(text) {
  await navigator.clipboard.writeText(String(text));
}

export function parseJsonBox(textarea) {
  const raw = textarea.value.trim();
  return raw ? JSON.parse(raw) : null;
}
