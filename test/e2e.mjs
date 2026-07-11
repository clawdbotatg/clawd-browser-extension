// End-to-end test: real Chromium + extension + bridge + MCP server.
//
// Uses the playwright-core install cached on this machine (no deps in this
// repo). Launches a throwaway Chromium profile with extension/ loaded, starts
// bridge.py, serves a local test page, then drives everything through the
// bridge's HTTP API and finally through mcp_server.py over stdio — the exact
// path a Claude Code session uses.
//
// Run: node test/e2e.mjs

import { createRequire } from "module";
import { spawn } from "child_process";
import { once } from "events";
import fs from "fs";
import http from "http";
import os from "os";
import path from "path";
import readline from "readline";
import { fileURLToPath } from "url";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PW_BASE = process.env.PLAYWRIGHT_CORE_DIR || "/Users/clawd/clawd-harness/tools";
const require = createRequire(path.join(PW_BASE, "x.js"));
const { chromium } = require("playwright-core");

// Not 8765: the live bridge + real Chrome extension own that port, and the
// bridge holds ONE extension slot — on a shared port the test extension and
// the real one steal it from each other every reconnect. The test extension
// still dials 8765 once at startup (storage isn't set yet); that transient
// drop of the real extension self-heals in ~3s.
const BRIDGE_PORT = +(process.env.CLAWD_BROWSER_PORT || 8766);
const PAGE_PORT = 8123;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failures = 0;
function check(name, cond, detail = "") {
  const mark = cond ? "PASS" : "FAIL";
  if (!cond) failures++;
  console.log(`  [${mark}] ${name}${cond ? "" : "  — " + detail}`);
}

function findChromium() {
  const cache = path.join(os.homedir(), "Library/Caches/ms-playwright");
  const dirs = fs.readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d)).sort();
  if (!dirs.length) throw new Error("no cached playwright chromium found");
  const base = path.join(cache, dirs[dirs.length - 1]);
  for (const sub of ["chrome-mac-arm64", "chrome-mac"]) {
    const p = path.join(base, sub);
    if (!fs.existsSync(p)) continue;
    const app = fs.readdirSync(p).find((f) => f.endsWith(".app"));
    if (app) {
      const bin = path.join(p, app, "Contents/MacOS", app.replace(/\.app$/, ""));
      if (fs.existsSync(bin)) return bin;
    }
  }
  throw new Error("chromium binary not found under " + base);
}

async function cmd(cmdName, args = {}, timeout = 30) {
  const resp = await fetch(`http://127.0.0.1:${BRIDGE_PORT}/cmd`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd: cmdName, args, timeout }),
  });
  return resp.json();
}

const TEST_PAGE = `<!doctype html>
<title>Clawd E2E Test Page</title>
<h1>Hello from the clawd e2e test page</h1>
<div id="count">0</div>
<button id="btn" onclick="document.getElementById('count').textContent = +document.getElementById('count').textContent + 1">bump</button>
<form onsubmit="event.preventDefault(); document.getElementById('submitted').textContent='yes'">
  <input id="inp" type="text">
  <span id="submitted">no</span>
</form>
<script>console.log("page-loaded-marker");
setTimeout(() => { const d = document.createElement("div"); d.id = "late"; d.textContent = "late-elem"; document.body.appendChild(d); }, 1200);
</script>`;

