#!/bin/bash
# One-click launcher for the Stock Flag Bot (macOS — double-click in Finder).
cd "$(dirname "$0")"
[ -f .envrc ] && source .envrc
echo "Starting Stock Flag Bot server on http://127.0.0.1:8765 ..."
echo "Keep this window open while trading. Close it (Cmd+W / Ctrl+C) to stop."
.venv/bin/python run_tradingview_bot.py --no-browser
