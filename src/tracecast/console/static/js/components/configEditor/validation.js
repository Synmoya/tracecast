// Cross-field constraints mirror the server-side config validation boundary.

import { inputValue } from "./overrides.js";

export function validateEditor(container) {
  const budget = {};
  container.querySelectorAll('input[data-config-kind="budget"]').forEach((input) => {
    const value = inputValue(input);
    if (value !== null) budget[input.dataset.key] = value;
    if (input.type === "number" && value !== null && input.min && value < Number(input.min))
      throw new Error(`${input.dataset.key} 必须 >= ${input.min}`);
    if (input.type === "number" && value !== null && input.max && value > Number(input.max))
      throw new Error(`${input.dataset.key} 必须 <= ${input.max}`);
  });
  if (budget.target_material_tokens > budget.max_material_tokens)
    throw new Error("target_material_tokens 不能大于 max_material_tokens");
  if (budget.fact_context_min_tokens > budget.fact_context_max_tokens)
    throw new Error("fact_context_min_tokens 不能大于 fact_context_max_tokens");
  if (budget.fact_context_ratio !== undefined
      && !(budget.fact_context_ratio > 0 && budget.fact_context_ratio <= 1))
    throw new Error("fact_context_ratio 必须在 (0, 1] 内");
  if (budget.prefer_node_boundary === false)
    throw new Error("v1 必须 prefer_node_boundary=true");

  const routes = {};
  container.querySelectorAll('input[data-config-kind="route"]').forEach((input) => {
    const value = inputValue(input);
    if (value !== null) (routes[input.dataset.route] ||= {})[input.dataset.key] = value;
    if (input.type === "number" && value !== null && input.min && value < Number(input.min))
      throw new Error(`${input.dataset.route}.${input.dataset.key} 必须 >= ${input.min}`);
    if (input.type === "number" && value !== null && input.max && value > Number(input.max))
      throw new Error(`${input.dataset.route}.${input.dataset.key} 必须 <= ${input.max}`);
  });
  Object.entries(routes).forEach(([name, route]) => {
    if (route.transport_backoff_base_seconds > route.transport_backoff_max_seconds)
      throw new Error(`${name}.transport_backoff_base_seconds 不能大于 transport_backoff_max_seconds`);
  });
}
