// Unit test for the pure link helpers (no browser): node test/link.test.mjs
import assert from "node:assert/strict";
import { composeUrl, contextText, sessionText, DEFAULTS } from "../extension/link.js";

const tab = { id: 42, title: "Example Domain", url: "https://example.com/a?b=1#c" };

// fleet form: machine prefix, encoded key, encoded text, send flag
const u = composeUrl({ harness: "https://h.atg.link/", project: "github.com/o/r", machine: "my-box", text: "hi / there?", send: true });
assert.equal(u, "https://h.atg.link/#/m/my-box/p/github.com%2Fo%2Fr/new?q=hi%20%2F%20there%3F&send=1");

// direct form: no machine, no send → draft only; defaults fill in
const d = composeUrl({ harness: "http://127.0.0.1:8787", text: "x", send: false });
assert.equal(d, "http://127.0.0.1:8787/#/p/" + encodeURIComponent(DEFAULTS.project) + "/new?q=x");

// the hash must not contain a raw "/" or "?" from the text (the harness splits on those)
const hash = new URL(u).hash;
assert.equal(hash.split("?").length, 2, "exactly one ? in the hash");
assert.ok(!decodeURIComponent(hash.split("?")[0]).includes("hi"), "text lives only in the query");

// context blob names the tab, and the session text ends with the opener
const c = contextText(tab, 8765);
assert.ok(c.includes("tab_id: 42") && c.includes("https://example.com/a?b=1#c"));
assert.ok(sessionText(tab, 8765).endsWith(DEFAULTS.opener));

// bridge up → the blob carries the LAN token URL(s), the skill pointer uses it,
// and the same-machine 127.0.0.1 pointer is gone (a remote reader must not try it)
const lan = { hosts: ["192.168.1.9", "box.local"], urls: ["http://192.168.1.9:8765/k/tok123", "http://box.local:8765/k/tok123"], token: "tok123" };
const l = contextText(tab, 8765, lan);
assert.ok(l.includes("bridge: http://192.168.1.9:8765/k/tok123"), "primary LAN url");
assert.ok(l.includes("(or http://box.local:8765/k/tok123)"), "fallback host");
assert.ok(l.includes("curl -s http://192.168.1.9:8765/k/tok123/skill"), "skill via token url");
assert.ok(!l.includes("127.0.0.1"), "no loopback pointer in the LAN form");
assert.ok(l.includes("tab_id: 42"));
assert.ok(sessionText(tab, 8765, DEFAULTS.opener, lan).startsWith(l) && sessionText(tab, 8765, DEFAULTS.opener, lan).endsWith(DEFAULTS.opener));
// bridge down / no addresses → the old same-machine form
assert.ok(contextText(tab, 8765, { urls: [] }).includes("http://127.0.0.1:8765/skill"));
assert.ok(contextText(tab, 8765, null).includes("http://127.0.0.1:8765/skill"));

// round trip: what the harness parses back equals what was sent
const text = sessionText(tab, 8765);
const q = new URLSearchParams(new URL(composeUrl({ text })).hash.split("?")[1]);
assert.equal(q.get("q"), text);
assert.equal(q.get("send"), "1");

console.log("PASS link.test.mjs");
