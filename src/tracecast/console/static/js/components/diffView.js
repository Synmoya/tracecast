// Diff renderers (pure HTML builders), ported verbatim from the old single-file
// console: sentence-level side-by-side, unified diff lines, selection path diff.

import { esc } from "../dom.js";

export function renderTextDiff(d) {
  const cell = (cls, textList) => `<div class="${cls}">${textList.map(esc).join("<br>")}</div>`;
  const rows = d.ops.map((op) => {
    if (op.op === "equal") return cell("eq", op.a) + cell("eq", op.b);
    if (op.op === "delete") return cell("del", op.a) + `<div></div>`;
    if (op.op === "insert") return `<div></div>` + cell("add", op.b);
    return cell("del", op.a) + cell("add", op.b);
  }).join("");
  return `<div class="msg">相似度 ${d.ratio} | A ${d.chars_a}字/${d.sentences_a}句`
    + ` | B ${d.chars_b}字/${d.sentences_b}句</div><div class="sxs">${rows}</div>`;
}

export const udiff = (linesArr) => `<div>${linesArr.map((l) => {
  const cls = l.startsWith("+") ? "plus" : l.startsWith("-") ? "minus"
    : l.startsWith("@@") ? "hunk" : "";
  return `<div class="dline ${cls}">${esc(l)}</div>`;
}).join("")}</div>`;

function nodeRow(n, extra) {
  return `<tr><td class="id" title="${esc(n.candidate_id)}">${esc(n.candidate_id)}</td>`
    + `<td>${esc(n.summary ?? "")}</td><td>${extra}</td></tr>`;
}

export function renderSelectionDiff(d) {
  const section = (title, rows) => rows.length
    ? `<h3>${title}(${rows.length})</h3><div class="table-wrap"><table><tbody>${rows.join("")}</tbody></table></div>`
    : "";
  return `<div class="msg">A=${esc(d.plan_a.plan_id)}(${d.plan_a.selected} 选中`
    + `) | B=${esc(d.plan_b.plan_id)}(${d.plan_b.selected} 选中`
    + `) | 未变 ${d.unchanged}</div>`
    + section("新增(B 有 A 无)", d.added.map((n) => nodeRow(n,
      `pos_b=${n.position_b} ${esc(n.role ?? "")}/${esc(n.treatment ?? "")}`)))
    + section("移除(A 有 B 无)", d.removed.map((n) => nodeRow(n, `pos_a=${n.position_a}`)))
    + section("移动", d.moved.map((n) => nodeRow(n, `${n.position_a} → ${n.position_b}`)))
    + section("字段变化", d.changed.map((n) => nodeRow(n,
      Object.entries(n.fields).map(([k, v]) =>
        `${esc(k)}: ${esc(v[0] ?? "∅")} → ${esc(v[1] ?? "∅")}`).join("<br>"))));
}

export function renderBlocksDiff(d) {
  return d.blocks.map((b) =>
    `<details ${b.changed ? "open" : ""}><summary>{{${esc(b.key)}}} `
    + `${b.changed ? "已变化" : "一致"}</summary>`
    + (b.changed ? udiff(b.diff) : "") + `</details>`).join("");
}
