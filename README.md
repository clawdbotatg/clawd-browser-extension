# clawd-browser-extension

Drive your own Chrome from a Claude Code session — a homegrown replacement for
the claude-in-chrome extension.

## How it works

Chrome extensions can't accept inbound connections, so the extension dials out:

```
Claude Code ──stdio/MCP──> mcp_server.py ──HTTP──> bridge.py <──WebSocket── extension (Chrome)
                                                (127.0.0.1:8765)
```

- **`extension/`** — Manifest V3 extension. The service worker keeps a WebSocket
  open to the bridge and executes commands via `chrome.debugger` (CDP): trusted
  mouse/keyboard events, screenshots without focusing the tab, JS eval, and
  console capture. Chrome shows the "is debugging this browser" bar while a tab
  is attached — that's the price of trusted input.
- **`bridge.py`** — localhost daemon, pure Python stdlib. One port, two faces:
  WebSocket endpoint `/ext` for the extension, HTTP `POST /cmd` + `GET /status`
  for clients.
- **`mcp_server.py`** — stdio MCP server Claude Code spawns per session (see
  `.mcp.json`). Thin client over the bridge's HTTP API; auto-starts the bridge
  if it isn't running.

## Setup

1. Load the extension: `chrome://extensions` → enable **Developer mode** →
   **Load unpacked** → pick the `extension/` folder.
2. Register the MCP server. Inside this repo it's automatic (`.mcp.json`).
   From anywhere else:
   ```sh
   claude mcp add clawd-browser -- python3 /path/to/clawd-browser-extension/mcp_server.py
   ```
3. That's it. The bridge starts on demand; the extension reconnects every few
   seconds until it finds it.

Port defaults to `8765`; override with `CLAWD_BROWSER_PORT` (the extension side
reads `port` from `chrome.storage.local`).

## Tools

| tool | what it does |
|---|---|
| `browser_tabs` | list open tabs |
| `browser_open` | open a URL in a new tab, wait for load |
| `browser_navigate` | point an existing tab at a URL |
| `browser_screenshot` | PNG of the viewport (tab needn't be focused) |
| `browser_read` | page text (`innerText`) + url + title |
| `browser_eval` | run JS in the page, promises awaited |
| `browser_click` | trusted click by CSS selector or x/y |
| `browser_type` | trusted keystrokes; optional focus selector + Enter |
| `browser_key` | press Enter/Tab/Escape/arrows/etc. |
| `browser_console` | read captured console messages |
| `browser_close_tab` | close a tab |

All tab-targeting tools take an optional `tab_id` (from `browser_tabs`) and
default to the active tab.

## Testing

- `python3 -m py_compile bridge.py mcp_server.py` and `node --check extension/background.js`
- End-to-end (launches a real Chromium with the extension loaded, drives it
  through the full MCP → bridge → extension path): `node test/e2e.mjs`

## Security notes

The bridge binds `127.0.0.1` and has **no auth**: any process on this machine
can drive the browser through it. Same trust model as other local automation
bridges, but keep it in mind. Don't run it on a shared box.
