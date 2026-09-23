"""Bulk decisions: one Jev call classifies up to 50 items at once (~1.5 s, ~$0.0004).

This is what makes triage fast. The old way was one frontier-model turn per inbox row.
The new way: extract the rows (JS or an API), ask Jev one question about every row in a
single request, act on the answers in bulk. Jev never generates text; each answer is a
probability over the options YOU offered, so there is nothing to parse or hallucinate.

    decide(items, question, criteria)            -> choice per item
    decide(items, question, kind="noul")         -> P(true) per item

Items are dicts (or strings). `context` is free text about who is asking / the policy.
Never sends anything but what you pass in. Key: TYPESAFE_API_KEY (from .env)."""

import json
import math
import os
import sys
import time

from .model import TYPESAFE_URL, post_json

CHUNK = 50  # questions per request; 50 rows ran in 1.46 s on 2026-09-23
PRICE_PER_MTOK = 0.042


def _question(question, kind, criteria, ref):
    if kind == "noul":
        crit = criteria or {"true": "yes", "false": "no"}
        return {"type": "noul", "instructions": f"{ref}: {question}", "criteria": crit}
    if not criteria or len(criteria) < 2:
        raise ValueError("choice needs criteria: {option: description, ...} with at least two options")
    return {"type": "choice", "instructions": f"{ref}: {question}", "criteria": dict(criteria)}


def _validate(answer, kind, options):
    try:
        if kind == "noul":
            p = answer["noul"]
            if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
                raise ValueError
            return {"p": round(float(p), 3), "confidence": answer.get("confidence")}
        probs = answer["probabilities"]
        if answer["choice"] not in options or set(probs) != set(options):
            raise ValueError
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in probs.values()):
            raise ValueError
        if abs(sum(probs.values()) - 1) > 0.02:
            raise ValueError
        return {"choice": answer["choice"], "p": round(float(probs[answer["choice"]]), 3),
                "probabilities": {k: round(float(v), 3) for k, v in probs.items()},
                "confidence": answer.get("confidence")}
    except (KeyError, TypeError, ValueError):
        return {"error": "invalid answer from the model; treat as undecided"}


def decide(items, question, criteria=None, kind="choice", context=None, chunk=CHUNK, model=None):
    """Return {"results": [...one per item, same order...], "calls", "latency_ms", "input_tokens", "approx_cost_usd"}."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise ValueError("TYPESAFE_API_KEY is not set; nothing decided.")
    if kind not in ("choice", "noul"):
        raise ValueError("kind must be choice or noul")
    if not items:
        return {"results": [], "calls": 0, "latency_ms": 0, "input_tokens": 0, "approx_cost_usd": 0.0}
    options = set(criteria or {}) if kind == "choice" else None
    model = model or os.environ.get("TYPESAFE_MODEL", "jev-latest")
    results, calls, tokens, started = [], 0, 0, time.perf_counter()
    for start in range(0, len(items), max(1, chunk)):
        batch = items[start:start + chunk]
        rows = [{"i": start + j, **(it if isinstance(it, dict) else {"text": it})} for j, it in enumerate(batch)]
        questions = {f"r{r['i']}": _question(question, kind, criteria, f"Item i={r['i']}") for r in rows}
        state = {"items": rows}
        if context:
            state["context"] = context
        reply = post_json(TYPESAFE_URL, key, {"model": model, "state": state, "questions": questions})
        calls += 1
        tokens += (reply.get("usage") or {}).get("input_tokens", 0) or 0
        answers = reply.get("answers") or {}
        for r in rows:
            out = _validate(answers.get(f"r{r['i']}") or {}, kind, options)
            out["i"] = r["i"]
            results.append(out)
    return {
        "results": results,
        "calls": calls,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "input_tokens": tokens,
        "approx_cost_usd": round(tokens * PRICE_PER_MTOK / 1e6, 5),
        "model": model,
    }


def buckets(result, items=None):
    """Group decide() results by choice (or true/false for noul) -> {label: [i, ...]}."""
    out = {}
    for r in result["results"]:
        label = r.get("choice") if "choice" in r else ("true" if r.get("p", 0) >= 0.5 else "false") if "p" in r else "error"
        out.setdefault(label, []).append(r["i"])
    return out


def _main():
    import argparse

    p = argparse.ArgumentParser(description="python3 -m jev.decide items.json --question '...' --criteria keep='...' seen='...'")
    p.add_argument("items", help="JSON file: a list of objects or strings (- for stdin)")
    p.add_argument("--question", required=True)
    p.add_argument("--criteria", nargs="*", default=[], help="option=description pairs (choice) or omit for --noul")
    p.add_argument("--noul", action="store_true", help="yes/no: P(true) per item")
    p.add_argument("--context", default=None, help="who is asking / the policy, one paragraph")
    a = p.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import mcp_server  # noqa: F401  (loads .env next to it)

    items = json.load(sys.stdin if a.items == "-" else open(a.items))
    criteria = dict(kv.split("=", 1) for kv in a.criteria) if a.criteria else None
    out = decide(items, a.question, criteria, kind="noul" if a.noul else "choice", context=a.context)
    out["buckets"] = buckets(out)
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    _main()
