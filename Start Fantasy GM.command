#!/bin/bash
# Double-click to start Fantasy GM. Close this window (or press Ctrl+C) to stop it.
cd "$(dirname "$0")" || exit 1
if [ -x "$HOME/anaconda3/bin/python3" ]; then PY="$HOME/anaconda3/bin/python3"; else PY="$(command -v python3)"; fi
if [ -z "$PY" ]; then echo "Python 3 was not found. Install it from python.org and try again."; read -r -p "Press Return to close."; exit 1; fi
# Stop any copy that is still running from earlier, so the newest version always starts.
pkill -f "fantasy_gm.py" 2>/dev/null && sleep 1
"$PY" fantasy_gm.py
status=$?
if [ $status -ne 0 ] && [ $status -ne 130 ]; then
  echo
  echo "Fantasy GM stopped with an error (see the message above)."
  read -r -p "Press Return to close this window."
fi
