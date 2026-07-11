// Clawd Browser — MV3 service worker.
// Dials out to the local bridge (extensions can't listen) and executes commands
// via chrome.debugger (CDP): trusted input events, screenshots without focusing
// the tab, JS eval, console capture.

const DEFAULT_PORT = 8765;
const VERSION = "0.3.1";
const RECONNECT_MS = 3000;
const CONSOLE_MAX = 500;

let ws = null;
let connecting = false;

async function bridgeUrl() {
  const { port } = await chrome.storage.local.get("port");
  return `ws://127.0.0.1:${port || DEFAULT_PORT}/ext`;
}

async function connect() {
  if (connecting || (ws && ws.readyState <= WebSocket.OPEN)) return;
  connecting = true;
  try {
    const url = await bridgeUrl();
    const sock = new WebSocket(url);
    sock.onopen = () => {
      ws = sock;
      connecting = false;
      sock.send(JSON.stringify({ type: "hello", version: VERSION }));
    };
    sock.onmessage = (ev) => handleMessage(sock, ev.data);
    sock.onclose = () => {
      if (ws === sock) ws = null;
      connecting = false;
      setTimeout(connect, RECONNECT_MS);
    };
    sock.onerror = () => sock.close();
  } catch (e) {
    connecting = false;
    setTimeout(connect, RECONNECT_MS);
  }
}

// App-level ping: an active WebSocket keeps the service worker alive (Chrome 116+).
setInterval(() => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "ping" }));
  }
}, 20000);

// Backstop: alarms survive service-worker suspension and re-trigger connect.
chrome.alarms.create("reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.runtime.onInstalled.addListener(() => connect());
connect();

async function handleMessage(sock, data) {
  let msg;
  try {
    msg = JSON.parse(data);
  } catch {
    return;
  }
  if (msg.type === "pong") return;
  if (!msg.id || !msg.cmd) return;
  let reply;
  try {
    const result = await dispatch(msg.cmd, msg.args || {});
    reply = { id: msg.id, ok: true, result };
  } catch (e) {
    reply = { id: msg.id, ok: false, error: String((e && e.message) || e) };
  }
  if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(reply));
}

// ---------------------------------------------------------------- debugger plumbing

const attached = new Set(); // tabIds we hold a debugger session on
const consoleBuf = new Map(); // tabId -> [{ts, level, text}]

function cdp(tabId, method, params) {
  return chrome.debugger.sendCommand({ tabId }, method, params || {});
}

async function attach(tabId) {
  if (attached.has(tabId)) return;
  await chrome.debugger.attach({ tabId }, "1.3");
  attached.add(tabId);
  await cdp(tabId, "Runtime.enable");
  await cdp(tabId, "Page.enable");
}

function pushConsole(tabId, level, text) {
  let buf = consoleBuf.get(tabId);
  if (!buf) consoleBuf.set(tabId, (buf = []));
  buf.push({ ts: Date.now(), level, text });
  if (buf.length > CONSOLE_MAX) buf.splice(0, buf.length - CONSOLE_MAX);
}

function remoteObjToString(o) {
  if (!o) return "";
  if (o.value !== undefined) {
    return typeof o.value === "string" ? o.value : JSON.stringify(o.value);
  }
  return o.description || o.type || "";
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (source.tabId == null) return;
  if (method === "Runtime.consoleAPICalled") {
    const text = (params.args || []).map(remoteObjToString).join(" ");
    pushConsole(source.tabId, params.type || "log", text);
  } else if (method === "Runtime.exceptionThrown") {
    const d = params.exceptionDetails || {};
    const text = (d.exception && d.exception.description) || d.text || "uncaught exception";
    pushConsole(source.tabId, "error", text);
  }
});

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId != null) attached.delete(source.tabId);
});

chrome.tabs.onRemoved.addListener((tabId) => {
  attached.delete(tabId);
  consoleBuf.delete(tabId);
});

// ---------------------------------------------------------------- helpers

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function tabInfo(t) {
  return { tab_id: t.id, url: t.url, title: t.title, active: t.active, window_id: t.windowId, status: t.status };
}

async function resolveTab(a) {
  if (a.tab_id != null) return a.tab_id;
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tab) throw new Error("no active tab and no tab_id given");
  return tab.id;
}

function waitForLoad(tabId, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (loaded) => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(loaded);
    };
    const listener = (id, changeInfo) => {
      if (id === tabId && changeInfo.status === "complete") finish(true);
    };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then(
      (t) => t.status === "complete" && finish(true),
      () => finish(false)
    );
    setTimeout(() => finish(false), timeoutMs);
  });
}