async function main() {
  const cleanup = [];
  process.on("exit", () => cleanup.forEach((f) => { try { f(); } catch {} }));

  // -- local test page server
  const pageServer = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(TEST_PAGE);
  });
  pageServer.listen(PAGE_PORT, "127.0.0.1");
  cleanup.push(() => pageServer.close());
  const PAGE_URL = `http://127.0.0.1:${PAGE_PORT}/`;

  // -- bridge
  console.log("starting bridge.py ...");
  const bridge = spawn("python3", [path.join(ROOT, "bridge.py")], {
    stdio: ["ignore", "inherit", "inherit"],
    env: { ...process.env, CLAWD_BROWSER_PORT: String(BRIDGE_PORT) },
  });
  cleanup.push(() => bridge.kill());
  for (let i = 0; ; i++) {
    try {
      const s = await (await fetch(`http://127.0.0.1:${BRIDGE_PORT}/status`)).json();
      if (s.ok) break;
    } catch {}
    if (i > 30) throw new Error("bridge never came up");
    await sleep(200);
  }

  // -- chromium with the extension
  console.log("launching chromium with extension ...");
  const profile = path.join(ROOT, "test/tmp-profile");
  fs.rmSync(profile, { recursive: true, force: true });
  const extPath = path.join(ROOT, "extension");
  const ctx = await chromium.launchPersistentContext(profile, {
    executablePath: findChromium(),
    headless: true,
    args: [`--disable-extensions-except=${extPath}`, `--load-extension=${extPath}`],
  });
  cleanup.push(() => ctx.close().catch(() => {}));

  // Point the test extension's service worker at OUR bridge port (it defaults
  // to 8765 = the live bridge). Its 3s reconnect loop re-reads storage, so it
  // lands on our port within a few seconds.
  let sw = ctx.serviceWorkers()[0];
  if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 });
  await sw.evaluate(async (port) => {
    await chrome.storage.local.set({ port });
    // If it already latched onto the default port, kick it loose — connect()
    // no-ops while a socket is open, so it would never re-read storage.
    try { ws && ws.close(); } catch {}
  }, BRIDGE_PORT);

  // -- wait for the extension to dial in
  let connected = false;
  for (let i = 0; i < 100; i++) {
    const s = await (await fetch(`http://127.0.0.1:${BRIDGE_PORT}/status`)).json();
    if (s.extension_connected) { connected = true; break; }
    await sleep(200);
  }
  console.log("\n== bridge/extension ==");
  check("extension connected to bridge", connected);
  if (!connected) { process.exitCode = 1; return; }

  // -- drive it over the bridge HTTP API
  const open = await cmd("open", { url: PAGE_URL });
  check("open tab", open.ok && open.result.loaded, JSON.stringify(open));
  const tabId = open.ok ? open.result.tab_id : null;

  const tabs = await cmd("tabs");
  check("tabs lists our tab", tabs.ok && tabs.result.tabs.some((t) => t.tab_id === tabId), JSON.stringify(tabs).slice(0, 300));

  const read = await cmd("read", { tab_id: tabId });
  check("read page text", read.ok && read.result.text.includes("Hello from the clawd e2e test page"), JSON.stringify(read).slice(0, 300));

  await cmd("click", { tab_id: tabId, selector: "#btn" });
  await cmd("click", { tab_id: tabId, selector: "#btn" });
  const count = await cmd("eval", { tab_id: tabId, code: "document.getElementById('count').textContent" });
  check("trusted clicks bump counter to 2", count.ok && count.result.value === "2", JSON.stringify(count));

  const badSel = await cmd("click", { tab_id: tabId, selector: "#nope" });
  check("click on missing selector errors cleanly", !badSel.ok && /selector not found/.test(badSel.error || ""), JSON.stringify(badSel));

  await cmd("type", { tab_id: tabId, selector: "#inp", text: "hi from clawd", submit: true });
  const inp = await cmd("eval", { tab_id: tabId, code: "JSON.stringify({v: document.getElementById('inp').value, s: document.getElementById('submitted').textContent})" });
  const parsed = inp.ok ? JSON.parse(inp.result.value) : {};
  check("type into input", parsed.v === "hi from clawd", JSON.stringify(inp));
  check("Enter submitted the form", parsed.s === "yes", JSON.stringify(parsed));

  const shot = await cmd("screenshot", { tab_id: tabId });
  const png = shot.ok ? Buffer.from(shot.result.data, "base64") : Buffer.alloc(0);
  check("screenshot is a real PNG", png.length > 1000 && png.subarray(1, 4).toString() === "PNG", `len=${png.length}`);

  await cmd("eval", { tab_id: tabId, code: "console.log('marker-xyz-123'); 1" });
  const cons = await cmd("console", { tab_id: tabId });
  check("console capture", cons.ok && cons.result.entries.some((e) => e.text.includes("marker-xyz-123")), JSON.stringify(cons).slice(0, 300));

  const promise = await cmd("eval", { tab_id: tabId, code: "new Promise(r => setTimeout(() => r('resolved!'), 100))" });
  check("eval awaits promises", promise.ok && promise.result.value === "resolved!", JSON.stringify(promise));

  // -- v0.2.0 commands: js-targeted click + wait_for + version
  console.log("\n== v0.2.0 commands ==");
  const ver = await cmd("version");
  check("version command", ver.ok && ver.result.version === "0.2.0", JSON.stringify(ver));

  const jsClick = await cmd("click", { tab_id: tabId, js: "[...document.querySelectorAll('button')].find(b => b.innerText.trim() === 'bump')" });
  check("click by js expression echoes element", jsClick.ok && jsClick.result.element?.tag === "button" && jsClick.result.element?.text === "bump", JSON.stringify(jsClick));
  const count3 = await cmd("eval", { tab_id: tabId, code: "document.getElementById('count').textContent" });
  check("js click bumped counter to 3", count3.ok && count3.result.value === "3", JSON.stringify(count3));

  const jsBad = await cmd("click", { tab_id: tabId, js: "42" });
  check("click js non-element errors cleanly", !jsBad.ok && /did not return a DOM Element/.test(jsBad.error || ""), JSON.stringify(jsBad));

  const wfNow = await cmd("wait_for", { tab_id: tabId, selector: "#btn" });
  check("wait_for present selector is immediate", wfNow.ok && wfNow.result.ready === true && wfNow.result.waited_ms < 1000, JSON.stringify(wfNow));

  const wfLate = await cmd("wait_for", { tab_id: tabId, selector: "#late", timeout_ms: 5000 });
  check("wait_for catches late-added element", wfLate.ok && wfLate.result.ready === true, JSON.stringify(wfLate));

  const wfJs = await cmd("wait_for", { tab_id: tabId, js: "document.getElementById('count').textContent === '3' ? { count: 3 } : null" });
  check("wait_for js returns the truthy value", wfJs.ok && wfJs.result.ready === true && wfJs.result.value?.count === 3, JSON.stringify(wfJs));

  const wfTimeout = await cmd("wait_for", { tab_id: tabId, selector: "#never", timeout_ms: 700 });
  check("wait_for timeout is ready:false, not an error", wfTimeout.ok && wfTimeout.result.ready === false && wfTimeout.result.waited_ms >= 700, JSON.stringify(wfTimeout));

  // -- now the full MCP stdio path
  console.log("\n== mcp server (stdio) ==");
  const mcp = spawn("python3", [path.join(ROOT, "mcp_server.py")], {
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, CLAWD_BROWSER_PORT: String(BRIDGE_PORT) },
  });
  cleanup.push(() => mcp.kill());
  const rl = readline.createInterface({ input: mcp.stdout });
  const pending = new Map();
  rl.on("line", (line) => {
    try {
      const msg = JSON.parse(line);
      if (msg.id != null && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    } catch {}
  });
  let nextId = 1;
  const rpc = (method, params = {}) => {
    const id = nextId++;
    const p = new Promise((resolve, reject) => {
      pending.set(id, resolve);
      setTimeout(() => reject(new Error(`rpc timeout: ${method}`)), 30000);
    });
    mcp.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return p;
  };

  const init = await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "e2e", version: "0" } });
  check("initialize", init.result?.serverInfo?.name === "clawd-browser", JSON.stringify(init));
  mcp.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");

  const list = await rpc("tools/list");
  check("tools/list has 12 tools", list.result?.tools?.length === 12, JSON.stringify(list.result?.tools?.map((t) => t.name)));
  check("tools/list includes browser_wait_for", list.result?.tools?.some((t) => t.name === "browser_wait_for"), "");

  const call = await rpc("tools/call", { name: "browser_read", arguments: { tab_id: tabId } });
  const text = call.result?.content?.[0]?.text || "";
  check("tools/call browser_read", text.includes("Hello from the clawd e2e test page"), text.slice(0, 200));

  const shot2 = await rpc("tools/call", { name: "browser_screenshot", arguments: { tab_id: tabId } });
  const img = shot2.result?.content?.[0];
  check("tools/call browser_screenshot returns image content", img?.type === "image" && img?.mimeType === "image/png" && img?.data?.length > 1000, JSON.stringify(shot2).slice(0, 200));

  const bad = await rpc("tools/call", { name: "browser_eval", arguments: { tab_id: tabId, code: "throw new Error('boom')" } });
  check("tool error surfaces as isError", bad.result?.isError === true && /boom/.test(bad.result?.content?.[0]?.text || ""), JSON.stringify(bad).slice(0, 200));

  const wfCall = await rpc("tools/call", { name: "browser_wait_for", arguments: { tab_id: tabId, selector: "h1" } });
  check("tools/call browser_wait_for", /"ready": true/.test(wfCall.result?.content?.[0]?.text || ""), JSON.stringify(wfCall).slice(0, 200));

  const jsCall = await rpc("tools/call", { name: "browser_click", arguments: { tab_id: tabId, js: "document.getElementById('btn')" } });
  check("tools/call browser_click with js", /"tag": "button"/.test(jsCall.result?.content?.[0]?.text || ""), JSON.stringify(jsCall).slice(0, 200));

  console.log(`\n${failures === 0 ? "ALL PASS" : failures + " FAILURE(S)"}`);
  process.exitCode = failures === 0 ? 0 : 1;
}

main()
  .catch((e) => {
    console.error("e2e crashed:", e);
    process.exitCode = 1;
  })
  .finally(() => setTimeout(() => process.exit(), 500));
