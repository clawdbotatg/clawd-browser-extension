"""Observed actions through the clawd-browser bridge: one real Chrome tab, raw CDP passthrough.

Ported from browser-use/jev-ultrafast (MIT). Their Browser owned a browser-harness target;
this one borrows a tab the user already has open, via the extension's "cdp" command.
Includes two fixes not (yet) upstream: covered elements are never offered (a calendar month
clipped by a scroller spun the loop for 113 calls), and Material-style hidden checkbox/radio
inputs are clicked through their <label>."""

import hashlib
import json
import sys
import time
from pathlib import Path

READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, tab_id, post):
        """post(cmd, args) -> result dict, raising RuntimeError when the bridge says not ok."""
        self.tab_id = tab_id
        self.post = post
        self.after_input = None
        # Keep rAF/menus rendering while the tab is in the background, like upstream.
        self.cdp("Emulation.setFocusEmulationEnabled", enabled=True)

    def cdp(self, method, **params):
        return self.post("cdp", {"tab_id": self.tab_id, "method": method, "params": params}) or {}

    def evaluate(self, expression):
        response = self.cdp("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self):
        if self.after_input:
            action, self.after_input = self.after_input, None
            # Read-only settle wait after an input; runs after the action was logged.
            try:
                self.cdp(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                info = self.evaluate(READ_STATE)
                if info is None:
                    raise StalePage("Document is navigating")
                info["fingerprint"] = fingerprint(info)
                return info
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.15)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            time.sleep(0.1)
            return {"executed": action["id"]}
        if kind == "scroll":
            self.cdp("Input.dispatchMouseEvent", type="mouseWheel", x=page["w"] / 2, y=page["h"] * 0.8,
                     deltaX=0, deltaY=action["delta"])
        else:
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = self.evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              const vis=n=>n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
              const p=(e && e.tagName==='INPUT' && ['checkbox','radio'].includes(e.type) && !vis(e) &&
                e.labels && e.labels[0]) ? e.labels[0] : e;
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !vis(p)) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=p.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!p.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    self.cdp("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    mods = 4 if sys.platform == "darwin" else 2
                    self.cdp("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=mods,
                             commands=["selectAll"])
                    self.cdp("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=mods)
                    self.cdp("Input.insertText", text=text)
        self.after_input = action
        return {"executed": action["id"]}

    def close(self):
        try:
            self.cdp("Emulation.setFocusEmulationEnabled", enabled=False)
        except RuntimeError:
            pass


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
