@echo off
REM One-click launcher for the Stock Flag Bot (Windows).
REM Assumes: venv created, requirements installed, GEMINI_API_KEY set via setx.
cd /d "%~dp0"
set PYTHONUTF8=1
echo Starting Stock Flag Bot server on http://127.0.0.1:8765 ...
echo Keep this window open while trading. Close it to stop the bot.
.venv\Scripts\python.exe run_tradingview_bot.py --no-browser
pause
