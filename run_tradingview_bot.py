#!/usr/bin/env python
"""Run the TradingView sidebar bot (free tier: free Yahoo data + Gemini free tier).

Setup (one-time): get a free Gemini API key at https://aistudio.google.com/apikey
    export GEMINI_API_KEY='your-key'          # enables conversational chat
    # without a key the bot still works, using rule-based answers

Usage:
    python run_tradingview_bot.py                     # server + TradingView browser
    python run_tradingview_bot.py --symbol NSE:RELIANCE
    python run_tradingview_bot.py --no-browser        # API server only
    GEMINI_MODEL=gemini-2.5-flash-lite python run_tradingview_bot.py   # pick model

Then navigate between stocks inside TradingView — the sidebar bot follows the
active symbol, shows a live BUY/SELL/HOLD flag, and answers questions about it.
"""

import argparse
import threading
import time


def main():
    parser = argparse.ArgumentParser(description="TradingView stock flag bot")
    parser.add_argument("--port", type=int, default=8765, help="local API port (default 8765)")
    parser.add_argument("--symbol", default=None,
                        help="open this symbol's chart directly (default: stock browsing page)")
    parser.add_argument("--no-browser", action="store_true", help="run the API server only")
    args = parser.parse_args()

    from tradingview_bot import server

    if args.no_browser:
        print(f"🟢 API server on http://127.0.0.1:{args.port}  (GET /analyze?symbol=AAPL, POST /ask)")
        server.run(port=args.port)
        return

    t = threading.Thread(target=server.run, kwargs={"port": args.port}, daemon=True)
    t.start()
    time.sleep(1.0)
    print(f"🟢 Analysis server: http://127.0.0.1:{args.port}")

    from tradingview_bot import browser
    browser.launch(port=args.port, symbol=args.symbol)


if __name__ == "__main__":
    main()
