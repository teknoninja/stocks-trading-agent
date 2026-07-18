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
    parser = argparse.ArgumentParser(description="Paper-trading watchlist scan (US via Alpaca, NSE via virtual broker)")
    parser.add_argument("--dry-run", action="store_true", help="decide but don't submit orders")
    parser.add_argument("--market", choices=["us", "in", "both"], default="both",
                        help="which market(s) to scan (default: both — each skips when closed)")
    args = parser.parse_args()

    from tradingview_bot.scanner import run_scan, run_virtual_scan
    statuses = []
    if args.market in ("us", "both"):
        print("=== US watchlist (Alpaca paper) ===", flush=True)
        statuses.append(run_scan(dry_run=args.dry_run)["status"])
    if args.market in ("in", "both"):
        print("=== NSE watchlist (virtual broker) ===", flush=True)
        statuses.append(run_virtual_scan(dry_run=args.dry_run)["status"])
    # non-zero exit only on config problems, so CI marks misconfig red
    if statuses and all(s in ("not_configured", "no_watchlist") for s in statuses):
        sys.exit(1)


if __name__ == "__main__":
    main()
