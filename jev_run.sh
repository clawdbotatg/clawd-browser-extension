#!/bin/bash
# python3 -m jev needs the repo as cwd; this wrapper lets the skill quote one path.
cd "$(dirname "$0")" && exec python3 -m jev --tab "$1" --goal "$2" "${@:3}"
