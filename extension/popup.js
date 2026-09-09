// Popup: copy ONE paste-able blob — the active tab's context plus a pointer to
// the skill (the how-to-drive-me instructions the bridge serves at /skill).
// Plus a quiet second act: open a harness session about this tab (link.js).
import { DEFAULT_PORT, DEFAULTS, contextText, composeUrl, sessionText } from "./link.js";

async function getPort() {
  const { port } = await chrome.storage.local.get("port");
  return port || DEFAULT_PORT;
}

async function getSettings() {
  const s = await chrome.storage.local.get(Object.keys(DEFAULTS));
  const out = { ...DEFAULTS };
  for (const k of Object.keys(DEFAULTS)) if (s[k]) out[k] = s[k];
  return out;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
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

// Open a harness session about the active tab. Reuses an open harness tab when
// there is one (a hash change is a same-document nav — no UI reboot, and on the
// fleet the tab's passkey login carries the spawn); else opens a new tab.
async function openSession() {
  const [tab, port, cfg] = await Promise.all([activeTab(), getPort(), getSettings()]);
  if (!tab) return;
  const url = composeUrl({
    harness: cfg.harness, project: cfg.project, machine: cfg.machine,
    text: sessionText(tab, port, cfg.opener), send: true,
  });
  const base = cfg.harness.replace(/\/+$/, "");
  const [existing] = await chrome.tabs.query({ url: base + "/*" });
  if (existing) {
    await chrome.tabs.update(existing.id, { url, active: true });
    await chrome.windows.update(existing.windowId, { focused: true });
  } else {
    await chrome.tabs.create({ url });
  }
  window.close();
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
document.getElementById("open").addEventListener("click", (e) => { e.preventDefault(); openSession(); });
document.getElementById("opts").addEventListener("click", (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
checkBridge();
showTab();
copyContext(); // opening the popup counts as the click — copy immediately
