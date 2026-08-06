# 📈 Stock Analysis Agents

AI-powered institutional-grade stock analysis with SEC filings, social sentiment, and comprehensive market data. Built with OpenAI Agents SDK (gpt-5.4-mini).

https://github.com/user-attachments/assets/c1bbd4e1-9792-4940-a3ac-b5c86f9c13e4

## 🆕 TradingView Sidebar Bot (100% free tier)

Open TradingView with an embedded AI analyst that follows whatever stock you navigate to, computes a **BUY / SELL / HOLD flag**, and answers questions about it — powered by free Yahoo Finance data + the **Google Gemini free tier** (with a rule-based fallback if no API key is set).

```bash
uv sync
export GEMINI_API_KEY='your-key'       # free key: https://aistudio.google.com/apikey

uv run python run_tradingview_bot.py                  # opens TradingView + sidebar
uv run python run_tradingview_bot.py --no-browser     # API only: /analyze, /ask
```

The launcher drives your installed Chrome (falls back to Playwright Chromium — `uv run playwright install chromium` if you have neither). Without `GEMINI_API_KEY` the flag, score, and reasons still work; only the free-form chat degrades to a fixed-format summary.

How it works:
1. `run_tradingview_bot.py` starts a local FastAPI server (port 8765) and opens `tradingview.com/chart` in Chromium via Playwright, injecting a sidebar chat panel.
2. As you navigate between symbols on TradingView, the sidebar detects the active symbol and calls `/analyze`, which runs the **multi-strategy technical engine** on weekly/daily/4H/1H data.
3. Ask questions in the sidebar ("why this flag?", "where is the demand zone?", "any divergence?") — answered by Gemini (free tier) grounded in the computed evidence.

**Strategies implemented** (`stocks_agent/technicals/`):

| Category | Strategies |
|----------|-----------|
| Price action & structure | HH/HL–LH/LL market structure, BOS & CHOCH, supply/demand zones, order blocks, liquidity sweeps |
| Indicator confluence | Multi-timeframe alignment (1W/1D/4H), RSI & MACD divergence, volume profile (VPOC + value area), anchored VWAP |
| Pattern-based | Harmonic patterns (Gartley, Bat, Butterfly, Crab), Elliott-wave heuristic, Wyckoff accumulation/distribution (spring/upthrust) |
| Quant/systematic | Z-score & Bollinger mean reversion with volatility filter, EMA/golden-cross trend following with ADX, Donchian breakouts with volume confirmation |
| Options-derived | Put/call OI & volume ratios, ATM implied volatility |

Every strategy emits a weighted signal; the scoring engine aggregates them into a score in [-1, 1] → **BUY** (≥ +0.22), **SELL** (≤ −0.22), else **HOLD**, plus confidence and top bullish/bearish reasons. The same engine is exposed to all agents as the `get_technical_flag` tool.

> Note: TradingView is used as the charting UI; price data comes from yfinance (free) for the same symbol. Exchange-prefixed symbols (e.g. `NSE:RELIANCE`, `BINANCE:BTCUSDT`) are mapped automatically.

### 🧩 Chrome extension (recommended over the Playwright launcher)

The sidebar is also packaged as a Chrome extension in [chrome_extension/](chrome_extension/) — it works in your normal Chrome on any TradingView tab, no bot-launched browser needed. One-time install (free):

1. Open `chrome://extensions` in Chrome
2. Toggle **Developer mode** (top right)
3. Click **Load unpacked** → select the `chrome_extension/` folder

Then just keep the analysis server running (`python run_tradingview_bot.py --no-browser`) and browse tradingview.com normally — the sidebar appears on every chart/symbol page. API calls route through the extension's background service worker, so no CSP or local-network workarounds are needed.

### 📄 Alpaca paper trading + after-hours automation

Free Alpaca **paper** account (fake money) integration, two ways to use it:

- **Manual:** the sidebar's **📄 Paper trade** button trades the current flag (BUY → buy `$TV_BOT_NOTIONAL`, SELL → close the position).
- **Automated:** `.github/workflows/paper-trader.yml` runs `run_scanner.py` on GitHub's servers every 30 min during US market hours — your laptop can be off. The scanner reconciles [watchlist.txt](watchlist.txt) flags with current Alpaca positions (long-only, stateless; Alpaca is the source of truth).

**The ON/OFF switch** is the repo variable `AUTO_TRADING`: the scheduled job runs only when it equals `on` (skipped = zero Actions minutes). Flip it with the sidebar's **⏻ Auto** toggle (needs `GITHUB_TOKEN`+`GITHUB_REPO` in `.envrc`) or on github.com → Settings → Secrets and variables → Actions → Variables. The manual "Run workflow" button bypasses the gate for testing.

