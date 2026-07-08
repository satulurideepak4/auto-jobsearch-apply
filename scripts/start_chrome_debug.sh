#!/usr/bin/env bash
# Launch a dedicated Chrome instance for portal automation (separate from your real Chrome).
#
# Uses ~/.auto-jobsearch/chrome-debug as the data dir — completely isolated.
# First run: Chrome opens blank, sign in to Google with sathulurideepak4@gmail.com,
#            then log into Naukri / Instahyre / Wellfound via Google.
# Every run after: already logged in.

set -euo pipefail

PORT=9222
DEBUG_DIR="$HOME/.auto-jobsearch/chrome-debug"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [[ ! -x "$CHROME_BIN" ]]; then
  echo "ERROR: Google Chrome not found at $CHROME_BIN"
  exit 1
fi

# Already running?
if curl -s --max-time 1 "http://localhost:$PORT/json/version" &>/dev/null; then
  echo "Chrome debug session already running on port $PORT"
  curl -s "http://localhost:$PORT/json/version" | python3 -c "import sys,json; print('Browser:', json.load(sys.stdin).get('Browser'))"
  exit 0
fi

mkdir -p "$DEBUG_DIR"

echo "Starting Chrome debug session..."
echo "  Data dir   : $DEBUG_DIR"
echo "  Debug port : $PORT"

"$CHROME_BIN" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$DEBUG_DIR" \
  --no-first-run \
  --no-default-browser-check \
  2>/dev/null &

echo "Waiting for port..."
for i in {1..20}; do
  if curl -s --max-time 1 "http://localhost:$PORT/json/version" &>/dev/null; then
    echo "Ready (${i}s) — $(curl -s http://localhost:$PORT/json/version | python3 -c "import sys,json; print(json.load(sys.stdin).get('Browser'))")"
    echo ""
    echo "First time? Sign in to Google with sathulurideepak4@gmail.com,"
    echo "then open naukri.com / instahyre.com / wellfound.com and log in via Google."
    echo ""
    echo "Then run:  .venv/bin/python scripts/test_naukri_apply.py"
    exit 0
  fi
  sleep 1
  echo -n "."
done

echo "Timeout — Chrome didn't open the debug port."
exit 1
