#!/bin/sh
# Periodically refresh data/data.json (every 30 min).
# Self-deduplicating via pidfile; safe to launch repeatedly.
# Logs to /tmp/pricemapfetch.log.
PIDFILE=/tmp/pricemaprefresh.pid
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

cd "$(dirname "$0")" || exit 1
while :; do
  python3 fetch_data.py >> /tmp/pricemapfetch.log 2>&1
  sleep 1800
done
