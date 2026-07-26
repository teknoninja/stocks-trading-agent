# Project Handoff & Context

This document lets the project (and any AI assistant) continue seamlessly on a
**different machine or Claude account**. It captures the full context that was
built up in conversation, so nothing is lost when you switch to your personal
Claude account. Everything here is account-agnostic — it travels with the git repo.

> **How to continue on your personal Claude account:** clone this repo on your
> personal machine, open Claude Code (signed into your personal account) in the
> folder, and point it at this file. The AI reads this + the docs and has full context.

---

## 1. What this project is

A **free-tier AI stock-analysis assistant**. It shows a sidebar on TradingView that,
for whatever stock you're viewing, computes a **BUY / SELL / HOLD flag** from a
17-strategy technical engine and answers questions about it (via the Gemini LLM,
grounded on the computed evidence). It also **paper-trades automatically** on a
schedule — US stocks through Alpaca's paper API, Indian stocks through a built-in
virtual broker — running in the cloud (GitHub Actions) even with the laptop off.

**Core design decision:** the buy/sell decision is made by **deterministic maths**,
not by the LLM. The LLM only *explains*. (Reproducible, backtestable, no hallucinated
levels.)

## 2. Architecture (one line each)

- `stocks_agent/technicals/` — the engine: `data.py` (Yahoo fetch), strategy modules
  (`structure`, `zones`, `quant`, `divergence`, `patterns`, `volume_profile`,
  `options_signals`), `scoring.py` (weighted vote → flag), `engine.py` (orchestrator),
  `backtest.py` (walk-forward).
- `tradingview_bot/` — the product: `server.py` (FastAPI), `scanner.py` (autonomous
  trader + exits), `alpaca.py` (US broker), `virtual_broker.py` (NSE sim broker),
  `watchlists.py`, `journal.py`.
- `chrome_extension/` — the sidebar (Manifest V3): `content.js` + `background.js`.
- `.github/workflows/paper-trader.yml` — the scheduled cloud scanner.
- `docs/` — full documentation PDFs + `system-design.html` + these Claude context notes.

Full detail is in **`docs/Stock-Flag-Bot-Documentation.pdf`** and
**`docs/Stock-Flag-Bot-Code-Walkthrough.pdf`** (line-by-line). Interview prep is in
**`docs/AI-ML-Interview-Study-Guide.pdf`**.

## 3. How it's operated (the daily flow)

- **Daily use is on a personal Windows laptop** (folder `stocks-scoring-agent-share`),
  not the dev machine. Changes are edited/tested on the dev machine, zipped, and
  committed/pushed from Windows to the personal GitHub repo.
- **Always `git pull` before pushing** — the bot itself commits to the repo
  (`nse_portfolio.json` from cloud runs, watchlist edits from the ★ button). On a
  `nse_portfolio.json` conflict, keep the **GitHub/remote** copy.
- Run locally: `python run_tradingview_bot.py --no-browser` (server) + load the Chrome
  extension unpacked. Scanner: `python run_scanner.py --dry-run`.

## 4. Keys / secrets (NOT in the repo — set these on the new machine)

Stored via `setx` on Windows / `.envrc` locally, and as GitHub repo **Secrets**:
- `GEMINI_API_KEY` — chat (free tier). Model ladder starts `gemini-flash-latest`.
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — US paper account (keys start `PK`).
- `GITHUB_TOKEN` (fine-grained, Variables + Contents r/w on the repo) + `GITHUB_REPO`
  — lets the local server flip the Auto toggle and sync portfolio/watchlists.
- Repo **variable** `AUTO_TRADING` = `on`/`off` — the after-hours master switch.

## 5. Current strategy config (as last set)

- **Entry:** BUY flag + confidence ≥ 0.55, max 10 positions, $1,000 (US) / ₹1,000 (NSE)
  per position.
- **Exits (both markets):** stop-loss −4%, take-profit +5%, SELL flag, time-exit 7
  trading days — first to fire wins.
- **Tiered breakeven stop:** once a position peaks at **+2.5%**, its stop moves up to a
  floor set by the symbol's **tier** in the watchlist:
  - `winner` → floor **0%** (breakeven; give room to run to +5%)
  - `mediocre` → floor **+1%** (lock a small profit)
  Tag in the watchlist file (`AAPL winner` / `TSLA mediocre`), on the `/watchlists`
  page, or via the sidebar tier pill. Every sell records its `tier` so you can compare
  which floor made more money. Env vars: `TV_BOT_BREAKEVEN_ARM`, `TV_BOT_FLOOR_WINNER`,
  `TV_BOT_FLOOR_MEDIOCRE`.
- Drawdown circuit breaker: equity −3% in a day → flips `AUTO_TRADING` off.

## 6. Watchlists = the scanner's universe

`watchlist.txt` (US → Alpaca) and `watchlist_in.txt` (NSE → virtual broker). One symbol
per line, optional tier tag. The scanner ONLY trades symbols listed here.

## 7. Open ideas / parked next steps

- Tune strategy weights from backtest evidence (early runs: `supply_demand` strongest;
  `mean_reversion` / `liquidity_sweep` weak).
- Full 5-year, 10-stock backtest to validate the exit tiers.
- Possible ML layer: gradient-boosted classifier over the 17 signals (meta-labeling).
- Real-money transition guide is in the main documentation PDF (§10) — gated on months
  of paper evidence + employer compliance.

## 8. Important honesty note

This is a **personal project** that was developed on a company laptop / company Claude
usage. The code and docs are the user's own and live in the user's **personal** GitHub.
It is educational — **not financial advice** — and trades **paper (fake) money** only.
