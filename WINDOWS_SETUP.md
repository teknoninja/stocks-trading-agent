# 🪟 Windows Setup — TradingView Stock Flag Bot

Verified flow for running the bot on a Windows laptop. Everything is free tier.

## 1. Prerequisites

- **Python 3.10+** — install from https://www.python.org/downloads/ (tick *"Add python.exe to PATH"* during install)
- **Google Chrome** installed (normal desktop Chrome)
- A **Gemini API key** (free): https://aistudio.google.com/apikey

## 2. Install (PowerShell, inside the unzipped folder)

```powershell
cd stocks-scoring-agent-main          # wherever you unzipped it
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If PowerShell blocks Activate.ps1, run once:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

## 3. Set your API key (the zip does NOT include one, on purpose)

```powershell
$env:GEMINI_API_KEY = "your-key-here"     # current window only
setx GEMINI_API_KEY "your-key-here"       # persist for future windows (reopen PowerShell after)
$env:PYTHONUTF8 = "1"                     # avoids emoji/console encoding issues on Windows
```

## 4. Run

**Option A — Chrome extension (recommended):**

```powershell
python run_tradingview_bot.py --no-browser
```

Then in Chrome: `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
select the `chrome_extension` folder. Browse https://www.tradingview.com — the
sidebar appears on every chart/symbol page.

**Option B — auto-launched browser (Playwright):**

```powershell
python run_tradingview_bot.py
```

This drives your installed Chrome. If Chrome isn't found it falls back to
Playwright's own Chromium — install it once with `python -m playwright install chromium`
(may fail behind a corporate proxy; Option A avoids this entirely).

## 5. Verify

- http://127.0.0.1:8765/health → should show `"status": "ok"` and `"llm": true`
- Open any stock on TradingView → sidebar shows a BUY/SELL/HOLD flag with reasons
- http://127.0.0.1:8765/performance → flag journal scoreboard
- Backtest: `python run_backtest.py --years 2y AAPL MSFT`

## Troubleshooting

| Symptom | Fix |
|---|---|
| SSL errors on all requests | Corporate proxy — the code already uses `truststore` (Windows cert store); make sure `pip install -r requirements.txt` completed |
| `analyze` returns rate-limit error | Yahoo throttling; wait a few minutes |
| Sidebar shows "server unreachable" | The Python server isn't running — keep the PowerShell window with `run_tradingview_bot.py --no-browser` open |
| Developer mode toggle greyed out in Chrome | Enterprise policy blocks unpacked extensions — use Option B instead |
| Emoji garbage/crash in console | `$env:PYTHONUTF8 = "1"` before running |
