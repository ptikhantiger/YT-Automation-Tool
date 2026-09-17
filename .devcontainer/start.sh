#!/usr/bin/env bash
# Runs every time the codespace starts (postStartCommand): bring the dashboard
# up in the background so opening the codespace is all it takes -- the
# "Dashboard" port (8765) pops up as soon as it's listening. Idempotent: a
# second start while one is already running does nothing.
cd "$(dirname "$0")/.."

if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8765/; then
  echo "Dashboard already running on port 8765."
  exit 0
fi

mkdir -p /tmp/ytat
nohup python launch.py --no-browser --port 8765 > /tmp/ytat/dashboard.log 2>&1 &
echo "Dashboard starting on port 8765 (log: /tmp/ytat/dashboard.log)."
echo "Open it from the PORTS tab -> 'Dashboard', or the toast that appears."
