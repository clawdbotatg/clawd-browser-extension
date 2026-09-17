"""Jev decision loop for clawd-browser: fast hands, slow brain.

Ported from browser-use/jev-ultrafast (MIT, https://github.com/browser-use/jev-ultrafast)
onto the clawd-browser bridge, so the loop runs inside the user's REAL Chrome tab instead
of a browser-harness tab. Claude stays the planner and verifier; Jev (TypeSafe's
choose-from-a-menu model) picks one observed element per step at ~150 ms and ~$0.0003.
A small LLM writes text only for TYPE_TEXT. Model output never becomes a selector,
coordinate or code: every action is an index into the elements we observed.
"""

from .agent import Agent
from .browser import Browser, StalePage

CAVEATS = """Read before trusting a run (learned 2026-09-17 on Google Flights):
- It only sees what is ON SCREEN. Anything below the fold or inside an inner scroll list
  (a filter popup, a long dropdown) does not exist to it, and it will not scroll to look.
  Give it goals whose targets are visible, or scroll/open things for it first.
- DONE is a guess, not proof. It called DONE before results had rendered. Always verify
  the outcome yourself (browser_read / browser_screenshot) before reporting success.
- When a target is impossible it loops (48 clicks on the same switch). The step budget
  stops it. A 'blocked' or 'budget' result means change the plan, never rerun the same goal.
- It runs on the user's real, logged-in accounts. Never give it a goal that buys, pays,
  sends, posts, publishes, signs, approves, transfers or deletes. Code refuses to click
  labels that look like that (a word-match heuristic, not a guarantee); do those yourself
  with an explicit confirm from the user.
- Text fields are filled by a small LLM (Mercury via OpenRouter) from the goal. Check the
  typed values in the trail; it can invent.
- Unsupported: shadow DOM, iframes, canvas, file uploads, pop-up windows. Material-style
  hidden checkboxes/radios are handled through their label.
- Cost: about $0.0003 per decision; a full run is under two cents. The cap is
  max_steps actions / 2x that in model calls."""

__all__ = ["Agent", "Browser", "StalePage", "CAVEATS"]