async function evalInTab(tabId, expression) {
  await attach(tabId);
  const r = await cdp(tabId, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (r.exceptionDetails) {
    const d = r.exceptionDetails;
    throw new Error((d.exception && d.exception.description) || d.text || "evaluation failed");
  }
  return r.result;
}

async function selectorCenter(tabId, selector) {
  const expr = `(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  })()`;
  const r = await evalInTab(tabId, expr);
  if (!r.value) throw new Error("selector not found: " + selector);
  await sleep(100); // let scrollIntoView settle before we aim a click at it
  return r.value;
}

async function elementCenterByJs(tabId, js) {
  // Find an element via a JS expression and measure it in ONE page round trip,
  // so the click that follows can't aim at a stale rect.
  const expr = `(async () => {
    const el = await (${js});
    if (!el) return { err: "expression returned null/undefined" };
    if (!(el instanceof Element)) return { err: "expression did not return a DOM Element (got " + (typeof el) + ")" };
    el.scrollIntoView({ block: "center", inline: "center" });
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return { err: "element has zero size (hidden?)" };
    return { x: r.x + r.width / 2, y: r.y + r.height / 2,
             tag: el.tagName.toLowerCase(),
             text: ((el.innerText || el.getAttribute("aria-label") || "").trim()).slice(0, 80) };
  })()`;
  const r = await evalInTab(tabId, expr);
  const v = r.value;
  if (!v || v.err) throw new Error("click js: " + ((v && v.err) || "no result"));
  await sleep(100); // let scrollIntoView settle before we aim a click at it
  return v;
}

async function clickAt(tabId, x, y) {
  await attach(tabId);
  const base = { x, y, button: "left", clickCount: 1, pointerType: "mouse" };
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, pointerType: "mouse" });
  await cdp(tabId, "Input.dispatchMouseEvent", { ...base, type: "mousePressed" });
  await cdp(tabId, "Input.dispatchMouseEvent", { ...base, type: "mouseReleased" });
  return { x: Math.round(x), y: Math.round(y) };
}

const KEYS = {
  Enter: { code: "Enter", key: "Enter", vk: 13, text: "\r" },
  Tab: { code: "Tab", key: "Tab", vk: 9 },
  Escape: { code: "Escape", key: "Escape", vk: 27 },
  Backspace: { code: "Backspace", key: "Backspace", vk: 8 },
  Delete: { code: "Delete", key: "Delete", vk: 46 },
  ArrowUp: { code: "ArrowUp", key: "ArrowUp", vk: 38 },
  ArrowDown: { code: "ArrowDown", key: "ArrowDown", vk: 40 },
  ArrowLeft: { code: "ArrowLeft", key: "ArrowLeft", vk: 37 },
  ArrowRight: { code: "ArrowRight", key: "ArrowRight", vk: 39 },
  Home: { code: "Home", key: "Home", vk: 36 },
  End: { code: "End", key: "End", vk: 35 },
  PageUp: { code: "PageUp", key: "PageUp", vk: 33 },
  PageDown: { code: "PageDown", key: "PageDown", vk: 34 },
  Space: { code: "Space", key: " ", vk: 32, text: " " },
};

