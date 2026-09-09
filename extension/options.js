import { DEFAULTS } from "./link.js";

const KEYS = Object.keys(DEFAULTS);
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get(KEYS);
  for (const k of KEYS) $(k).value = s[k] || DEFAULTS[k];
}

async function save() {
  const out = {};
  for (const k of KEYS) out[k] = $(k).value.trim();
  await chrome.storage.local.set(out);
  $("saved").style.visibility = "visible";
  setTimeout(() => ($("saved").style.visibility = "hidden"), 1500);
}

$("save").addEventListener("click", save);
load();