**Guardrails:** market-clock check, confidence ≥ 0.55 to buy, max 10 positions, and a daily drawdown circuit breaker (equity −3% vs yesterday → flips `AUTO_TRADING` off and stops). If Yahoo rate-limits GitHub's IPs, the scanner falls back to Alpaca's free IEX daily bars.

**Short-swing exits** (the backtest showed the engine's edge concentrates in ~5 days): held positions are closed by the first rule that fires — **stop-loss −4%**, **take-profit +5%**, **SELL flag**, or **time-exit after 7 trading days**. Tune/disable via `TV_BOT_STOP_LOSS`, `TV_BOT_TAKE_PROFIT`, `TV_BOT_MAX_HOLD_DAYS`. Sell trades record which rule fired (`reason`).

**Tiered breakeven stop** (both markets): once a position's peak gain reaches **+2.5%** (`TV_BOT_BREAKEVEN_ARM`), the stop moves up to a floor set by the symbol's **tier** in the watchlist:

- **winner** → floor **0%** (`TV_BOT_FLOOR_WINNER`) — pure breakeven; gives the stock room to run to the +5% target. A faded winner exits at ~breakeven, not a loss.
- **mediocre** → floor **+1%** (`TV_BOT_FLOOR_MEDIOCRE`) — locks a small profit; for stocks that pop then fade.

Tag a symbol on the [/watchlists](http://127.0.0.1:8765/watchlists) page (winner/mediocre toggle) or in the file directly: `AAPL winner` / `TSLA mediocre` (untagged = `TV_BOT_DEFAULT_TIER`, default winner). The floor is derived from daily-bar peak-since-entry (no stored state, cloud-safe). Every sell records its `tier` (NSE trade log; encoded in the Alpaca `client_order_id` as `tvbot-W-`/`tvbot-M-`) so you can compare which floor made more money. `TV_BOT_BREAKEVEN_ARM=0` disables the whole mechanism.

**Setup:** add `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` as repo **secrets**, create the `AUTO_TRADING` **variable** (value `off`), edit `watchlist.txt`, and test with `python run_scanner.py --dry-run`.

### 🇮🇳 NSE virtual paper broker

Alpaca can't trade Indian stocks, so NSE symbols get a **built-in virtual broker**: [watchlist_in.txt](watchlist_in.txt) is scanned during NSE hours (9:15–15:30 IST) by the same workflow, with simulated fills at the latest Yahoo price (whole shares, long-only, same confidence/max-position rules). The portfolio lives in `nse_portfolio.json` **tracked in the repo** — GitHub Actions commits it after each trading run, so state survives with your laptop off.

- **View it:** the `/performance` page shows equity, positions, P&L, and recent trades in ₹.
- **Reset it:** same page — type a starting amount and click **Reset portfolio**. Syncing the reset to GitHub from the button needs the fine-grained token to also have **Contents: Read and write**; otherwise the reset saves locally and you commit/push `nse_portfolio.json` yourself.
- Defaults: ₹10,00,000 start, ₹1,00,000 per position (`TV_BOT_START_CASH_INR`, `TV_BOT_NOTIONAL_INR`).
- Same `AUTO_TRADING` switch gates both markets; `python run_scanner.py --market in --dry-run` tests NSE only.

### 📓 Flag journal & performance scoreboard

Every fresh flag the server generates is logged to `data/flag_journal.db` (SQLite, gitignored). Once flags are 5/10/20 trading days old, outcomes are fetched automatically and scored (BUY correct if price rose, SELL if it fell).

- **Scoreboard:** open <http://127.0.0.1:8765/performance> (also linked from the sidebar footer), or `python -m tradingview_bot.journal` from the CLI. JSON at `/performance.json`.

### 🧪 Backtester

Replay the engine over years of history — every 5 trading days it computes the flag using only past data, then measures forward returns:

```bash
python run_backtest.py                          # default 10-stock basket, 5y
python run_backtest.py --years 3y RELIANCE.NS TCS.NS
```

Reports hit-rate per flag vs the buy&hold baseline **and per-strategy edge** (was each strategy's vote direction right more often than drift?), saving CSVs to `backtest_results/`. Backtests use 1W+1D signals only — intraday and options history aren't available on the free tier.


**17 analysis tools** covering fundamentals, SEC filings, social sentiment, earnings, news, and screening.

## ✨ Key Features

✅ **SEC Filing Analysis** - 10-K/10-Q with period comparisons  
✅ **Social Sentiment** - High-engagement Twitter/X and Reddit analysis  
✅ **Real-time Market Data** - Yahoo Finance integration  
✅ **EPS Trend Analysis** - Historical tracking with analyst revisions  
✅ **Comprehensive Earnings** - Estimates, revisions, growth projections  
✅ **Advanced Screening** - Value/growth company filters  
✅ **Structured Outputs** - 30-field Pydantic models for programmatic use  
✅ **Conversational Memory** - Context-aware follow-up questions  






```
