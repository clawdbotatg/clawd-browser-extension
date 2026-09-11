// Pure helpers for the "open a session" link — no chrome.* here so node can
// unit-test them (test/link.test.mjs). The popup imports them as an ES module.

export const DEFAULT_PORT = 8765;
export const REPO_SKILL = "~/clawd/clawd-harness/projects/clawd-browser-extension/extension/skill.txt";

export const DEFAULTS = {
  harness: "https://h.atg.link",             // or http://127.0.0.1:8787 for one box
  project: "github.com/clawdbotatg/clawd-web", // fleet projectKey (direct mode resolves it too)
  machine: "",                               // fleet machine id of the box this browser runs on
  opener: "Read this tab and give me a short TLDR in plain english, then wait for my questions.",
};

// The paste-able blob the copy button makes: which tab, and how to drive it.
// `lan` is the bridge's GET /status "lan" object ({urls:[...]}) when the bridge
// answered: its token URLs make the paste work from ANY machine on the LAN —
// the paste is the credential. Without it (bridge down) the blob is the
// same-machine form, which still names the tab.
export function contextText(tab, port = DEFAULT_PORT, lan = null) {
  const urls = (lan && Array.isArray(lan.urls)) ? lan.urls.filter(Boolean) : [];
  const head = [
    "You can drive my real, logged-in browser (clawd-browser). I'm talking about this tab:",
    `  tab_id: ${tab.id}`,
    `  title: ${tab.title || "(untitled)"}`,
    `  url: ${tab.url || "(no url)"}`,
  ];
  if (!urls.length) {
    return [
      ...head,
      "",
      "If you have mcp__clawd-browser__* tools, use them directly — pass tab_id " + tab.id,
      "to target this tab (browser_read, browser_screenshot, browser_click, …).",
      "Otherwise, learn the API first — read the skill:",
      `  curl -s http://127.0.0.1:${port}/skill`,
      `  (or Read the file at ${REPO_SKILL} — it also covers starting the bridge)`,
      `If tab_id ${tab.id} no longer resolves, find the url above with browser_tabs.`,
    ].join("\n");
  }
  const [primary, ...alts] = urls;
  return [
    ...head,
    `  bridge: ${primary}`,
    ...alts.map((u) => `          (or ${u})`),
    "",
    "That bridge runs on the browser's machine and answers from any machine on my LAN;",
    "the /k/… path is its access token — keep it on every request, never send it elsewhere.",
    "Learn the API first — read the skill (already rewritten for that URL):",
    `  curl -s ${primary}/skill`,
    "If you have mcp__clawd-browser__* tools, use them ONLY if browser_tabs lists the url",
    `above (they may point at a different machine's browser); then pass tab_id ${tab.id}.`,
    `If tab_id ${tab.id} no longer resolves, find the url above with the tabs command.`,
  ].join("\n");
}

// The harness compose deep link (clawd-harness docs/DEEPLINKS.md):
//   <harness>/#/[m/<machine>/]p/<projectKey>/new?q=<text>&send=1
export function composeUrl({ harness, project, machine, text, send = true }) {
  const base = String(harness || DEFAULTS.harness).replace(/\/+$/, "");
  let hash = "#/";
  if (machine) hash += "m/" + encodeURIComponent(machine) + "/";
  hash += "p/" + encodeURIComponent(project || DEFAULTS.project) + "/new?q=" + encodeURIComponent(text);
  if (send) hash += "&send=1";
  return base + "/" + hash;
}

// Message for a new session about `tab`: the context blob plus the opener.
export function sessionText(tab, port, opener = DEFAULTS.opener, lan = null) {
  return contextText(tab, port, lan) + "\n\n" + opener;
}
