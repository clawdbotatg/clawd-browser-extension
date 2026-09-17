#!/usr/bin/env python3
"""MCP stdio server for clawd-browser.

Claude Code spawns this per session (registered in .mcp.json). It's a thin
client: each tool call becomes a POST to the local bridge (bridge.py), which
relays it to the Chrome extension over WebSocket. If the bridge isn't running
it gets spawned automatically.

Speaks newline-delimited JSON-RPC 2.0 on stdio. Pure stdlib.

Remote bridge: set CLAWD_BROWSER_URL=http://<host>:8765/k/<token> (the LAN URL
from a pasted tab context) to drive a browser on ANOTHER machine. Then nothing
is auto-started here — the bridge lives with the browser.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    """KEY=VALUE lines from .env next to this file (gitignored); never overrides the real env."""
    try:
        with open(os.path.join(HERE, ".env")) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"'))
    except OSError:
        pass


load_env()
PORT = int(os.environ.get("CLAWD_BROWSER_PORT", "8765"))
BRIDGE_URL = (os.environ.get("CLAWD_BROWSER_URL") or f"http://127.0.0.1:{PORT}").rstrip("/")
REMOTE = not BRIDGE_URL.startswith(("http://127.", "http://localhost"))
VERSION = "0.9.0"


def log(msg):
    print(f"[clawd-browser] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- bridge client

def bridge_post(cmd, args, timeout=60):
    body = json.dumps({"cmd": cmd, "args": args, "timeout": min(timeout, 110)}).encode()
    req = urllib.request.Request(
        f"{BRIDGE_URL}/cmd", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
        return json.loads(resp.read().decode())


def bridge_alive():
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL}/status", timeout=2) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ensure_bridge():
    if bridge_alive() is not None:
        return True
    if REMOTE:
        return False
    log("bridge not running; starting it")
    logfile = open(os.path.join(HERE, "bridge.log"), "ab")
    subprocess.Popen(
        [sys.executable, os.path.join(HERE, "bridge.py")],
        stdout=logfile, stderr=logfile, start_new_session=True,
    )
    for _ in range(20):
        time.sleep(0.15)
        if bridge_alive() is not None:
            return True
    return False


def call_browser(cmd, args, timeout=60):
    if not ensure_bridge():
        if REMOTE:
            return {"ok": False, "error": f"remote bridge {BRIDGE_URL} is not answering — it runs on the browser's machine; nothing to start here"}
        return {"ok": False, "error": "could not start the bridge (bridge.py) — see bridge.log"}
    try:
        return bridge_post(cmd, args, timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"bridge request failed: {e}"}


# ---------------------------------------------------------------- tool definitions

TAB_ID = {"tab_id": {"type": "integer", "description": "Target tab id (from browser_tabs); the bridge routes it to whichever connected browser owns the tab. Omit to use the active tab of the newest browser that has windows."}}

TOOLS = [
    {
        "name": "browser_tabs",
        "description": "List all open browser tabs with their tab_id, url, and title. If several browsers are connected to the bridge, tabs from all of them are listed, each tagged with a \"browser\" connection id.",
        "inputSchema": {"type": "object", "properties": {}},
        "cmd": "tabs",
    },
    {
        "name": "browser_open",
        "description": "Open a new browser tab at the given URL and wait for it to load. Returns the new tab's info including its tab_id.",
        "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        "cmd": "open",
    },
    {
        "name": "browser_navigate",
        "description": "Navigate an existing tab to a URL and wait for it to load.",
        "inputSchema": {"type": "object", "properties": {**TAB_ID, "url": {"type": "string"}}, "required": ["url"]},
        "cmd": "navigate",
    },
    {
        "name": "browser_screenshot",
        "description": "Take a PNG screenshot of a tab's viewport (works even if the tab is not focused).",
        "inputSchema": {"type": "object", "properties": {**TAB_ID}},
        "cmd": "screenshot",
    },
    {
        "name": "browser_read",
        "description": "Read a tab's visible text content (document.body.innerText), plus its url and title.",
        "inputSchema": {
            "type": "object",
            "properties": {**TAB_ID, "max_chars": {"type": "integer", "description": "Truncate text to this many characters (default 20000)."}},
        },
        "cmd": "read",
    },
    {
        "name": "browser_eval",
        "description": "Evaluate JavaScript in a tab and return the result (JSON-serializable values come back by value; promises are awaited).",
        "inputSchema": {"type": "object", "properties": {**TAB_ID, "code": {"type": "string"}}, "required": ["code"]},
        "cmd": "eval",
    },
    {
        "name": "browser_click",
        "description": "Click an element in a tab. Target it one of three ways: js (a JS expression evaluating to a DOM Element — may be async; the element is scrolled into view, measured, and clicked in one atomic operation, immune to stale coordinates; prefer this on dynamic pages), a CSS selector (scrolled into view first), or viewport x/y coordinates. Dispatches trusted mouse events. With js, the reply echoes the clicked element's tag and text so you can verify the target.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **TAB_ID,
                "js": {"type": "string", "description": "JS expression returning the Element to click, e.g. \"[...document.querySelectorAll('button')].find(b => /^Block$/i.test(b.innerText))\"."},
                "selector": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
        },
        "cmd": "click",
    },
    {
        "name": "browser_wait_for",
        "description": "Wait until a condition holds in a tab, instead of guessing with sleeps. Give either a CSS selector (resolves when it matches) or a js expression (re-evaluated every poll_ms until it returns a truthy value — note: falsy-but-valid results like 0 or '' are treated as not-ready, so return an object/array/true). Survives mid-wait navigations. Returns {ready, value, waited_ms}; ready=false means it timed out (not an error).",
        "inputSchema": {
            "type": "object",
            "properties": {
                **TAB_ID,
                "js": {"type": "string", "description": "Expression to poll until truthy; its value is returned."},
                "selector": {"type": "string", "description": "Resolve when document.querySelector matches."},
                "timeout_ms": {"type": "integer", "description": "Give up after this long (default 15000, max 110000)."},
                "poll_ms": {"type": "integer", "description": "Poll interval (default 100, min 50)."},
            },
        },
        "cmd": "wait_for",
    },
    {
        "name": "browser_type",
        "description": "Type text into a tab as trusted keyboard input. If selector is given, that element is clicked first to focus it. Set submit=true to press Enter afterwards.",
        "inputSchema": {
            "type": "object",
            "properties": {
                **TAB_ID,
                "text": {"type": "string"},
                "selector": {"type": "string", "description": "Element to click/focus before typing."},
                "submit": {"type": "boolean", "description": "Press Enter after typing."},
            },
            "required": ["text"],
        },
        "cmd": "type",
    },
    {
        "name": "browser_key",
        "description": "Press a special key in a tab: Enter, Tab, Escape, Backspace, Delete, ArrowUp/Down/Left/Right, Home, End, PageUp, PageDown, Space.",
        "inputSchema": {"type": "object", "properties": {**TAB_ID, "key": {"type": "string"}}, "required": ["key"]},
        "cmd": "key",
    },
    {
        "name": "browser_console",
        "description": "Read console messages captured from a tab (capture starts when the tab is first touched by any browser_* tool). Set clear=true to flush the buffer after reading.",
        "inputSchema": {
            "type": "object",
            "properties": {**TAB_ID, "limit": {"type": "integer"}, "clear": {"type": "boolean"}},
        },
        "cmd": "console",
    },
    {
        "name": "browser_close_tab",
        "description": "Close a browser tab.",
        "inputSchema": {"type": "object", "properties": {**TAB_ID}},
        "cmd": "close_tab",
    },
]

TOOL_CMDS = {t["name"]: t["cmd"] for t in TOOLS}


# ---------------------------------------------------------------- browser_run: the Jev loop

def run_jev(tab_id, goal, max_steps=30, allow_irreversible=False):
    """Run the Jev decision loop on one real tab. Returns the report dict (never raises)."""
    from jev import Agent, Browser, CAVEATS

    def post(cmd, args):
        r = call_browser(cmd, args, timeout=30)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "bridge error"))
        return r.get("result")

    try:
        browser = Browser(tab_id, post)
    except RuntimeError as e:
        return {"status": "error", "reason": f"could not attach to tab {tab_id}: {e}"}
    try:
        report = Agent(browser, goal, max_steps=max_steps, allow_irreversible=allow_irreversible).run()
    except (RuntimeError, ValueError) as e:
        report = {"status": "error", "reason": str(e)}
    finally:
        browser.close()
    report["caveats"] = CAVEATS
    return report


RUN_TOOL = {
    "name": "browser_run",
    "description": (
        "Hand a small, well-scoped browser task to a fast cheap decision model (TypeSafe Jev, ported from "
        "browser-use/jev-ultrafast) that runs INSIDE the given real tab: it snapshots the visible controls, picks one "
        "click/type/select per step at ~150 ms and ~$0.0003, and stops on DONE, BLOCKED, or the step budget. You "
        "stay the planner and the verifier. Good for: fill this form, get to the results page, open the article "
        "about X, dismiss this dialog. Returns the action trail plus the final page text.\n"
        "CAVEATS, each one bit us on 2026-09-17:\n"
        "- It only sees what is on screen. Targets below the fold or inside an inner scroll list do not exist to "
        "it and it will not scroll to find them. Scroll/open things for it first, or split the task.\n"
        "- DONE is a guess, not proof; it declared DONE before results rendered. ALWAYS verify with browser_read "
        "or browser_screenshot before telling the user it worked.\n"
        "- On an impossible target it loops until the budget. 'blocked'/'budget' means change the plan; never "
        "rerun the same goal.\n"
        "- This is the user's real, logged-in browser. Never give it goals that buy, pay, send, post, publish, sign, "
        "approve, transfer or delete. Code refuses to click labels that look like that (word-match heuristic, not "
        "a guarantee; allow_irreversible=true overrides it and needs the user's explicit OK for that action).\n"
        "- Text fields are filled by a small LLM from the goal; check typed values in the trail.\n"
        "- No shadow DOM, iframes, canvas, uploads, pop-ups."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "tab_id": {"type": "integer", "description": "The tab to work in (from browser_tabs). Required: it must never guess a tab."},
            "goal": {"type": "string", "description": "One concrete, visible-on-this-page goal, with a stop condition. E.g. 'Search for one-way flights Denver to Mumbai on Nov 1, 2026. Stop when flight options are visible.'"},
            "max_steps": {"type": "integer", "description": "Action cap (default 30; model-call cap is double). Wall clock cap is 90 s."},
            "allow_irreversible": {"type": "boolean", "description": "Let it click buy/send/post/sign/delete-looking controls. Only with the user's explicit OK for that specific action."},
        },
        "required": ["tab_id", "goal"],
    },
}
TOOLS.append(RUN_TOOL)


# ---------------------------------------------------------------- MCP plumbing

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def reply(mid, result):
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def reply_error(mid, code, message):
    send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def tool_result(reply_obj):
    """Convert a bridge reply into MCP tool-call content."""
    if not reply_obj.get("ok"):
        return {
            "content": [{"type": "text", "text": f"Error: {reply_obj.get('error', 'unknown error')}"}],
            "isError": True,
        }
    result = reply_obj.get("result") or {}
    if "data" in result and result.get("mime", "").startswith("image/"):
        return {
            "content": [{"type": "image", "data": result["data"], "mimeType": result["mime"]}]
        }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        reply(mid, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "clawd-browser", "version": VERSION},
        })
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        reply(mid, {})
    elif method == "tools/list":
        reply(mid, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "browser_run":
            report = run_jev(int(args["tab_id"]), args.get("goal", ""), int(args.get("max_steps") or 30),
                             bool(args.get("allow_irreversible")))
            reply(mid, {"content": [{"type": "text", "text": json.dumps(report, indent=2, ensure_ascii=False)}],
                        "isError": report.get("status") == "error"})
            return
        cmd = TOOL_CMDS.get(name)
        if not cmd:
            reply_error(mid, -32602, f"unknown tool: {name}")
            return
        reply(mid, tool_result(call_browser(cmd, args)))
    elif mid is not None:
        reply_error(mid, -32601, f"method not found: {method}")


def main():
    log(f"clawd-browser MCP server v{VERSION}, bridge at {BRIDGE_URL}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as e:  # never die mid-session; report per-request
            log(f"error handling {msg.get('method')}: {e}")
            if msg.get("id") is not None:
                reply_error(msg["id"], -32603, str(e))


if __name__ == "__main__":
    main()
