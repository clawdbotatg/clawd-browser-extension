"""rip: run a batch of browser goals with Jev, in parallel across tabs, verify each one.

    rip(post, [
        {"url": "https://en.wikipedia.org", "goal": "Open the article about Gödel", "verify": "location.pathname.includes('del')"},
        {"tab_id": 123, "goal": "Dismiss the cookie banner"},
        {"tab_id": 123, "goal": "Search for 'zk rollups'; stop when results are listed"},
    ], parallel=3)

Rules: goals on the SAME tab run in order; different tabs run at the same time (the bridge
serves tabs independently; two 1.5 s evals on two tabs took 2.1 s wall on 2026-09-23).
A plan with `url` opens its own tab and closes it afterwards unless `keep: true`.
`verify` is a JS expression evaluated on the tab after the run; its value goes in the
report as `verified`, so Claude does not have to trust DONE. Nothing here overrides the
loop's refusal to click buy/pay/send/post/sign/delete labels (`allow_irreversible` per plan).

Reports are compact: status, reason, steps, elapsed, cost, verified, final url/title, trail."""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .agent import Agent
from .browser import Browser

MAX_PARALLEL = 4


def _run_one(post, plan):
    goal = (plan.get("goal") or "").strip()
    out = {"goal": goal, "tab_id": plan.get("tab_id"), "url": plan.get("url"), "status": "error"}
    if not goal:
        out["reason"] = "empty goal"
        return out
    opened = None
    started = time.perf_counter()
    try:
        tab_id = plan.get("tab_id")
        if tab_id is None:
            if not plan.get("url"):
                out["reason"] = "plan needs tab_id or url"
                return out
            opened = (post("open", {"url": plan["url"]}) or {}).get("tab_id")
            if opened is None:
                out["reason"] = "could not open a tab"
                return out
            tab_id = opened
            out["tab_id"] = tab_id
        browser = Browser(tab_id, post)
        try:
            report = Agent(
                browser, goal,
                max_steps=int(plan.get("max_steps") or 30),
                time_budget_s=int(plan.get("time_budget_s") or 90),
                allow_irreversible=bool(plan.get("allow_irreversible")),
            ).run()
            if plan.get("verify"):
                try:
                    out["verified"] = browser.evaluate(f"(() => {{ try {{ return ({plan['verify']}); }} catch (e) {{ return 'verify error: ' + e; }} }})()")
                except Exception as e:  # noqa: BLE001 - a verify failure is data, not a crash
                    out["verified"] = f"verify error: {e}"
        finally:
            browser.close()
        out.update({k: report.get(k) for k in ("status", "reason", "steps", "model_calls", "approx_cost_usd", "trail")})
        out["page"] = {k: report.get("page", {}).get(k) for k in ("url", "title")}
        out["final_text"] = (report.get("page", {}).get("text") or "")[:1500]
    except Exception as e:  # noqa: BLE001 - one bad plan must not sink the batch
        out["status"], out["reason"] = "error", str(e)
    finally:
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        if opened is not None and not plan.get("keep"):
            try:
                post("cdp", {"tab_id": opened, "method": "Page.close", "params": {}})
                out["closed_tab"] = True
            except Exception:  # noqa: BLE001
                out["closed_tab"] = False
    return out


def _lanes(plans):
    """Group plans into lanes that must run sequentially: same tab_id -> one lane; each url -> its own lane."""
    lanes, by_tab = [], {}
    for p in plans:
        t = p.get("tab_id")
        if t is None:
            lanes.append([p])
        elif t in by_tab:
            by_tab[t].append(p)
        else:
            by_tab[t] = [p]
            lanes.append(by_tab[t])
    return lanes


def rip(post, plans, parallel=3):
    """Run every plan; return {"results": [...in input order...], "totals": {...}}."""
    if not isinstance(plans, list) or not plans:
        raise ValueError("plans must be a non-empty list")
    parallel = max(1, min(int(parallel or 1), MAX_PARALLEL))
    order = {id(p): i for i, p in enumerate(plans)}
    results = [None] * len(plans)
    started = time.perf_counter()

    def lane(items):
        for p in items:
            results[order[id(p)]] = _run_one(post, p)

    with ThreadPoolExecutor(max_workers=parallel) as ex:
        list(ex.map(lane, _lanes(plans)))
    done = sum(1 for r in results if r and r.get("status") == "done")
    return {
        "results": results,
        "totals": {
            "plans": len(plans),
            "done": done,
            "not_done": len(plans) - done,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "approx_cost_usd": round(sum((r or {}).get("approx_cost_usd") or 0 for r in results), 5),
            "verify": "DONE is the model's guess. Trust `verified` (your JS) or read the page; never the status alone.",
        },
    }


def _main():
    import argparse
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import mcp_server  # noqa: E402  (loads .env, gives us the bridge client)

    ap = argparse.ArgumentParser(description="python3 -m jev.rip plan.json [--parallel 3]   (plan: JSON list of {tab_id|url, goal, verify?, keep?, max_steps?})")
    ap.add_argument("plan", help="JSON file or - for stdin")
    ap.add_argument("--parallel", type=int, default=3)
    a = ap.parse_args()
    plans = json.load(sys.stdin if a.plan == "-" else open(a.plan))
    print(json.dumps(rip(mcp_server.bridge_call_for_jev, plans, a.parallel), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    _main()
