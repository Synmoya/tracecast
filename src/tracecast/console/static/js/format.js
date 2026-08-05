// Display formatting; all "—" fallbacks live here.

export function fmtNumber(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v !== "number") return String(v);
  return Number.isInteger(v) ? v.toLocaleString("en-US") : String(v);
}

export function fmtPercent(v, digits = 1) {
  if (v === null || v === undefined) return "—";
  return (v * 100).toFixed(digits) + "%";
}

export function fmtCompact(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
  const number = Number(v);
  if (Math.abs(number) < 1000) return fmtNumber(number);
  const units = [[1_000_000_000, "B"], [1_000_000, "M"], [1_000, "k"]];
  const [scale, suffix] = units.find(([threshold]) => Math.abs(number) >= threshold);
  const scaled = number / scale;
  return `${scaled.toFixed(scaled >= 100 ? 0 : digits).replace(/\.0$/, "")}${suffix}`;
}

export function fmtTokens(v) {
  const compact = fmtCompact(v);
  return compact === "—" ? compact : `${compact} tokens`;
}

export function distText(d) {
  if (!d || d.median === null || d.median === undefined) return "—";
  return `中位 ${fmtNumber(d.median)} · P90 ${fmtNumber(d.p90)} · 均值 ${fmtNumber(d.avg)}`
    + ` · 范围 ${fmtNumber(d.min)}–${fmtNumber(d.max)}`;
}

// ISO timestamp -> "MM-DD HH:MM" (sidebar) / "YYYY-MM-DD HH:MM:SS" (detail).
export const fmtTimeShort = (iso) => (iso ? iso.slice(5, 16).replace("T", " ") : "");
export const fmtTimeFull = (iso) => (iso ? iso.slice(0, 19).replace("T", " ") : "");
export const fmtTimeClock = (iso) => (iso ? iso.slice(11, 19) : "");
