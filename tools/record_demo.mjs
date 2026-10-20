/**
 * Reusable capture of the ordinary Console, using examples/console-case.
 * Requires Node.js, Playwright, installed Chrome/Chromium, and FFmpeg.
 * Only local GET requests; no model calls or DOM content/style replacements.
 * Raw takes and MP4 stay under ignored tmp/. See docs/user/console-case.md.
 */
import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdir, readdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = { url: "http://127.0.0.1:8770/", "case-dir": "examples/console-case",
  "output-dir": "tmp/showcase/console-case-v2", browser: "chrome", ffmpeg: "ffmpeg" };
let preview = false;
for (let i = 2; i < process.argv.length; i++) {
  const option = process.argv[i];
  if (option === "--help") {
    console.log("node tools/record_demo.mjs [--url URL] [--case-dir DIR] [--output-dir DIR]"
      + " [--playwright-dir NODE_MODULES] [--browser chrome|chromium] [--ffmpeg PATH]"
      + " [--screenshots-only]");
    process.exit(0);
  }
  if (option === "--screenshots-only") { preview = true; continue; }
  if (!/^--(?:url|case-dir|output-dir|playwright-dir|browser|ffmpeg)$/.test(option)
      || !process.argv[i + 1]) throw new Error(`Unknown or incomplete option: ${option}`);
  args[option.slice(2)] = process.argv[++i];
}
const target = new URL(args.url);
if (!["127.0.0.1", "localhost", "[::1]"].includes(target.hostname)) {
  throw new Error("Record only a loopback Console.");
}
if (!["chrome", "chromium"].includes(args.browser)) throw new Error("Unsupported browser.");
const caseRoot = path.resolve(root, args["case-dir"]);
const fixture = JSON.parse(await readFile(path.join(caseRoot, "case.json"), "utf8"));
if (fixture.complete_run_included !== false || fixture.saved_request_authoritative !== true) {
  throw new Error("Expected the documented historical excerpt.");
}
const { local_run_id: runId, chunk_id: chunkId, call_id: callId } = fixture;
if (![runId, chunkId, callId].every((v) => typeof v === "string" && v)) {
  throw new Error("Case identifiers are missing.");
}
const require = createRequire(args["playwright-dir"]
  ? path.join(path.resolve(args["playwright-dir"]), "__recording__.cjs") : import.meta.url);
let chromium;
try { ({ chromium } = require("playwright")); }
catch { throw new Error("Install Playwright with npm install --no-save --prefix"
  + " tmp/showcase-tools playwright ; pass --playwright-dir tmp/showcase-tools/node_modules"); }
