// Popup: copy ONE paste-able blob — the active tab's context plus a pointer to
// the skill (the how-to-drive-me instructions the bridge serves at /skill).

const DEFAULT_PORT = 8765;
const REPO_SKILL = "~/clawd/clawd-harness/projects/clawd-browser-extension/extension/skill.txt";

async function getPort() {
  const { port } = await chrome.storage.local.get("port");
  return port || DEFAULT_PORT;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

function contextText(tab, port) {
  return [
    "You can drive my real, logged-in browser (clawd-browser). I'm talking about this tab:",
    `  tab_id: ${tab.id}`,
    `  title: ${tab.title || "(untitled)"}`,
    `  url: ${tab.url || "(no url)"}`,
    "",
    "If you have mcp__clawd-browser__* tools, use them directly — pass tab_id " + tab.id,
    "to target this tab (browser_read, browser_screenshot, browser_click, …).",
    "Otherwise, learn the API first — read the skill:",
    `  curl -s http://127.0.0.1:${port}/skill`,
    `  (or Read the file at ${REPO_SKILL} — it also covers starting the bridge)`,
    `If tab_id ${tab.id} no longer resolves, find the url above with browser_tabs.`,
  ].join("\n");
}

async function skillText(port) {
  const text = await fetch(chrome.runtime.getURL("skill.txt")).then((r) => r.text());
  return port === DEFAULT_PORT ? text : text.replaceAll(String(DEFAULT_PORT), String(port));
}

async function copyContext() {
  const [tab, port] = await Promise.all([activeTab(), getPort()]);
  // No readable active tab (rare) — fall back to the full skill so the paste still works.
  const text = tab ? contextText(tab, port) : await skillText(port);
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Fallback for when the popup doesn't hold clipboard permission via the API.
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  document.getElementById("copied").classList.add("show");
}

async function showTab() {
  const tab = await activeTab();
  const el = document.getElementById("tab-line");
  if (tab) el.textContent = "▸ " + (tab.title || tab.url || "(untitled)");
}

function setStatus(id, ok, okText, badText) {
  document.getElementById(id + "-dot").className = "dot " + (ok ? "ok" : "bad");
  document.getElementById(id + "-txt").textContent = ok ? okText : badText;
}

async function checkBridge() {
  const port = await getPort();
  try {
    const r = await fetch(`http://127.0.0.1:${port}/status`, { signal: AbortSignal.timeout(2000) });
    const s = await r.json();
    setStatus("bridge", true, `bridge: up on :${port}`, "");
    setStatus("ext", !!s.extension_connected, "extension link: connected", "extension link: not connected");
  } catch {
    setStatus("bridge", false, "", `bridge: not running on :${port}`);
    setStatus("ext", false, "", "extension link: n/a");
  }
}

document.getElementById("copy").addEventListener("click", copyContext);
checkBridge();
showTab();
copyContext(); // opening the popup counts as the click — copy immediately
