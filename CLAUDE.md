# clawd-browser-extension — rules for Claude

- **A change to `bridge.py` is not live until the bridge process is restarted.**
  It's a bare nohup process, no launchd, no file watcher. Commit, then
  `pgrep -fl clawd-browser-extension/bridge.py`, kill it, start it again, and
  verify `curl -s 127.0.0.1:8765/status` on the new process. Compare the process
  start time to the commit time before debugging "the feature doesn't work".
- **A change to `extension/` needs a reload in `chrome://extensions` in every
  Chrome profile.** `bridge.log` prints each profile's version on connect
  (`extension hello: x.y.z`).
- Tests: `node --test test/` and `node test/e2e.mjs`.
- Never commit `.clawd-browser.token` or `bridge.log` (gitignored).
- `browser_run` (the Jev loop in `jev/`) is fast hands, not a brain: it only sees
  on-screen controls, its DONE is a guess you must verify with `browser_read` or a
  screenshot, a `blocked`/`budget` result means change the plan (never rerun the
  same goal), and it must never be pointed at buy/pay/send/post/sign/delete actions
  on Austin's real accounts (`IRREVERSIBLE` in `jev/agent.py` refuses those labels;
  it is a heuristic). Full list in README "Caveats". Keys in `.env` (gitignored).
  Offline tests: `python3 test/test_jev.py`.
- The extension's `cdp` command is a raw CDP passthrough for `jev/`; changing it
  needs an extension reload (`{"cmd":"reload_extension","target":"<id>"}` works).
- `browser_rip` / `browser_decide` (v0.10.0, `jev/rip.py`, `jev/decide.py`) are the
  bulk versions: many goals across tabs, or one question over many items in one
  call. The fast pattern for chores is extract → decide → act in bulk; reach for
  `rip` only when each item needs real clicking. `verified` (your JS) beats DONE.
