#!/bin/bash
# jev_run.sh <tab_id | url-substring> "goal" [extra args]  — wrapper so the skill can quote one path.
cd "$(dirname "$0")" || exit 1
case "$1" in ''|*[!0-9]*) sel=--url;; *) sel=--tab;; esac
exec python3 -m jev $sel "$1" --goal "$2" "${@:3}"
