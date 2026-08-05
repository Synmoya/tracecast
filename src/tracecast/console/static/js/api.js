// fetch wrappers; every endpoint speaks JSON and errors carry {detail}.

export async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(`${res.status} ${body.detail || JSON.stringify(body)}`);
  return body;
}

export const post = (path, payload) => api(path, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify(payload),
});

export const put = (path, payload) => api(path, {
  method: "PUT",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify(payload),
});

export function qs(params) {
  const pairs = Object.entries(params)
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
  return pairs.length ? "?" + pairs.join("&") : "";
}
