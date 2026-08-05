// Parameter display markup. Tune field grouping/copy here and visuals in forms.css.

import { esc } from "../../dom.js";
import { lookupOverride } from "./overrides.js";

function fieldInput(field, kind, owner, base, initial) {
  const data = `data-config-kind="${kind}" data-key="${esc(field.key)}" `
    + (kind === "budget" ? `data-bucket="${esc(owner)}" ` : `data-route="${esc(owner)}" `)
    + `data-base="${esc(JSON.stringify(base ?? null))}" data-value-type="${esc(field.type)}"`;
  if (field.type === "bool") {
    return `<input type="checkbox" ${data} ${initial ? "checked" : ""}`
      + ` ${field.readonly ? "disabled" : ""}>`;
  }
  const type = ["int", "float"].includes(field.type) ? "number" : "text";
  const step = field.type === "float" ? "any" : "1";
  const displayed = field.type === "json" ? JSON.stringify(initial ?? {}) : (initial ?? "");
  return `<input type="${type}" ${data} value="${esc(displayed)}" step="${step}"`
    + ` ${field.min !== null && field.min !== undefined ? `min="${field.min}"` : ""}`
    + ` ${field.max !== null && field.max !== undefined ? `max="${field.max}"` : ""}`
    + ` ${field.readonly ? "readonly" : ""}>`;
}

function renderField(field, kind, owner, baseline, values, optionalLabel = false) {
  const base = lookupOverride(baseline, kind, owner, field.key) ?? field.value;
  const initial = lookupOverride(values, kind, owner, field.key) ?? base;
  return `<div class="config-field"><label>${esc(field.key)}`
    + `${optionalLabel && field.optional ? "（可空）" : ""}</label>`
    + fieldInput(field, kind, owner, base, initial)
    + `<small>${esc(field.help)}`
    + `${field.override_path ? `<br>${esc(field.override_path)}` : ""}</small></div>`;
}

export function renderBudgetGroups(fields, baseline, values, {collapsible = false} = {}) {
  const groups = [...new Set(fields.map((field) => field.group))];
  return groups.map((group) => {
    const rows = fields.filter((field) => field.group === group);
    const body = `<div class="config-group-tools"><span class="kv">${rows.length} 项</span>`
      + `<button type="button" class="mini" data-config-reset-group="${esc(group)}">重置本组</button></div>`
      + `<div class="config-grid">${rows.map((field) =>
        renderField(field, "budget", field.bucket, baseline, values, true)).join("")}</div>`;
    if (!collapsible) return `<section class="config-group" data-config-group="${esc(group)}">
      <h3>${esc(group)}</h3>${body}</section>`;
    return `<details class="config-section" data-config-group="${esc(group)}">
      <summary><span>${esc(group)}</span><span class="config-summary-meta"><b data-config-count>0</b> 修改</span></summary>
      <div class="config-section-body">${body}</div></details>`;
  }).join("");
}

export function renderRoutes(routes, baseline, values, {managed = false} = {}) {
  return routes.map((route) => `<details class="config-route" data-config-route-row
      data-config-route-name="${esc(route.name.toLocaleLowerCase())}"
      data-config-route-stage="${esc(route.stage)}"><summary><span>${esc(route.name)}</span>
      <span class="config-summary-meta">${esc(route.stage)}${managed
        ? ` · <b data-config-count>0</b> 修改` : ""}</span></summary>`
    + (managed ? `<div class="config-group-tools"><span></span><button type="button" class="mini"
      data-config-reset-route="${esc(route.name)}">重置此 route</button></div>` : "")
    + `<div class="kv config-readonly">provider=${esc(route.readonly.provider)}`
    + ` · cost_profile=${esc(route.readonly.cost_profile ?? "—")}</div>`
    + `<div class="config-grid">${route.fields.map((field) =>
      renderField(field, "route", route.name, baseline, values)).join("")}</div></details>`)
    .join("");
}
