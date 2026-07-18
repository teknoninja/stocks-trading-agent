#!/usr/bin/env python
"""Walk-forward backtest of the flag engine on historical data (free tier).

Usage:
    python run_backtest.py                          # default 10-stock basket
    python run_backtest.py AAPL MSFT NVDA TSLA      # your own symbols
    python run_backtest.py --years 3y --step 5 RELIANCE.NS TCS.NS

Outputs hit-rates per flag type, per-strategy edge, and saves the raw
samples to backtest_results/ as CSV.
"""

import argparse
import os

DEFAULT_BASKET = ["AAPL", "MSFT", "NVDA", "GOOG", "AMZN", "TSLA", "JPM", "XOM", "JNJ", "CAT"]


def main():
    parser = argparse.ArgumentParser(description="Backtest the BUY/SELL/HOLD flag engine")
    parser.add_argument("symbols", nargs="*", default=None, help="yfinance symbols")
    parser.add_argument("--years", default="5y", help="history window (default 5y)")
    parser.add_argument("--step", type=int, default=5, help="trading days between samples (default 5)")
    args = parser.parse_args()
    symbols = args.symbols or DEFAULT_BASKET

    from stocks_agent.technicals import backtest

    print(f"Backtesting {len(symbols)} symbols over {args.years}, sampling every {args.step} trading days…")
    print("(1W + 1D signals only — intraday/options history isn't available on free tier)\n")

    result = backtest.run(symbols, years=args.years, step=args.step)
    if "error" in result:
        print("ERROR:", result["error"], "| skipped:", result.get("skipped"))
        return

    print(f"Samples: {result['samples']}  |  symbols: {', '.join(result['symbols_tested'])}")
    if result["skipped"]:
        print(f"Skipped (no data): {', '.join(result['skipped'])}")

    print("\n=== Flag performance vs buy&hold baseline ===")
    print(result["flag_stats"].to_string(index=False))

    print("\n=== Per-strategy edge (10-day horizon, signed by vote direction) ===")
    print(result["strategy_stats"].to_string(index=False))

    os.makedirs("backtest_results", exist_ok=True)
    raw = result["raw"].drop(columns=["signals"])
    raw_path = os.path.join("backtest_results", "samples.csv")
    raw.to_csv(raw_path, index=False)
    result["flag_stats"].to_csv(os.path.join("backtest_results", "flag_stats.csv"), index=False)
    result["strategy_stats"].to_csv(os.path.join("backtest_results", "strategy_stats.csv"), index=False)
    print(f"\nSaved: {raw_path}, flag_stats.csv, strategy_stats.csv")
    print("Reminder: past performance ≠ future results. Educational only.")


if __name__ == "__main__":
    main()