async function filesUnder(directory) {
  const result = [];
  for (const item of await readdir(directory, { withFileTypes: true })) {
    if (item.name === "__pycache__" || item.name.endsWith(".pyc")) continue;
    const name = path.join(directory, item.name);
    if (item.isDirectory()) result.push(...await filesUnder(name));
    else if (item.isFile()) result.push(name);
  }
  return result;
}
async function treeHash(entries) {
  const files = [];
  for (const entry of entries) {
    const name = path.resolve(root, entry);
    files.push(...(await stat(name)).isDirectory() ? await filesUnder(name) : [name]);
  }
  const hash = createHash("sha256");
  for (const name of files.sort()) {
    hash.update(path.relative(root, name).replaceAll("\\", "/") + "\0");
    hash.update(await readFile(name)); hash.update("\0");
  }
  return hash.digest("hex");
}
const codeEntries = ["src/tracecast", "prompt", "tools/prepare_console_case.py", "tools/record_demo.mjs"];
const sourceHash = await treeHash(codeEntries), caseHash = await treeHash([caseRoot]);
const baseCommit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
const out = path.resolve(root, args["output-dir"]);
await mkdir(out, { recursive: true });
const take = path.join(out, `take-${new Date().toISOString().replaceAll(/[:.]/g, "-")}`);
await mkdir(take);
const browser = await chromium.launch({
  ...(args.browser === "chrome" ? { channel: "chrome" } : {}), headless: true,
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: "light",
  ...(preview ? {} : { recordVideo: { dir: take, size: { width: 1440, height: 900 } } }),
});
const errors = [], blocked = [];
await context.route("**/*", (route) => {
  const r = route.request();
  if (new URL(r.url()).origin === target.origin
      && ["GET", "HEAD", "OPTIONS"].includes(r.method())) return route.continue();
  blocked.push({ method: r.method(), url: r.url() }); return route.abort();
});
const page = await context.newPage(), video = page.video(), began = performance.now(), marks = [];
page.on("pageerror", (error) => errors.push(error.message));
const stamp = (label, caption = label) => marks.push({
  label, caption, url: page.url(), seconds: (performance.now() - began) / 1000,
});
const hold = (seconds) => page.waitForTimeout(preview ? 150 : seconds * 1000);
const click = async (locator) => {
  await locator.hover();
  await hold(.35);
  await locator.click();
};
let browserVersion, technical, cutStart;
try {
  const response = await context.request.get(
    new URL(`/api/runs/${runId}/trace/rendering/${chunkId}`, target).href);
  if (!response.ok()) throw new Error(`Trace preflight failed: ${response.status()}`);
  const trace = await response.json();
  technical = trace.technical;
  if (!technical.recorded_request_matches_hash
      || !trace.call_links.some((call) => call.call_id === callId)) {
    throw new Error("Saved request does not match the case call/hash.");
  }
  // Enter through the same root URL printed by the Console. Everything after
  // this navigation uses ordinary controls, with no hidden route jumps.
  await page.goto(target.origin + "/", { waitUntil: "domcontentloaded" });
  const caseLink = page.locator(`#run-list-nav a[data-run="${runId}"]`);
  await caseLink.waitFor({ state: "visible" });
  await page.locator(".page-title").filter({ hasText: "新建运行" }).waitFor();
  await page.evaluate(() => document.fonts.ready);
  if (await page.locator("html").getAttribute("data-theme") !== "light") throw new Error("Wrong theme.");
  browserVersion = browser.version();
  cutStart = (performance.now() - began) / 1000;
  stamp("Console 首页", "打开 Console 首页；从左侧选择 FF6 历史摘录");
  await page.screenshot({ path: path.join(out, "00-entry.png") });
  await hold(1.5);
  await click(caseLink);
  await page.locator(".run-tab.active").filter({ hasText: "流程" }).waitFor();
  stamp("选择历史案例", "这是第 1/50 段历史摘录；点击「诊断」");
  await hold(1.5);
  await click(page.locator(`.run-tabs a[href="#/run/${runId}/diagnostics"]`));
  await page.locator('[data-tr="chunk"]').waitFor({ state: "visible" });
  stamp("进入阶段追踪", "在「阶段追踪」选择第 1 段，再点击「载入追查」");
  await hold(1);
  await page.locator('[data-tr="chunk"]').selectOption(chunkId);
  await hold(.7);
  await click(page.locator('[data-tr="load"]'));
  await page.locator('[data-stage="rendering"]').waitFor({ state: "visible" });
  await hold(.7);
  await click(page.locator('[data-stage="rendering"]'));
  await page.waitForFunction(() =>
    document.querySelector('[data-tr="compiled"]')?.textContent.includes('"text"'));
  stamp("历史分段与实际输入", "rendering：左侧是历史输入，右侧是保存的分段正文");
  await page.locator('[data-tr="output"]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, "01-rendering.png") });
  await hold(3);
  await click(page.locator('[data-stage="packing"]'));
  const materials = page.locator(".blocked").filter({ hasText: "{{materials}}" }).locator("textarea");
  await materials.waitFor({ state: "visible" });
  stamp("对应材料", "packing：向下找到 materials，滚动到原材料的故事开场");
  await hold(1);
  // Retain the default control sizes; scroll as a viewer would.
  await materials.scrollIntoViewIfNeeded();
  await materials.hover();
  await hold(.7);
  await page.mouse.wheel(0, 900);
  await page.waitForTimeout(400);
  await page.screenshot({ path: path.join(out, "02-materials.png") });
  await hold(3);
  await click(page.locator('[data-tool="calls"]'));
  const callRow = page.locator(`tr[data-call="${callId}"]`);
  await callRow.waitFor({ state: "visible" });
  stamp("选择实际调用", "点击「模型调用」，再点这一分段对应的调用行");
  await hold(1.5);
  await click(callRow);
  const raw = page.locator("#raw-pre");
  await page.waitForFunction(() =>
    document.querySelector("#raw-pre")?.textContent.includes("=== response ==="));
  await page.locator("#raw-panel").scrollIntoViewIfNeeded();
  await page.waitForTimeout(250);
  stamp("保存的实际请求", "查看保存的 request；浏览这些记录不调用模型");
  await page.screenshot({ path: path.join(out, "03-call.png") });
  await hold(3);
  // Locate the response in the existing text, then reach it by wheel input.
  const responseOffset = await raw.evaluate((element) => {
    const index = element.textContent.indexOf("=== response ===");
    if (index < 0 || element.firstChild?.nodeType !== Node.TEXT_NODE) {
      throw new Error("The saved response marker is missing.");
    }
    const range = document.createRange();
    range.setStart(element.firstChild, index);
    range.setEnd(element.firstChild, index + "=== response ===".length);
    return range.getBoundingClientRect().top - element.getBoundingClientRect().top - 12;
  });
  stamp("保存的实际响应", "向下滚到 response；历史 prompt 为 v16，当前为 v20");
  await raw.hover();
  for (let step = 0; step < 4; step++) {
    await page.mouse.wheel(0, responseOffset / 4);
    await page.waitForTimeout(150);
  }
  await page.waitForFunction(() => {
    const element = document.querySelector("#raw-pre");
    const index = element?.textContent.indexOf("=== response ===") ?? -1;
    if (index < 0) return false;
    const range = document.createRange();
    range.setStart(element.firstChild, index);
    range.setEnd(element.firstChild, index + "=== response ===".length);
    const marker = range.getBoundingClientRect(), panel = element.getBoundingClientRect();
    return marker.top >= panel.top && marker.bottom <= panel.bottom;
  });
  await page.screenshot({ path: path.join(take, "response-check.png") });
  await hold(3.5);
  stamp("结束");
  if (errors.length || blocked.length) throw new Error(JSON.stringify({ errors, blocked }));
} finally { await context.close(); await browser.close(); }
if (await treeHash(codeEntries) !== sourceHash || await treeHash([caseRoot]) !== caseHash) {
  throw new Error("Code or case changed during capture; retain this take and record again.");
}
const relative = (value) => path.relative(root, value).replaceAll("\\", "/");
const receipt = {
  date: new Date().toISOString(), base_commit: baseCommit,
  source_tree_sha256: sourceHash, case_tree_sha256: caseHash, case_version: fixture.version,
  original_run_id: fixture.original_run_id, local_run_id: runId, chunk_id: chunkId, call_id: callId,
  browser: `${args.browser} ${browserVersion}`, viewport: [1440, 900], device_scale_factor: 1,
  theme: "light", entry_url: target.origin + "/",
  scenario: "home → select case → diagnostics → rendering → packing materials → calls → saved request → saved response",
  historical_request_check: technical, blocked_requests: blocked, page_errors: errors,
  screenshots: ["00-entry.png", "01-rendering.png", "02-materials.png", "03-call.png"], code_hash_paths: codeEntries,
  timeline: marks.map((m) => ({ ...m, seconds: +(m.seconds - cutStart).toFixed(3) })),
};
if (!preview) {
  const rawVideo = path.join(take, "capture.webm");
  await rename(await video.path(), rawVideo);
  function srtTime(seconds) {
    const ms = Math.max(0, Math.round(seconds * 1000));
    return `${String(Math.floor(ms / 3600000)).padStart(2, "0")}:`
      + `${String(Math.floor(ms / 60000) % 60).padStart(2, "0")}:`
      + `${String(Math.floor(ms / 1000) % 60).padStart(2, "0")},`
      + String(ms % 1000).padStart(3, "0");
  }
  const captions = marks.slice(0, -1).map((mark, i) => [String(i + 1),
    `${srtTime(marks[i].seconds - cutStart)} --> ${srtTime(marks[i + 1].seconds - cutStart - .05)}`,
    mark.caption, ""].join("\n")).join("\n");
  const subtitlePath = path.join(out, "tracecast-console-case.srt");
  await writeFile(subtitlePath, captions, "utf8");
  const output = path.join(out, "tracecast-console-case.mp4");
  const duration = marks.at(-1).seconds - cutStart;
  const encoded = spawnSync(args.ffmpeg, [
    "-hide_banner", "-loglevel", "error", "-y", "-ss", cutStart.toFixed(3), "-i", rawVideo,
    "-i", subtitlePath, "-map", "0:v:0", "-map", "1:0", "-c:v", "libx264", "-crf", "20",
    "-pix_fmt", "yuv420p", "-r", "30", "-an", "-c:s", "mov_text",
    "-metadata:s:s:0", "language=zho", "-disposition:s:0", "default",
    "-t", duration.toFixed(3), "-movflags", "+faststart", output,
  ], { encoding: "utf8" });
  if (encoded.error || encoded.status !== 0) {
    throw new Error(`FFmpeg failed; raw take retained: ${encoded.error?.message || encoded.stderr}`);
  }
  Object.assign(receipt, {
    encoder: execFileSync(args.ffmpeg, ["-version"], { encoding: "utf8" }).split(/\r?\n/)[0],
    video: relative(output), subtitles: relative(subtitlePath), raw_take: relative(rawVideo),
    trim_start_seconds: +cutStart.toFixed(3), duration_seconds: +duration.toFixed(3),
    encoding: "H.264 CRF 20; yuv420p; 30 fps; no audio; optional Chinese subtitles; faststart",
    video_sha256: createHash("sha256").update(await readFile(output)).digest("hex"),
  });
}
await writeFile(path.join(out, preview ? "preview.json" : "recording.json"),
  JSON.stringify(receipt, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ output: relative(out), preview, sourceHash, caseHash, browserVersion }));
