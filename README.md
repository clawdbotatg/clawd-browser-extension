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
- **`bridge.py`** — daemon, pure Python stdlib. One port, two faces:
  WebSocket endpoint `/ext` for the extension, HTTP `POST /cmd` + `GET /status`
  for clients. Listens on the LAN as well as loopback (see *Cross-machine* below).
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

**Sharing the browser with any Claude session — one copy, one paste:** click
the extension's toolbar icon and the popup copies a single paste-able blob (and
shows bridge health). It carries the current tab's context (tab_id, title, url)
*plus* a link to the full instructions — the bridge serves `extension/skill.txt`
at `GET /skill`. Paste it into any Claude session and it knows (1) how to drive
this browser: the MCP tools if it has them, otherwise it fetches
`curl -s http://127.0.0.1:8765/skill` and learns the plain-HTTP API — and
(2) exactly which tab you're talking about, no "which of your 40 tabs?" round
trip.

**Cross-machine — the paste works from any computer on your LAN:** the bridge
binds every interface (`CLAWD_BROWSER_BIND`, default `0.0.0.0`) and trusts by
origin. Loopback callers (this machine) need nothing, as before. Anyone else
must prefix every path with `/k/<token>/`; the token is generated once into
`.clawd-browser.token` (gitignored, mode 0600) next to `bridge.py`, and the
bridge hands it to the popup via `GET /status` (`lan: {hosts, urls, token}`).
So the copied blob carries `bridge: http://192.168.x.y:8765/k/<token>` (plus
the box's `.local` name as a fallback), and a Claude session on a different
machine follows it verbatim: `curl -s <that url>/skill` returns the skill with
every example URL rewritten to the token URL as the caller reached it. The
paste is the credential — whoever holds it can drive that browser. Without a
token from off-box the bridge answers 403. To get native MCP tools on the
other machine, point this repo's server at the LAN URL:
```sh
claude mcp add clawd-browser --scope user -e CLAWD_BROWSER_URL=http://<host>:8765/k/<token> -- python3 /path/to/mcp_server.py
```
(with a remote URL it never tries to start a bridge locally — the bridge lives
with the browser). Set `CLAWD_BROWSER_BIND=127.0.0.1` to go back to
loopback-only.

**Open a session about this tab — one click:** under the copy button a small
`open a session ↗` link opens your clawd-harness on a new session in a scratch
project ([clawd-web](https://github.com/clawdbotatg/clawd-web)) with the tab
context already sent, plus an opener ("give me a TLDR, then wait"). It uses the
harness's compose deep link (`#/p/<project>/new?q=…&send=1`, see the harness's
`docs/DEEPLINKS.md`) and reuses an open harness tab when there is one. Harness
URL, project, machine and opener live in the extension's settings page.

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
| `browser_click` | trusted click by `js` expression → Element (atomic find+measure+click, no stale rects), CSS selector, or x/y |
| `browser_wait_for` | poll until a selector matches / a `js` expression is truthy — replaces guessy sleeps |
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

On loopback the bridge has **no auth**: any process on this machine can drive
the browser through it. Same trust model as other local automation bridges,
but keep it in mind. Don't run it on a shared box. From the LAN every request
needs the per-install token in its path (`/k/<token>/…`, 403 otherwise); the
token travels inside the copied blob, so treat a paste like a key — anyone
with it can drive your logged-in browser until you delete
`.clawd-browser.token` and restart the bridge (a new one is generated). It is
plain HTTP on your LAN; don't use it on networks you don't trust.
