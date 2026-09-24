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
const TOKEN = "e2e-test-token-not-secret";
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
    env: { ...process.env, CLAWD_BROWSER_PORT: String(BRIDGE_PORT), CLAWD_BROWSER_TOKEN: TOKEN },
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
    // Re-kick while waiting: if the first kick ran mid-dial (ws still null),
    // the sw latches onto the live 8765 bridge and connect() no-ops forever.
    // Only close a socket pointed at the WRONG port, never our own.
    if (i % 5 === 4) {
      await sw.evaluate((port) => {
        try { if (ws && !ws.url.includes(`:${port}/`)) ws.close(); } catch {}
      }, BRIDGE_PORT);
    }
    await sleep(200);
  }
  console.log("\n== bridge/extension ==");
  check("extension connected to bridge", connected);
  if (!connected) { process.exitCode = 1; return; }

  // GET /skill serves the instructions, port-substituted for non-default ports.
  const skill = await (await fetch(`http://127.0.0.1:${BRIDGE_PORT}/skill`)).text();
  check(
    "GET /skill serves port-substituted skill.txt",
    skill.includes("clawd-browser") && skill.includes(`:${BRIDGE_PORT}/cmd`) && !skill.includes("8765"),
    skill.slice(0, 200),
  );

  // -- LAN face: another machine dials the box's LAN address and must carry the
  // token in the path; loopback stays token-free. We stand in for the other
  // machine by dialing our own LAN IP (not loopback), which the bridge treats
  // exactly like a remote peer.
  console.log("\n== LAN / token ==");
  const status = await (await fetch(`http://127.0.0.1:${BRIDGE_PORT}/status`)).json();
  const lanHost = status.lan?.hosts?.[0];
  check("GET /status (loopback) reports lan hosts + token url", !!lanHost && status.lan.urls[0] === `http://${lanHost}:${BRIDGE_PORT}/k/${TOKEN}`, JSON.stringify(status.lan));
  if (lanHost) {
    const LAN = `http://${lanHost}:${BRIDGE_PORT}`;
    const noTok = await fetch(`${LAN}/status`);
    check("LAN /status without token → 403", noTok.status === 403, String(noTok.status));
    const badTok = await fetch(`${LAN}/k/wrong-token/status`);
    check("LAN /status with wrong token → 403", badTok.status === 403, String(badTok.status));
    const noTokCmd = await fetch(`${LAN}/cmd`, { method: "POST", body: JSON.stringify({ cmd: "tabs" }) });
    check("LAN /cmd without token → 403", noTokCmd.status === 403, String(noTokCmd.status));
    const okTok = await (await fetch(`${LAN}/k/${TOKEN}/status`)).json();
    check("LAN /k/<token>/status → ok, extension visible", okTok.ok && okTok.extension_connected, JSON.stringify(okTok).slice(0, 200));
    const lanSkill = await (await fetch(`${LAN}/k/${TOKEN}/skill`)).text();
    check(
      "LAN /k/<token>/skill rewrites every example URL to the token url",
      lanSkill.includes(`${LAN}/k/${TOKEN}/cmd`) && !lanSkill.includes("127.0.0.1") && lanSkill.includes("another machine"),
      lanSkill.slice(0, 200),
    );
    const lanTabs = await (await fetch(`${LAN}/k/${TOKEN}/cmd`, { method: "POST", body: JSON.stringify({ cmd: "tabs" }) })).json();
    check("LAN /k/<token>/cmd tabs works", lanTabs.ok && Array.isArray(lanTabs.result.tabs), JSON.stringify(lanTabs).slice(0, 200));
  }
  check("loopback /skill mentions the LAN url", skill.includes("From another machine") && skill.includes(`/k/${TOKEN}`), skill.slice(-300));

  // -- drive it over the bridge HTTP API
  const open = await cmd("open", { url: PAGE_URL });
  check("open tab", open.ok && open.result.loaded, JSON.stringify(open));
  const tabId = open.ok ? open.result.tab_id : null;

  // active:false opens in the background: the active tab must not change
  const bg = await cmd("open", { url: PAGE_URL + "?bg", active: false });
  const afterBg = await cmd("tabs");
  const bgTab = bg.ok && afterBg.ok ? afterBg.result.tabs.find((t) => t.tab_id === bg.result.tab_id) : null;
  const fgTab = afterBg.ok ? afterBg.result.tabs.find((t) => t.tab_id === tabId) : null;
  check("open active:false stays in the background", bg.ok && bg.result.loaded && bgTab && !bgTab.active && fgTab && fgTab.active, JSON.stringify({ bg, bgTab, fgTab }).slice(0, 300));
  if (bg.ok) await cmd("close_tab", { tab_id: bg.result.tab_id });

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
  check("version command", ver.ok && ver.result.version === JSON.parse(fs.readFileSync(path.join(ROOT, "extension/manifest.json"), "utf8")).version, JSON.stringify(ver));

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

  const wfGone = await cmd("wait_for", { tab_id: 999999999, selector: "#x", timeout_ms: 10000 });
  check("wait_for on a vanished tab fails fast", !wfGone.ok && /no longer exists/.test(wfGone.error || ""), JSON.stringify(wfGone));

  // -- now the full MCP stdio path
  console.log("\n== mcp server (stdio) ==");
  // Point it at the LAN token URL when we have one — the exact config a session
  // on another machine uses (CLAWD_BROWSER_URL); else the loopback default.
  const mcpUrl = lanHost ? `http://${lanHost}:${BRIDGE_PORT}/k/${TOKEN}` : `http://127.0.0.1:${BRIDGE_PORT}`;
  console.log(`  (mcp_server.py → ${mcpUrl})`);
  const mcp = spawn("python3", [path.join(ROOT, "mcp_server.py")], {
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, CLAWD_BROWSER_PORT: String(BRIDGE_PORT), CLAWD_BROWSER_URL: mcpUrl },
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
  check("tools/list has 15 tools", list.result?.tools?.length === 15, JSON.stringify(list.result?.tools?.map((t) => t.name)));
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
