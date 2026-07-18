#!/usr/bin/env python
"""Run one watchlist scan against the Alpaca PAPER account.

Used by the GitHub Actions schedule and runnable by hand:
    python run_scanner.py            # real paper orders
    python run_scanner.py --dry-run  # print decisions, no orders

Env: ALPACA_API_KEY, ALPACA_SECRET_KEY (paper keys)
Optional: TV_BOT_NOTIONAL, TV_BOT_MIN_CONF, TV_BOT_MAX_POS, TV_BOT_MAX_DD,
          GITHUB_TOKEN + GITHUB_REPO[SITORY] (for the drawdown breaker to
          flip AUTO_TRADING off).
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Alpaca paper-trading watchlist scan")
    parser.add_argument("--dry-run", action="store_true", help="decide but don't submit orders")
    args = parser.parse_args()

    from tradingview_bot.scanner import run_scan
    result = run_scan(dry_run=args.dry_run)
    # non-zero exit only on config problems, so CI marks misconfig red
    if result["status"] in ("not_configured", "no_watchlist"):
        sys.exit(1)


if __name__ == "__main__":
    main()
