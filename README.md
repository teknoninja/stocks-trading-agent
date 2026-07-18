# 📈 Stock Analysis Agents

AI-powered institutional-grade stock analysis with SEC filings, social sentiment, and comprehensive market data. Built with OpenAI Agents SDK (gpt-5.4-mini).

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

## 🎯 Overview

Four intelligent agent types for different analysis needs:

- **SimpleAgent** - Stateless Q&A for quick lookups (OpenAI API)
- **ConversationAgent** - Memory-enabled multi-turn conversations with auto-ticker tracking (OpenAI API)
- **StructuredAgent** - Returns narrative + structured JSON (30 fields) using Pydantic Structured Outputs (OpenAI API)
- **FreeAgent** - Same functionality as SimpleAgent but uses free local Ollama models (Qwen, Llama, etc.)

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

## 🚀 Quick Start

```bash
# 1. Install dependencies
uv sync

# 2. Set up environment variables
cp .envrc.example .envrc
# Edit .envrc with your API keys:
#   - OPENAI_API_KEY (required)
#   - SEC_IDENTITY_EMAIL (required for SEC filings)
#   - POLYGON_API_KEY (optional)
direnv allow .

# 3. Activate and test
source .venv/bin/activate
python -c "from stocks_agent import SimpleAgent; print('✅ Ready!')"
```

## 🤖 Agents Usage

### SimpleAgent
Stateless - each query is independent.

```python
from stocks_agent import SimpleAgent

agent = SimpleAgent(model="gpt-5.4-mini")
response = await agent.ask("What's AAPL's PE ratio and recent SEC filings?")
```

### ConversationAgent
Maintains context across questions.

```python
from stocks_agent import ConversationAgent

agent = ConversationAgent(track_tickers=True)
await agent.ask("Analyze TSLA's latest 10-Q filing")
await agent.ask("What about social sentiment?")  # Auto-knows TSLA
await agent.ask("Compare to competitors")        # Still TSLA

agent.switch_to("AAPL")  # Switch ticker
agent.reset()            # Clear history
```

### StructuredAgent
Returns both text analysis AND structured data (30 fields).

```python
from stocks_agent import StructuredAgent

agent = StructuredAgent()
text, data = await agent.analyze('NVDA')

# Access structured fields
print(data['pe_ratio'])                  # 45.57
print(data['eps_trend_direction'])       # "improving"
print(data['valuation_level'])           # "expensive"
print(data['analyst_sentiment'])         # "improving"
print(data['social_sentiment'])          # "Bullish discussions..."
print(data['recommendation'])            # "buy"
print(data['confidence_score'])          # 8/10
```

### FreeAgent
Uses free local Ollama models instead of OpenAI API. No API costs!

```python
from stocks_agent import FreeAgent

# Setup (one-time)
# 1. Install Ollama: https://ollama.com/download
# 2. Pull model: `ollama pull qwen3:32b`  
# 3. Start Ollama: `ollama serve`

agent = FreeAgent(model='qwen3:32b')
response = await agent.ask("Analyze AAPL's valuation")

# Check status
print(agent.get_status())  # ✅ Connected to Ollama
print(agent.list_models()) # See recommended models
```

**Recommended Models:**
- `qwen3:32b` (best) - Excellent tool calling, requires 32GB+ RAM
- `qwen3:14b` (good) - Solid performance, requires 8GB+ RAM  
- `llama3.1:8b` (fast) - Lightweight, requires 4GB+ RAM

## 🛠️ Available Tools (17)

### 📊 Core Fundamentals (7 tools)
| Tool | Description |
|------|-------------|
| `get_company_info_basic` | Essential metrics (15 fields) |
| `get_company_info` | Comprehensive company data |
| `get_eps_trend` | EPS estimates across time periods |
| `get_earnings_dates` | Earnings calendar with surprises |
| `get_earnings_analysis` | Analyst estimates, revisions, growth projections |
| `get_historical_prices` | OHLCV data with momentum indicators |
| `get_ticker_news` | Latest news articles |

