// Schema-driven config editor orchestrator. Parameter markup lives in
// configEditor/fields.js; override-tree math and validation stay separately testable.

import { parseJsonBox } from "../dom.js";
import { renderBudgetGroups, renderRoutes } from "./configEditor/fields.js";
import {
  deleteNested, inputValue, pruneEmpty, sameJson, setNested,
} from "./configEditor/overrides.js";
import { validateEditor } from "./configEditor/validation.js";

export function createConfigEditor(container, {
  config,
  surface = null,
  baseline = null,
  values = null,
  extraBox = true,
  onChange = null,
}) {
  const schema = config.editor;
  const fits = (surfaces) => surface === null || surfaces.includes(surface);
  const fields = schema.budget_fields.filter((field) => fits(field.surfaces));
  const routes = schema.routes.filter((route) => fits(route.surfaces));
  const progressive = surface === null;
  const stages = [...new Set(routes.map((route) => route.stage))].sort();

  container.innerHTML = (fields.length
    ? `<div class="config-heading"><h3>阶段预算</h3><span class="kv">按阶段展开</span></div>`
      + `${renderBudgetGroups(fields, baseline, values, {collapsible: progressive})}` : "")
    + `<div class="config-group config-routes"><div class="config-heading"><h3>Routes</h3>`
    + `<span class="kv"><b data-config-routes-count>0</b> 项修改</span></div>`
    + (progressive && routes.length ? `<div class="config-route-filters">
        <input type="search" data-config-route-search placeholder="搜索 route…" autocomplete="off">
        <select data-config-route-filter><option value="">全部阶段</option>
          ${stages.map((stage) => `<option value="${stage}">${stage}</option>`).join("")}</select>
      </div>` : "")
    + `${renderRoutes(routes, baseline, values, {managed: progressive}) || '<div class="kv">无</div>'}</div>`
    + (extraBox
      ? `<details><summary>额外 JSON override（实验逃生口）</summary>`
        + `<textarea data-config-extra rows="3"></textarea></details>`
      : "")
    + `<details class="config-preview"><summary>最终 override 预览</summary>`
    + `<pre data-config-preview>{}</pre></details>`;

  const extra = container.querySelector("[data-config-extra]");
  const preview = container.querySelector("[data-config-preview]");

  function resetInputs(inputs) {
    inputs.forEach((input) => {
      const base = JSON.parse(input.dataset.base);
      if (input.type === "checkbox") input.checked = Boolean(base);
      else if (input.dataset.valueType === "json") input.value = JSON.stringify(base ?? {});
      else input.value = base ?? "";
    });
    changed();
  }

  function refreshChangedCounts() {
    container.querySelectorAll("input[data-config-kind]").forEach((input) => {
      let differs = false;
      try { differs = !sameJson(inputValue(input), JSON.parse(input.dataset.base)); }
      catch (_error) { differs = true; }
      input.closest(".config-field")?.classList.toggle("is-changed", differs);
    });
    container.querySelectorAll("[data-config-group]").forEach((group) => {
      const count = group.querySelectorAll(".config-field.is-changed").length;
      const target = group.querySelector(":scope > summary [data-config-count]");
      if (target) target.textContent = count;
    });
    container.querySelectorAll("[data-config-route-row]").forEach((route) => {
      const count = route.querySelectorAll(".config-field.is-changed").length;
      const target = route.querySelector("summary [data-config-count]");
      if (target) target.textContent = count;
    });
    const total = container.querySelectorAll("[data-config-route-row] .config-field.is-changed").length;
    const target = container.querySelector("[data-config-routes-count]");
    if (target) target.textContent = total;
  }

  function filterRoutes() {
    const needle = container.querySelector("[data-config-route-search]")?.value
      .trim().toLocaleLowerCase() ?? "";
    const stage = container.querySelector("[data-config-route-filter]")?.value ?? "";
    container.querySelectorAll("[data-config-route-row]").forEach((route) => {
      route.classList.toggle("is-hidden", Boolean(
        (needle && !route.dataset.configRouteName.includes(needle))
        || (stage && route.dataset.configRouteStage !== stage)
      ));
    });
  }

  function collect() {
    const merged = JSON.parse(JSON.stringify((extra && parseJsonBox(extra)) || {}));
    container.querySelectorAll("input[data-config-kind]").forEach((input) => {
      const value = inputValue(input);
      const base = JSON.parse(input.dataset.base);
      const path = input.dataset.configKind === "budget"
        ? ["stage_budgets", input.dataset.bucket, input.dataset.key]
        : ["routes", input.dataset.route, input.dataset.key];
      setNested(merged, path, value);
      if (sameJson(value, base)) deleteNested(merged, path);
    });
    pruneEmpty(merged);
    validateEditor(container);
    return Object.keys(merged).length ? merged : null;
  }

  function changed() {
    refreshChangedCounts();
    try {
      const value = collect();
      if (preview) preview.textContent = JSON.stringify(value || {}, null, 2);
    } catch (error) {
      if (preview) preview.textContent = `配置错误: ${error.message}`;
    }
    if (onChange) onChange();
  }

  container.querySelectorAll("input[data-config-kind]").forEach((input) =>
    input.addEventListener("input", changed));
  container.querySelectorAll("[data-config-reset-group]").forEach((button) =>
    button.addEventListener("click", () => resetInputs([
      ...button.closest("[data-config-group]").querySelectorAll("input[data-config-kind]"),
    ])));
  container.querySelectorAll("[data-config-reset-route]").forEach((button) =>
    button.addEventListener("click", () => resetInputs([
      ...button.closest("[data-config-route-row]").querySelectorAll("input[data-config-kind]"),
    ])));
  container.querySelector("[data-config-route-search]")?.addEventListener("input", filterRoutes);
  container.querySelector("[data-config-route-filter]")?.addEventListener("change", filterRoutes);
  if (extra) extra.addEventListener("input", changed);
  changed();
  return {collect};
}
