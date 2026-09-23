"""Offline tests for the Jev loop. No paid APIs, no browser: python3 test/test_jev.py"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jev import agent as loop  # noqa: E402
from jev import model  # noqa: E402
from jev.browser import StalePage, fingerprint  # noqa: E402


def page(actions=None):
    state = {
        "url": "https://example.test/", "title": "Search", "text": "Search", "scroll": {"y": 0}, "w": 1000, "h": 700,
        "marker": "m1", "page_key": ["k"], "guards": {"10": ["g"], "20": ["g"]},
        "actions": actions or [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "e4", "kind": "click", "label": "Buy now", "role": "button", "value": "", "node": 30},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


class FakeBrowser:
    def __init__(self):
        self.acted = []
        self.page = page()

    def observe(self):
        return self.page

    def fresh(self, page, action=None):
        return True

    def act(self, action, page, text=None):
        self.acted.append((action["id"], text))
        self.page = dict(page, text=page["text"] + "!", fingerprint=f"f{len(self.acted)}")


def decision(choice, operation="CLICK"):
    return {"choice": choice, "operation": operation, "target": "1", "confidence": 0.9,
            "probabilities": {choice: 0.9}, "latency_ms": 10, "usage": {"input_tokens": 100}}


class ModelTests(unittest.TestCase):
    def test_invalid_choice_is_rejected(self):
        good = {"choice": "a", "confidence": 1.0, "probabilities": {"a": 1.0, "b": 0.0}}
        model.validate_choice(good, {"a", "b"})
        for bad in (
            dict(good, choice="invented"),
            dict(good, probabilities={"a": float("nan"), "b": 0}),
            dict(good, probabilities={"a": 1.0}),
            dict(good, probabilities={"a": 0.4, "b": 0.6}),
            dict(good, confidence=5),
        ):
            with self.assertRaises(ValueError):
                model.validate_choice(bad, {"a", "b"})

    def test_one_index_per_node_with_operation_specific_targets(self):
        elements, targets, controls = model.action_space(page()["actions"])
        self.assertEqual(len(elements), 3)
        self.assertEqual(elements[0]["operations"], ["TYPE_TEXT", "CLICK"])
        self.assertEqual(targets["TYPE_TEXT"]["1"]["id"], "e1")
        self.assertEqual(targets["CLICK"]["1"]["id"], "e2")
        self.assertEqual(targets["CLICK"]["2"]["id"], "e3")
        self.assertIn("WAIT", controls)

    def test_missing_key_fails_before_any_network(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            with self.assertRaises(ValueError):
                model.choose(page(), "goal", [])


class AgentTests(unittest.TestCase):
    def test_click_then_done(self):
        b = FakeBrowser()
        with patch.object(loop, "choose", side_effect=[decision("e3"), decision("DONE", "DONE")]):
            report = loop.Agent(b, "press go").run()
        self.assertEqual(report["status"], "done")
        self.assertEqual(b.acted, [("e3", None)])
        self.assertEqual(report["steps"], 1)
        self.assertIn("verify", report)

    def test_fill_uses_text_helper_once(self):
        b = FakeBrowser()
        with patch.object(loop, "choose", side_effect=[decision("e1", "TYPE_TEXT"), decision("DONE", "DONE")]), \
             patch.object(loop, "field_text", return_value=("hello", {"latency_ms": 5})) as ft:
            report = loop.Agent(b, "search hello").run()
        self.assertEqual(b.acted, [("e1", "hello")])
        self.assertEqual(ft.call_count, 1)
        self.assertIn('typed "hello"', report["trail"][0])

    def test_irreversible_click_is_refused(self):
        b = FakeBrowser()
        with patch.object(loop, "choose", side_effect=[decision("e4")]):
            report = loop.Agent(b, "buy it").run()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("irreversible", report["reason"])
        self.assertEqual(b.acted, [])

    def test_irreversible_allowed_when_opted_in(self):
        b = FakeBrowser()
        with patch.object(loop, "choose", side_effect=[decision("e4"), decision("DONE", "DONE")]):
            report = loop.Agent(b, "buy it", allow_irreversible=True).run()
        self.assertEqual(b.acted, [("e4", None)])
        self.assertEqual(report["status"], "done")

    def test_sign_in_is_not_irreversible(self):
        self.assertIsNone(loop.IRREVERSIBLE.search("Sign in"))
        self.assertIsNone(loop.IRREVERSIBLE.search("Sign up"))
        self.assertIsNotNone(loop.IRREVERSIBLE.search("Sign transaction"))
        self.assertIsNotNone(loop.IRREVERSIBLE.search("Send"))
        self.assertIsNone(loop.IRREVERSIBLE.search("Search"))

    def test_budget_stops_a_spinning_loop(self):
        b = FakeBrowser()
        b.fresh = lambda page, action=None: action is None  # every click goes stale, never executes
        b.act = lambda action, page, text=None: (_ for _ in ()).throw(StalePage("covered"))
        with patch.object(loop, "choose", return_value=decision("e3")):
            report = loop.Agent(b, "loop", max_steps=5).run()
        self.assertEqual(report["status"], "budget")
        self.assertEqual(report["model_calls"], 10)

    def test_three_no_change_actions_block(self):
        b = FakeBrowser()
        b.act = lambda action, page, text=None: None  # page never changes
        with patch.object(loop, "choose", return_value=decision("e3")):
            report = loop.Agent(b, "loop").run()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["steps"], 3)


class DecideTests(unittest.TestCase):
    """Bulk classification: batching, validation, no network without a key."""

    def setUp(self):
        from jev import decide as d
        self.d = d

    def fake_post(self, url, key, body, timeout=25):
        self.bodies.append(body)
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.9}
            else:
                opts = list(q["criteria"])
                answers[qid] = {"type": "choice", "choice": opts[0], "confidence": 0.8,
                                "probabilities": {o: (1.0 if o == opts[0] else 0.0) for o in opts}}
        return {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 100 * len(body["questions"])}}

    def test_batches_of_50_and_order(self):
        self.bodies = []
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), patch.object(self.d, "post_json", self.fake_post):
            out = self.d.decide([{"t": i} for i in range(120)], "q?", {"a": "A", "b": "B"})
        self.assertEqual(out["calls"], 3)
        self.assertEqual([len(b["questions"]) for b in self.bodies], [50, 50, 20])
        self.assertEqual([r["i"] for r in out["results"]], list(range(120)))
        self.assertTrue(all(r["choice"] == "a" for r in out["results"]))
        self.assertEqual(self.d.buckets(out), {"a": list(range(120))})
        self.assertAlmostEqual(out["approx_cost_usd"], 12000 * 0.042 / 1e6, places=4)

    def test_noul_and_context(self):
        self.bodies = []
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), patch.object(self.d, "post_json", self.fake_post):
            out = self.d.decide(["x", "y"], "spam?", kind="noul", context="policy")
        self.assertEqual([r["p"] for r in out["results"]], [0.9, 0.9])
        self.assertEqual(self.bodies[0]["state"]["context"], "policy")
        self.assertEqual(self.bodies[0]["state"]["items"][1], {"i": 1, "text": "y"})
        self.assertEqual(self.d.buckets(out), {"true": [0, 1]})

    def test_invalid_answers_become_errors_not_actions(self):
        def bad(url, key, body, timeout=25):
            return {"answers": {"r0": {"choice": "zzz", "probabilities": {"a": 1.0, "b": 0.0}},
                                "r1": {"choice": "a", "probabilities": {"a": 0.4, "b": 0.4}}}, "usage": {}}
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}), patch.object(self.d, "post_json", bad):
            out = self.d.decide(["p", "q"], "q?", {"a": "A", "b": "B"})
        self.assertTrue(all("error" in r for r in out["results"]))
        self.assertEqual(self.d.buckets(out), {"error": [0, 1]})

    def test_no_key_no_network(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": ""}):
            with self.assertRaises(ValueError):
                self.d.decide(["x"], "q?", {"a": "A", "b": "B"})
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}):
            with self.assertRaises(ValueError):
                self.d.decide(["x"], "q?", {"only": "one"})


class RipTests(unittest.TestCase):
    """Batch runner: lanes, parallelism, verify, tab bookkeeping. Agent and Browser are faked."""

    def setUp(self):
        from jev import rip as r
        self.r = r
        self.calls = []

    def post(self, cmd, args):
        self.calls.append((cmd, args))
        if cmd == "open":
            return {"tab_id": 900 + len([c for c in self.calls if c[0] == "open"])}
        return {}

    def fake_agent(self, outcome="done", delay=0.0):
        r = self.r
        calls = self.calls

        class B:
            def __init__(self, tab_id, post):
                self.tab_id = tab_id
            def evaluate(self, expr):
                return f"eval on {self.tab_id}"
            def close(self):
                calls.append(("close", self.tab_id))

        class A:
            def __init__(self, browser, goal, **kw):
                self.browser, self.goal = browser, goal
            def run(self):
                import time as _t
                _t.sleep(delay)
                calls.append(("run", self.browser.tab_id, self.goal))
                return {"status": outcome, "reason": "x", "steps": 1, "model_calls": 2, "approx_cost_usd": 0.0001,
                        "trail": ["1. CLICK a"], "page": {"url": "u", "title": "t", "text": "hello"}}
        return patch.object(r, "Agent", A), patch.object(r, "Browser", B)

    def test_lanes_same_tab_sequential_urls_independent(self):
        plans = [{"tab_id": 1, "goal": "a"}, {"url": "http://x", "goal": "b"}, {"tab_id": 1, "goal": "c"}, {"tab_id": 2, "goal": "d"}]
        self.assertEqual([[p["goal"] for p in lane] for lane in self.r._lanes(plans)], [["a", "c"], ["b"], ["d"]])

    def test_runs_all_verifies_and_closes_opened_tabs(self):
        pa, pb = self.fake_agent()
        with pa, pb:
            out = self.r.rip(self.post, [
                {"tab_id": 1, "goal": "a", "verify": "1+1"},
                {"url": "http://x", "goal": "b"},
                {"url": "http://y", "goal": "c", "keep": True},
                {"tab_id": 1, "goal": "d"},
            ], parallel=2)
        res = out["results"]
        self.assertEqual([r["goal"] for r in res], ["a", "b", "c", "d"])
        self.assertEqual(res[0]["verified"], "eval on 1")
        self.assertEqual({res[1]["tab_id"], res[2]["tab_id"]}, {901, 902})  # opened in parallel, either order
        self.assertTrue(res[1]["closed_tab"])
        self.assertNotIn("closed_tab", res[2])
        self.assertEqual(out["totals"]["done"], 4)
        runs = [c for c in self.calls if c[0] == "run" and c[1] == 1]
        self.assertEqual([c[2] for c in runs], ["a", "d"])  # same tab, in order
        closes = [c for c in self.calls if c[0] == "cdp" and c[1]["method"] == "Page.close"]
        self.assertEqual([c[1]["tab_id"] for c in closes], [res[1]["tab_id"]])

    def test_parallel_is_faster_than_serial(self):
        import time as _t
        pa, pb = self.fake_agent(delay=0.3)
        with pa, pb:
            t = _t.perf_counter()
            out = self.r.rip(self.post, [{"tab_id": i, "goal": "g"} for i in range(3)], parallel=3)
        self.assertLess(_t.perf_counter() - t, 0.7)
        self.assertEqual(out["totals"]["plans"], 3)

    def test_bad_plan_does_not_sink_batch(self):
        pa, pb = self.fake_agent()
        with pa, pb:
            out = self.r.rip(self.post, [{"goal": "no tab"}, {"tab_id": 5, "goal": ""}, {"tab_id": 6, "goal": "ok"}])
        self.assertEqual([r["status"] for r in out["results"]], ["error", "error", "done"])
        with self.assertRaises(ValueError):
            self.r.rip(self.post, [])


if __name__ == "__main__":
    unittest.main()