### 📋 SEC Filings (1 tool)
| Tool | Description |
|------|-------------|
| `get_sec_filing` | 10-K/10-Q filing text with period comparisons |

### 💬 Social Sentiment (3 tools)
| Tool | Description |
|------|-------------|
| `get_twitter_posts_by_engagement` | Viral Twitter/X posts sorted by engagement |
| `get_reddit_discussions_by_impact` | Reddit posts sorted by impact score |
| `get_social_sentiment` | Combined Twitter + Reddit analysis |

### 🔍 Search & Screening (6 tools)
| Tool | Description |
|------|-------------|
| `search_news_by_ticker` | Keyword-filtered news for ticker |
| `search_news_by_query` | General news search |
| `search_companies` | Advanced company filtering |
| `get_top_value_companies` | Value stock screener |
| `get_top_growth_companies` | Growth stock screener |
| `WebSearchTool` | General web search for context |

## 📊 Structured Output Models

### Production: StockAnalysisOutput (30 fields)
Used by `StructuredAgent` - comprehensive institutional analysis.

**Field Categories:**
- **Basic Info** (4): ticker, company_name, sector, industry
- **EPS & Earnings** (3): estimates, trend, surprise %
- **Valuation** (8): PE, forward PE, PEG, P/B, price, target, distance from highs/lows
- **Analyst Data** (4): count, revisions, sentiment, targets
- **Market Activity** (3): news count, sentiment score, social sentiment
- **Technical** (2): momentum, volatility
- **Investment Summary** (5): thesis, catalysts, risks, recommendation, confidence
- **Analysis** (1): comprehensive narrative

### Tutorial: SimpleStockAnalysis (15 fields)
Simpler version demonstrated in `notebooks/1_tools_and_sample_agents.ipynb`.

**Use StockAnalysisOutput for production, SimpleStockAnalysis for learning.**

## 📁 Project Structure

```
stocks-scoring-agent/
├── stocks_agent/
│   ├── tools.py              # 17 analysis tools
│   ├── simple_agent.py       # Stateless agent (OpenAI)
│   ├── conversation_agent.py # Memory-enabled agent (OpenAI)
│   ├── structured_agent.py   # Structured output agent (OpenAI, 30 fields)
│   └── free_agent.py         # Local Ollama agent (Qwen, Llama, etc.)
├── notebooks/
│   ├── 0_api_endpoints_test_data.ipynb    # API testing
│   ├── 1_tools_and_sample_agents.ipynb    # Tool demos & tutorials
│   └── 2_testing_py_code.ipynb            # Agent testing
├── .envrc.example            # Environment template
├── pyproject.toml            # Dependencies
└── README.md
```

## 🔧 Setup Details

### Dependencies
Managed with `uv`. Key libraries:
- `openai-agents` - AI agent framework
- `yfinance` - Market data
- `edgar` - SEC filing access
- `pydantic` - Structured outputs
- `jupyter` - Notebook support

### Environment Variables
Required:
```bash
export OPENAI_API_KEY='your-openai-key'
export SEC_IDENTITY_EMAIL='your-email@example.com'  # For SEC API
```

Optional:
```bash
export XAI_API_KEY='your-xai-key'
export POLYGON_API_KEY='your-polygon-key'
```

### Jupyter Setup
```bash
source .venv/bin/activate
python -m ipykernel install --user --name=stocks-scoring-agent
# Then select "stocks-scoring-agent" kernel in VS Code
```

## 📚 Learning Path

1. **Start here:** `notebooks/1_tools_and_sample_agents.ipynb` - Learn tools & basic agents
2. **Test agents:** `notebooks/2_testing_py_code.ipynb` - Test production code
3. **Explore API:** `notebooks/0_api_endpoints_test_data.ipynb` - Raw API testing

## ⚠️ Disclaimer

**For research and educational purposes only.** Not financial advice. The author is not responsible for financial losses. Always conduct your own research and consult financial advisors before making investment decisions.

## 📄 License

See LICENSE file.