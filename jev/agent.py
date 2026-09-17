"""The loop: observe -> Jev picks an operation + element -> execute -> repeat. Bounded.

Ported from browser-use/jev-ultrafast (MIT), minus the demo UI, plus: a wall-clock budget,
a code-side refusal for irreversible-looking clicks, and a compact report for Claude."""

import re
import time

from .browser import StalePage
from .model import choose, field_context, field_text
from .questions import MAX_STEPS

# Word-match heuristic, not a guarantee. "Sign in"/"Sign up" are allowed; "Sign" alone is not.
IRREVERSIBLE = re.compile(
    r"\b(buy|purchase|pay|checkout|place order|order now|send|confirm|delete|remove|post|publish|tweet|reply"
    r"|transfer|approve|swap|withdraw|deposit|mint|unsubscribe|cancel subscription|sign(?! in| up)|submit payment)\b",
    re.I,
)


class Agent:
    def __init__(self, browser, goal, *, max_steps=MAX_STEPS, time_budget_s=90, allow_irreversible=False):
        goal = goal.strip()
        if not goal:
            raise ValueError("Supply a goal")
        self.browser = browser
        self.goal = goal
        self.max_steps = max_steps
        self.time_budget_s = time_budget_s
        self.allow_irreversible = allow_irreversible
        self.pending_text = None
        self.history = []
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.status = "ready"
        self.reason = None
        self.page = browser.observe()
        self.started = time.perf_counter()

    def elapsed_ms(self):
        return round((time.perf_counter() - self.started) * 1000)

    def tick(self):
        """One decision + execution. Sets self.status to done/blocked/budget when the run ends."""
        if not self.browser.fresh(self.page):
            self.page = self.browser.observe()
        if self.calls >= self.max_steps * 2:
            self.status, self.reason = "budget", f"{self.calls} model calls (cap {self.max_steps * 2})"
            return
        if self.elapsed_ms() > self.time_budget_s * 1000:
            self.status, self.reason = "budget", f"{self.time_budget_s}s wall clock"
            return
        decision = choose(self.page, self.goal, self.history)
        self.calls += 1
        for k in self.usage:
            self.usage[k] += decision.get("usage", {}).get(k, 0) or 0
        selected = decision["choice"]
        page = self.page
        try:
            if selected in {"DONE", "BLOCKED"}:
                if not self.browser.fresh(page):
                    raise StalePage("Page changed since the decision.")
                self.status = "done" if selected == "DONE" else "blocked"
                self.reason = f"model chose {selected} at {decision['confidence']:.0%} confidence"
                return
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(self.history) >= self.max_steps:
                self.status, self.reason = "budget", f"{self.max_steps} actions"
                return
            if action["kind"] in {"click", "select"} and not self.allow_irreversible and IRREVERSIBLE.search(action["label"]):
                self.status = "blocked"
                self.reason = f"refused to click {action['label']!r}: looks irreversible (buy/send/post/sign/delete...). A human does that."
                return
            text, helper = None, None
            if action["kind"] == "fill":
                if not self.browser.fresh(page):
                    raise StalePage("Page changed before text generation.")
                context = field_context(self.goal, action, page, self.history)
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context)
                    self.pending_text = (context, text, helper)
            self.browser.act(action, page, text=text)
            self.pending_text = None
            self.history.append(
                {
                    "step": len(self.history) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "operation": decision["operation"],
                    "probability": round(decision["probabilities"].get(selected, 0), 2),
                    "confidence": round(decision["confidence"], 2),
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper_ms": helper["latency_ms"] if helper else None,
                    "page_changed": None,
                    "elapsed_ms": self.elapsed_ms(),
                }
            )
            self.page = self.browser.observe()
            self.history[-1]["page_changed"] = self.page["fingerprint"] != page["fingerprint"]
            repeated = self.history[-3:]
            if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated):
                self.status, self.reason = "blocked", "three actions in a row changed nothing"
        except StalePage:
            self.page = self.browser.observe()

    def run(self):
        while self.status == "ready":
            self.tick()
        return self.report()

    def report(self):
        page = self.page
        return {
            "status": self.status,
            "reason": self.reason,
            "goal": self.goal,
            "steps": len(self.history),
            "model_calls": self.calls,
            "elapsed_ms": self.elapsed_ms(),
            "approx_cost_usd": round(self.usage["input_tokens"] * 0.042 / 1e6, 5),
            "trail": [
                f"{h['step']}. {h['operation']} {h['action']!s:.60}"
                + (f' typed "{h["text"]}"' if h["text"] else "")
                + (f" ({h['latency_ms']} ms, {h['probability']:.0%})")
                + ("" if h["page_changed"] else " [no change]")
                for h in self.history
            ],
            "page": {"url": page["url"], "title": page["title"], "text": page["text"][:3000]},
            "verify": "DONE is the model's guess. Check page.text/screenshot against the goal before reporting success.",
        }
