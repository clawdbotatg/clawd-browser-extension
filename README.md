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
3. Keep the bridge alive across reboots (macOS): `./install-launchd.sh`
   installs it as a launchd user agent (`com.clawd.browser-bridge`,
   RunAtLoad + KeepAlive, logs to `bridge.log`). Without this the bridge only
   starts on demand from the MCP server, so after a reboot the extension
   spams `ERR_CONNECTION_REFUSED` in its console until some session calls a
   browser tool. The extension reconnects every few seconds either way.

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

## `browser_run` — fast hands, slow brain (v0.9.0)

A ported copy of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast)
(MIT) lives in `jev/` and runs INSIDE one of your real tabs through the extension's
new raw `cdp` command. Each step it snapshots the visible controls into a numbered
list, asks TypeSafe's Jev model to pick one operation + one element (~150 ms,
~$0.0003), executes it with trusted input, and repeats until DONE, BLOCKED or the
budget. A small LLM (Mercury via OpenRouter) writes text only for TYPE_TEXT steps.
Model output never becomes a selector, coordinate or code: every action is an index
into elements we observed, re-validated right before input.

Why: Claude stays the planner and verifier; Jev is the hands. "Fill this form" is one
tool call and ten seconds instead of ten frontier-model turns.

```
browser_run(tab_id, goal, max_steps=30, allow_irreversible=false)   # MCP tool
python3 -m jev --url wikipedia --goal "..."   # or --tab <id>; same loop from a shell
```

Keys go in `.env` next to `mcp_server.py` (see `.env.example`, gitignored):
`TYPESAFE_API_KEY` from https://console.typesafe.ai/keys and an OpenRouter key.

### Caveats — read these, every one bit us on 2026-09-17

- **It only sees what is on screen.** Anything below the fold or inside an inner
  scroll list (a filter popup, a long dropdown) does not exist to it, and it will
  not scroll to look. On Google Flights it clicked "Select all airlines" 48 times
  because "United" was 1,000 px down the list. Scroll or open things for it first,
  or split the task.
- **DONE is a guess, not proof.** It declared DONE before the results had rendered.
  Verify with `browser_read` / `browser_screenshot` before telling anyone it worked.
- **On an impossible target it loops** until the budget (30 actions, 60 model
  calls, 90 s). `blocked` or `budget` means change the plan, never rerun the same
  goal.
- **It runs on your real, logged-in accounts.** Never give it goals that buy, pay,
  send, post, publish, sign, approve, transfer or delete. `jev/agent.py` refuses to
  click labels that look like that (`IRREVERSIBLE`, a word-match heuristic, not a
  guarantee); `allow_irreversible=true` overrides it and needs your explicit OK for
  that specific action.
- **Typed text comes from a small LLM** reading the goal. Check the values in the
  returned trail; it can invent.
- **Unsupported:** shadow DOM, iframes, canvas, file uploads, pop-up windows.
  Material-style hidden checkboxes/radios are clicked through their `<label>`
  (our patch; upstream can't see them at all).

Tests: `python3 test/test_jev.py` (offline, no paid calls; covers run, rip and decide).

## `browser_rip` + `browser_decide` — let Jev rip (v0.10.0)

Two more tools in `jev/`, same key, same caveats. They exist because "triage my
inbox" and "do this on these eight pages" were taking one frontier-model turn per
row. Now they take one cheap call per fifty rows.

**`browser_decide(items, question, criteria, kind, context)`** — bulk classify.
Hand it a list (inbox rows, search results, links, messages; from `browser_eval`,
an API, or a file) and ONE question with named options. One request per 50 items,
~1.5 s and ~$0.0004 each; every item comes back with its option and probability.
No browser involved. `kind: noul` gives P(true) instead. Treat p below ~0.7 as
"ask", not "act".

**`browser_rip(plans, parallel)`** — a batch of `browser_run` goals. Plans on
different tabs run at the same time (cap 4); plans on the same tab run in order.
`{url, goal}` opens its own tab and closes it after (`keep: true` to leave it).
`verify` is a JS expression evaluated after the run; its value comes back as
`verified` so you check the outcome with code, not with the model's DONE.

```
python3 -m jev.decide rows.json --question "How should this row be triaged?" \
    --criteria keep="real mail" seen="noise, mark read" spam="unsolicited" --context "Austin is..."
python3 -m jev.rip plan.json --parallel 3      # plan: [{tab_id|url, goal, verify?, keep?}, ...]
```

The pattern that makes a chore fast: **extract → decide → act in bulk.** Pull the
rows with one `browser_eval` (or the API), one `browser_decide`, then one
`browser_eval` / API call that acts on all the indices in a bucket. Three calls
for fifty rows. `browser_rip` is for the chores that need real clicking.

Live 2026-09-23: 50 inbox rows classified in 1.46 s for $0.0004 (10,116 tokens);
two tabs worked in parallel at 2.1 s wall for two 1.5 s steps.

## Changed bridge.py? Restart it

Nothing watches the file, nothing restarts it on a commit. Until you kill it,
the OLD code keeps running and every "it doesn't work" looks like a bug in the
new code. (v0.8.0 LAN shipped 2026-09-11; the bridge kept running August code
until 09-15.)

```sh
launchctl kickstart -k gui/$(id -u)/com.clawd.browser-bridge   # launchd: kill + restart with the new code
curl -s http://127.0.0.1:8765/status            # verify on the running process
```

Not installed as a launchd agent (`./install-launchd.sh`)? Then it's a plain
background process: `pkill -f clawd-browser-extension/bridge.py` and
`nohup python3 bridge.py >> bridge.log 2>&1 &`. Don't do that when launchd owns
it — KeepAlive restarts its own copy and the nohup one dies on `EADDRINUSE`.

Extensions reconnect on their own within seconds. An extension change needs a
reload in `chrome://extensions` in EVERY Chrome profile (bridge.log prints each
one's version: `extension hello: x.y.z`).

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
