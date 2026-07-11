#!/usr/bin/env python3
"""MCP stdio server for clawd-browser.

Claude Code spawns this per session (registered in .mcp.json). It's a thin
client: each tool call becomes a POST to the local bridge (bridge.py), which
relays it to the Chrome extension over WebSocket. If the bridge isn't running
it gets spawned automatically.

Speaks newline-delimited JSON-RPC 2.0 on stdio. Pure stdlib.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CLAWD_BROWSER_PORT", "8765"))
BRIDGE_URL = f"http://127.0.0.1:{PORT}"
VERSION = "0.3.1"


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
        return {"ok": False, "error": "could not start the bridge (bridge.py) — see bridge.log"}
    try:
        return bridge_post(cmd, args, timeout)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"bridge request failed: {e}"}


# ---------------------------------------------------------------- tool definitions

TAB_ID = {"tab_id": {"type": "integer", "description": "Target tab id (from browser_tabs). Omit to use the active tab."}}

TOOLS = [
    {
        "name": "browser_tabs",
        "description": "List all open browser tabs with their tab_id, url, and title.",
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
        cmd = TOOL_CMDS.get(name)
        if not cmd:
            reply_error(mid, -32602, f"unknown tool: {name}")
            return
        args = params.get("arguments") or {}
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
