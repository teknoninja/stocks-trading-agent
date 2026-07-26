---
name: stock-flag-bot-project
description: "AI stock flag bot — architecture, repo location, and operating setup as of July 2026"
metadata: 
  node_type: memory
  type: project
  originSessionId: 3920358b-7275-47f6-a920-a75550768c50
---

Complete free-tier AI stock trading assistant built July 2026. Repo: **github.com/teknoninja/stocks-trading-agent** (user's PERSONAL GitHub "teknoninja" — never push via the company account Shivam-Tyagi1108 logged into `gh` on this Mac; an accidental push there was reverted 2026-07-18).

**Daily use happens on the user's personal Windows laptop** (folder `C:\Users\tyagi\Downloads\stocks-scoring-agent-share`), not this company Mac. This Mac's copy is the dev workspace; changes are handed off as a zip (`~/Downloads/stocks-scoring-agent-share.zip`, excludes .envrc/data/venv/nse_portfolio.json) shared via Slack, then committed/pushed from Windows. Remind user to `git pull` before pushing — the bot itself commits to the repo (portfolio updates, watchlist edits).

Architecture: deterministic 17-strategy technical engine (`stocks_agent/technicals/`) → BUY/SELL/HOLD flag; FastAPI server on :8765 (`tradingview_bot/`); Chrome extension sidebar on TradingView (`chrome_extension/`, loaded unpacked); Gemini free tier for chat (key in gitignored `.envrc`, model ladder starting `gemini-flash-latest`); flag journal + backtester; US paper trading via Alpaca paper API; NSE paper trading via built-in virtual broker (`nse_portfolio.json` tracked in repo, ₹1000/position min-1-share); GitHub Actions scanner every 30 min during US + NSE market hours, gated on repo variable `AUTO_TRADING` (workflow: `.github/workflows/paper-trader.yml`).

Keys live as: Windows setx (GEMINI_API_KEY, ALPACA_API_KEY/SECRET, GITHUB_TOKEN fine-grained with Variables+Contents rw, GITHUB_REPO=teknoninja/stocks-trading-agent) and GitHub repo secrets. Watchlists = repo files `watchlist.txt` (US) / `watchlist_in.txt` (NSE) — the scanner's only universe.

Parked next steps the user showed interest in: tuning strategy weights from backtest evidence (early 2-symbol run: supply_demand strongest edge, mean_reversion/liquidity_sweep negative), per-position stop-loss for NSE, full 5y/10-stock backtest.
