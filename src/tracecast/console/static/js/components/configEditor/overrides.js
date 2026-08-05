// Small, DOM-agnostic helpers for producing minimal ConfigOverrides payloads.

export const sameJson = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export function setNested(root, path, value) {
  let cursor = root;
  path.slice(0, -1).forEach((key) => cursor = cursor[key] ||= {});
  cursor[path[path.length - 1]] = value;
}

export function deleteNested(root, path) {
  let cursor = root;
  for (const key of path.slice(0, -1)) {
    if (!cursor || !(key in cursor)) return;
    cursor = cursor[key];
  }
  delete cursor[path[path.length - 1]];
}

export function pruneEmpty(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  Object.keys(value).forEach((key) => {
    pruneEmpty(value[key]);
    if (value[key] && typeof value[key] === "object" && !Array.isArray(value[key])
        && !Object.keys(value[key]).length) delete value[key];
  });
  return value;
}

export function lookupOverride(overrides, kind, owner, key) {
  if (!overrides) return undefined;
  const group = kind === "budget" ? overrides.stage_budgets : overrides.routes;
  return group?.[owner]?.[key];
}

export function inputValue(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.value.trim() === "") return null;
  if (input.type === "number") {
    const value = Number(input.value);
    if (!Number.isFinite(value)) throw new Error(`${input.dataset.key} 不是有效数字`);
    return value;
  }
  if (input.dataset.valueType === "json") {
    const value = JSON.parse(input.value);
    if (!value || Array.isArray(value) || typeof value !== "object")
      throw new Error(`${input.dataset.key} 必须是 JSON object`);
    return value;
  }
  return input.value;
}
