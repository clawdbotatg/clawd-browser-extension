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
