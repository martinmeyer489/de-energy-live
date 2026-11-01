#!/bin/sh
# Periodically refresh data/data.json with live data (every 15 min).
# Self-deduplicating via pidfile; safe to launch repeatedly. Logs to /tmp/mapfetch.log.
PIDFILE=/tmp/maprefresh.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

cd "$(dirname "$0")/.." || exit 1
while :; do
  python3 build/fetch_live.py >> /tmp/mapfetch.log 2>&1
  # battery layer (MaStR register) changes slowly — regenerate at most daily
  if [ -n "$(find data/batteries.json -mtime +1 2>/dev/null)" ] || [ ! -f data/batteries.json ]; then
    python3 build/build_batteries.py >> /tmp/mapfetch.log 2>&1
  fi
  sleep 1800
done
