"""CLI: python3 -m jev --tab <tab_id> --goal "..."  (talks to the bridge like mcp_server.py).

For sessions that only have the bridge's HTTP API (another machine, no MCP tools):
set CLAWD_BROWSER_URL=http://<host>:8765/k/<token>. Reads .env next to mcp_server.py."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mcp_server  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--tab", type=int, help="tab id (from the extension popup or the tabs command)")
parser.add_argument("--url", help="instead of --tab: first open tab whose url contains this text")
parser.add_argument("--goal", required=True)
parser.add_argument("--max-steps", type=int, default=30)
parser.add_argument("--allow-irreversible", action="store_true")
args = parser.parse_args()
if args.tab is None:
    if not args.url:
        parser.error("give --tab <id> or --url <substring>")
    tabs = (mcp_server.call_browser("tabs", {}).get("result") or {}).get("tabs") or []
    hits = [t for t in tabs if args.url.lower() in (t.get("url") or "").lower()]
    if not hits:
        sys.exit(f"no open tab with {args.url!r} in its url; open one first")
    args.tab = hits[0]["tab_id"]
    print(f"using tab {args.tab}: {hits[0].get('title')}", file=sys.stderr)
print(json.dumps(mcp_server.run_jev(args.tab, args.goal, args.max_steps, args.allow_irreversible), indent=1))