async function pressKey(tabId, name) {
  const k = KEYS[name];
  if (!k) throw new Error(`unknown key "${name}" (known: ${Object.keys(KEYS).join(", ")})`);
  await attach(tabId);
  const base = { key: k.key, code: k.code, windowsVirtualKeyCode: k.vk, nativeVirtualKeyCode: k.vk };
  await cdp(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyDown", ...(k.text ? { text: k.text } : {}) });
  await cdp(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyUp" });
}

// ---------------------------------------------------------------- command dispatch

async function dispatch(cmd, a) {
  switch (cmd) {
    case "tabs": {
      const tabs = await chrome.tabs.query({});
      return { tabs: tabs.map(tabInfo) };
    }

    case "open": {
      const tab = await chrome.tabs.create({ url: a.url || "about:blank" });
      const loaded = await waitForLoad(tab.id);
      return { ...tabInfo(await chrome.tabs.get(tab.id)), loaded };
    }

    case "navigate": {
      const tabId = await resolveTab(a);
      if (!a.url) throw new Error("missing url");
      await chrome.tabs.update(tabId, { url: a.url });
      const loaded = await waitForLoad(tabId);
      return { ...tabInfo(await chrome.tabs.get(tabId)), loaded };
    }

    case "close_tab": {
      const tabId = await resolveTab(a);
      await chrome.tabs.remove(tabId);
      return { closed: tabId };
    }

    case "screenshot": {
      const tabId = await resolveTab(a);
      await attach(tabId);
      const r = await cdp(tabId, "Page.captureScreenshot", { format: "png" });
      return { data: r.data, mime: "image/png" };
    }

    case "read": {
      const tabId = await resolveTab(a);
      const max = Math.min(a.max_chars || 20000, 200000);
      const expr = `(() => {
        const text = document.body ? document.body.innerText : "";
        return { url: location.href, title: document.title,
                 text: text.slice(0, ${max}), truncated: text.length > ${max} };
      })()`;
      const r = await evalInTab(tabId, expr);
      return r.value;
    }

    case "eval": {
      const tabId = await resolveTab(a);
      if (!a.code) throw new Error("missing code");
      const r = await evalInTab(tabId, a.code);
      return { value: r.value !== undefined ? r.value : (r.description ?? null) };
    }

    case "click": {
      const tabId = await resolveTab(a);
      let { x, y } = a;
      let element;
      if (a.js) {
        const c = await elementCenterByJs(tabId, a.js);
        ({ x, y } = c);
        element = { tag: c.tag, text: c.text };
      } else if (a.selector) {
        ({ x, y } = await selectorCenter(tabId, a.selector));
      }
      if (x == null || y == null) throw new Error("need js, selector, or x/y");
      const clicked = await clickAt(tabId, x, y);
      return element ? { clicked, element } : { clicked };
    }

    case "wait_for": {
      const tabId = await resolveTab(a);
      if (!a.js && !a.selector) throw new Error("need js or selector");
      const expr = a.js
        ? `(async () => { const v = await (${a.js}); return v || null; })()`
        : `document.querySelector(${JSON.stringify(a.selector)}) ? true : null`;
      const timeout = Math.min(a.timeout_ms || 15000, 110000);
      const poll = Math.max(a.poll_ms || 100, 50);
      const start = Date.now();
      for (;;) {
        let v = null;
        try {
          v = (await evalInTab(tabId, expr)).value;
        } catch (e) {
          // Context torn down mid-navigation is fine — keep polling. A tab
          // that no longer exists is not: fail fast, don't burn the timeout.
          const gone = await chrome.tabs.get(tabId).then(() => false, () => true);
          if (gone) throw new Error(`tab ${tabId} no longer exists`);
        }
        if (v != null && v !== false) {
          return { ready: true, value: v, waited_ms: Date.now() - start };
        }
        if (Date.now() - start >= timeout) {
          return { ready: false, waited_ms: Date.now() - start };
        }
        await sleep(poll);
      }
    }

    case "version": {
      return { version: VERSION };
    }

    case "reload_extension": {
      // Dev convenience: picks up edited extension code without a trip to
      // chrome://extensions. Reply first, then reload (reload kills this worker).
      setTimeout(() => chrome.runtime.reload(), 200);
      return { reloading: true, version: VERSION };
    }

    case "type": {
      const tabId = await resolveTab(a);
      if (a.text == null) throw new Error("missing text");
      if (a.selector) {
        const { x, y } = await selectorCenter(tabId, a.selector);
        await clickAt(tabId, x, y); // real click to focus the field
        await sleep(50);
      }
      await attach(tabId);
      await cdp(tabId, "Input.insertText", { text: a.text });
      if (a.submit) await pressKey(tabId, "Enter");
      return { typed: a.text.length, submitted: !!a.submit };
    }

    case "key": {
      const tabId = await resolveTab(a);
      if (!a.key) throw new Error("missing key");
      await pressKey(tabId, a.key);
      return { pressed: a.key };
    }

    case "console": {
      const tabId = await resolveTab(a);
      await attach(tabId); // start capturing if we weren't already
      const buf = consoleBuf.get(tabId) || [];
      const entries = buf.slice(-(a.limit || 100));
      if (a.clear) consoleBuf.set(tabId, []);
      return { entries, note: attached.has(tabId) ? undefined : "capture just started; only future logs will appear" };
    }

    case "detach": {
      const tabId = await resolveTab(a);
      if (attached.has(tabId)) {
        await chrome.debugger.detach({ tabId });
        attached.delete(tabId);
      }
      return { detached: tabId };
    }

    default:
      throw new Error(`unknown command "${cmd}"`);
  }
}
