// Popup: copy the active tab's context (or the skill prompt) + show bridge health.

const DEFAULT_PORT = 8765;

async function getPort() {
  const { port } = await chrome.storage.local.get("port");
  return port || DEFAULT_PORT;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab || null;
}

function tabContextText(tab) {
  return [
    "I'm talking about this specific browser tab — drive it with the clawd-browser tools:",
    `  tab_id: ${tab.id}`,
    `  title: ${tab.title || "(untitled)"}`,
    `  url: ${tab.url || "(no url)"}`,
    `Pass tab_id ${tab.id} to any browser_* tool (browser_read, browser_screenshot, browser_click, …).`,
    "If that tab_id no longer resolves, find this URL with browser_tabs.",
  ].join("\n");
}

async function skillText() {
  const [text, port] = await Promise.all([
    fetch(chrome.runtime.getURL("skill.txt")).then((r) => r.text()),
    getPort(),
  ]);
  return port === DEFAULT_PORT ? text : text.replaceAll(String(DEFAULT_PORT), String(port));
}

async function copyText(text, note) {
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
  const el = document.getElementById("copied");
  el.textContent = "✓ " + note;
  el.classList.add("show");
}

async function copyTabContext() {
  const tab = await activeTab();
  if (!tab) {
    await copySkill();
    return;
  }
  await copyText(tabContextText(tab), "Tab context copied — paste it so Claude knows which tab you mean.");
}

async function copySkill() {
  await copyText(await skillText(), "Skill prompt copied — paste it into any Claude session.");
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

document.getElementById("copy-tab").addEventListener("click", copyTabContext);
document.getElementById("copy-skill").addEventListener("click", copySkill);
checkBridge();
showTab();
copyTabContext(); // opening the popup counts as the click — copy the tab context immediately
