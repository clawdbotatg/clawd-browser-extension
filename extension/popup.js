// Popup: copy the skill prompt to the clipboard + show bridge health.

const DEFAULT_PORT = 8765;

async function getPort() {
  const { port } = await chrome.storage.local.get("port");
  return port || DEFAULT_PORT;
}

async function skillText() {
  const [text, port] = await Promise.all([
    fetch(chrome.runtime.getURL("skill.txt")).then((r) => r.text()),
    getPort(),
  ]);
  return port === DEFAULT_PORT ? text : text.replaceAll(String(DEFAULT_PORT), String(port));
}

async function copySkill() {
  const text = await skillText();
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

document.getElementById("copy").addEventListener("click", copySkill);
checkBridge();
copySkill(); // opening the popup counts as the click — copy immediately
