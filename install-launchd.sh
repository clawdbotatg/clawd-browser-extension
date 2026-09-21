#!/bin/sh
# Install the bridge as a launchd user agent so it survives reboots and
# logouts (RunAtLoad + KeepAlive). Without this the bridge only starts on
# demand from mcp_server.py, and after every reboot the extension sits
# spamming ERR_CONNECTION_REFUSED until some Claude session happens to call
# a browser tool (2026-09-21).
#
# Idempotent: re-run after moving the checkout or changing the port.
#   CLAWD_BROWSER_PORT=8765 ./install-launchd.sh
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
LABEL=com.clawd.browser-bridge
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=${CLAWD_BROWSER_PORT:-8765}
BIND=${CLAWD_BROWSER_BIND:-0.0.0.0}
LOG="$HERE/bridge.log"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$HERE/bridge.py</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CLAWD_BROWSER_PORT</key><string>$PORT</string>
    <key>CLAWD_BROWSER_BIND</key><string>$BIND</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

# Stop any on-demand copy (mcp_server.py's ensure_bridge) so launchd can bind
# the port instead of crash-looping on EADDRINUSE.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -f "$HERE/bridge.py" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
if launchctl print "gui/$(id -u)/$LABEL" | grep -q 'state = running'; then
  echo "$LABEL running (port $PORT). log: $LOG"
else
  echo "$LABEL did not start — see $LOG" >&2
  exit 1
fi
