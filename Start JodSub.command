#!/bin/zsh
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/macos/"
  read -r
  exit 1
fi
python3 app.py >/tmp/jodsub-start.log 2>&1 &
server_pid=$!
for attempt in {1..20}; do
  if curl -fsS "http://127.0.0.1:8877/api/status" >/dev/null 2>&1; then break; fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat /tmp/jodsub-start.log
    exit 1
  fi
  sleep 0.5
done
open -a "Google Chrome" "http://127.0.0.1:8877/"
wait "$server_pid"
