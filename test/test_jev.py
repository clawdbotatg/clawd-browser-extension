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


if __name__ == "__main__":
    unittest.main()
