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
parser.add_argument("--tab", type=int, required=True)
parser.add_argument("--goal", required=True)
parser.add_argument("--max-steps", type=int, default=30)
parser.add_argument("--allow-irreversible", action="store_true")
args = parser.parse_args()
print(json.dumps(mcp_server.run_jev(args.tab, args.goal, args.max_steps, args.allow_irreversible), indent=1))
