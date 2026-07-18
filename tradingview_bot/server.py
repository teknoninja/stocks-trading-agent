"""Local FastAPI server backing the TradingView sidebar bot.

Endpoints:
  GET  /health           -> liveness + LLM availability
  GET  /analyze?symbol=  -> full multi-strategy analysis + BUY/SELL/HOLD flag
  POST /ask              -> chat about the current symbol (Gemini API if
                            GEMINI_API_KEY is set, rule-based fallback otherwise)

Everything runs on the free tier: free Yahoo data + Google Gemini free tier.
Get a free API key at https://aistudio.google.com/apikey and export it:
  export GEMINI_API_KEY='your-key'
"""

import json
import os
import threading
import time
from typing import Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from stocks_agent.technicals import analyze_ticker  # also injects truststore for proxy TLS
from . import alpaca, journal, virtual_broker
from .scanner import get_auto_trading, set_auto_trading

# "-latest" aliases always point at the current models, so they keep working
# when Google retires specific versions. If the preferred model is overloaded
# (503) or out of free quota (429), the next one in the ladder is tried.
GEMINI_MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
]
GEMINI_MODEL = GEMINI_MODELS[0]  # reported in /health
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CACHE_TTL_SECONDS = 300


def _gemini_key() -> Optional[str]:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

app = FastAPI(title="TradingView Stock Flag Bot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # local-only server; sidebar is injected into tradingview.com
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def allow_private_network(request, call_next):
    """Chrome Private/Local Network Access: an https page (tradingview.com)
    calling a loopback server must see this header on the CORS preflight."""
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    response.headers["Access-Control-Allow-Local-Network"] = "true"
    return response

_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()
_chat_history: Dict[str, List[dict]] = {}


class AskRequest(BaseModel):
    symbol: str
    question: str


def _get_analysis(symbol: str) -> dict:
    key = symbol.upper().strip()
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit["ts"] < CACHE_TTL_SECONDS:
            return hit["analysis"]
    analysis = analyze_ticker(key)
    if "error" not in analysis:  # never cache failures (rate limits are transient)
        with _cache_lock:
            _cache[key] = {"ts": now, "analysis": analysis}
        try:
            journal.log_flag(analysis)
        except Exception:
            pass  # journaling must never break analysis
    return analysis


def _llm_available() -> bool:
    return _gemini_key() is not None


def _compact_evidence(analysis: dict) -> dict:
    """Trim the analysis to what the LLM needs (keeps prompt small for local models)."""
    return {
        "symbol": analysis.get("symbol"),
        "price": analysis.get("price"),
        "change_pct": analysis.get("change_pct"),
        "flag": analysis.get("flag"),
        "score": analysis.get("score"),
        "confidence": analysis.get("confidence"),
        "bullish_reasons": analysis.get("bullish_reasons"),
        "bearish_reasons": analysis.get("bearish_reasons"),
        "signals": [
            {"name": s["name"], "timeframe": s["timeframe"],
             "direction": s["direction_label"], "detail": s["detail"]}
            for s in analysis.get("signals", [])
        ],
    }


SYSTEM_PROMPT = """You are a professional technical analyst embedded as a sidebar bot on TradingView.
You are given a pre-computed multi-strategy technical analysis (market structure/BOS/CHOCH,
supply-demand zones, order blocks, liquidity sweeps, divergences, volume profile, VWAP,
harmonics, Elliott, Wyckoff, mean reversion, trend following, breakouts, options positioning)
for the stock the user is currently viewing.

Rules:
- Ground every claim in the provided analysis JSON; cite concrete signals and levels.
- Lead with the flag (BUY/SELL/HOLD) and confidence when the user asks for a view.
- Be concise (under ~200 words), plain language, no markdown headers.
- Mention both sides (bull and bear evidence) briefly.
- Always end with: "Not financial advice."
"""


def _rule_based_answer(analysis: dict, question: str) -> str:
    """Fallback answer when no local LLM is running."""
    if "error" in analysis:
        return f"Sorry, I couldn't analyze this symbol: {analysis['error']}"
    lines = [
        f"{analysis['symbol']} — flag: {analysis['flag']} "
        f"(score {analysis['score']:+.2f}, confidence {int(analysis['confidence'] * 100)}%). "
        f"Price {analysis['price']} ({analysis['change_pct']:+.2f}% today).",
    ]
    if analysis.get("bullish_reasons"):
        lines.append("Bullish: " + "; ".join(analysis["bullish_reasons"][:3]) + ".")
    if analysis.get("bearish_reasons"):
        lines.append("Bearish: " + "; ".join(analysis["bearish_reasons"][:3]) + ".")
    if not _llm_available():
        lines.append("(Set GEMINI_API_KEY for conversational answers — free key at aistudio.google.com/apikey. Not financial advice.)")
    else:
        lines.append("(Gemini request failed — showing rule-based summary. Not financial advice.)")
    return "\n".join(lines)


def _llm_answer(analysis: dict, symbol: str, question: str) -> Optional[str]:
    """Ask Gemini (free tier) via its REST API. Returns None on any failure so
    the caller falls back to the rule-based answer."""
    key = _gemini_key()
    if not key:
        return None
    try:
        import requests

        history = _chat_history.setdefault(symbol.upper(), [])
        contents = [
            *history[-8:],
            {"role": "user", "parts": [{"text": question}]},
        ]
        payload = {
            "systemInstruction": {
                "parts": [
                    {"text": SYSTEM_PROMPT},
                    {"text": "Current analysis JSON:\n"
                             + json.dumps(_compact_evidence(analysis), default=str)},
                ]
            },
            "contents": contents,
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024},
        }
        answer = ""
        for model in GEMINI_MODELS:
            r = requests.post(
                GEMINI_URL.format(model=model),
                params={"key": key},
                json=payload,
                timeout=45,
            )
            if r.status_code in (429, 503):  # quota/overload: try next model
                continue
            r.raise_for_status()
            candidates = r.json().get("candidates") or []
            parts = (candidates[0].get("content", {}).get("parts") or []) if candidates else []
            answer = "".join(p.get("text", "") for p in parts).strip()
            if answer:
                break
        if not answer:
            return None
        history.append({"role": "user", "parts": [{"text": question}]})
        history.append({"role": "model", "parts": [{"text": answer}]})
        del history[:-16]
        return answer
    except Exception:
        return None


@app.get("/health")
def health():
    return {"status": "ok", "llm": _llm_available(), "model": GEMINI_MODEL}


class TradeRequest(BaseModel):
    symbol: str


class AutoTradeRequest(BaseModel):
    on: bool


def _alpaca_symbol(analysis: dict):
    """Alpaca trades US-listed stocks only; reject suffixed/crypto symbols."""
    sym = (analysis.get("yf_symbol") or "").upper()
    if not sym or "." in sym or "-" in sym or "=" in sym:
        return None
    return sym


@app.post("/papertrade")
def papertrade(req: TradeRequest):
    """Manual paper trade of the CURRENT flag: BUY -> buy $notional, SELL -> close."""
    if not alpaca.configured():
        return {"error": "Alpaca not configured — set ALPACA_API_KEY / ALPACA_SECRET_KEY in .envrc"}
    analysis = _get_analysis(req.symbol)
    if "error" in analysis:
        return {"error": analysis["error"]}
    sym = _alpaca_symbol(analysis)
    if not sym:
        return {"error": f"{analysis.get('yf_symbol')} isn't tradable on Alpaca (US stocks only)"}
    flag = analysis["flag"]
    try:
        notional = float(os.getenv("TV_BOT_NOTIONAL", "1000"))
        if flag == "BUY":
            if not alpaca.tradable(sym):
                return {"error": f"{sym} not tradable on Alpaca"}
            order = alpaca.buy_notional(sym, notional)
            return {"ok": True, "action": f"BUY ${notional:.0f} of {sym}",
                    "order_id": order.get("id"), "flag": flag}
        if flag == "SELL":
            pos = alpaca.position(sym)
            if not pos:
                return {"error": f"Flag is SELL but you hold no {sym} — nothing to close"}
            alpaca.close_position(sym)
            return {"ok": True, "action": f"CLOSED {pos['qty']} {sym}", "flag": flag}
        return {"error": "Flag is HOLD — no trade to take"}
    except Exception as e:
        return {"error": f"Alpaca error: {e}"}


@app.get("/autotrade")
def autotrade_state():
    """Current state of the after-hours AUTO_TRADING switch (GitHub repo variable)."""
    state = get_auto_trading()
    return {"configured": state is not None, "enabled": state}


@app.post("/autotrade")
def autotrade_set(req: AutoTradeRequest):
    if not set_auto_trading(req.on):
        return {"error": "Could not update — set GITHUB_TOKEN and GITHUB_REPO in .envrc, "
                         "or flip the AUTO_TRADING variable on github.com"}
    return {"ok": True, "enabled": req.on}


class ResetRequest(BaseModel):
    starting_cash: float


def _latest_price(symbol: str):
    from stocks_agent.technicals.data import _history
    try:
        df = _history(symbol, "5d", "1d")
        return float(df["Close"].iloc[-1]) if df is not None and not df.empty else None
    except Exception:
        return None


def _load_portfolio() -> dict:
    """Prefer the repo copy (GitHub Actions may have traded since we last pulled)."""
    remote = virtual_broker.gh_fetch()
    if remote:
        virtual_broker.save(remote)  # keep local file in sync for the scanner
        return remote
    return virtual_broker.load()


@app.get("/portfolio")
def portfolio():
    p = _load_portfolio()
    return virtual_broker.valuation(p, _latest_price)


@app.post("/portfolio/reset")
def portfolio_reset(req: ResetRequest):
    if req.starting_cash <= 0:
        return {"error": "Starting amount must be positive"}
    p = virtual_broker.reset(req.starting_cash)
    synced = virtual_broker.gh_push(p, f"Reset NSE virtual portfolio to {req.starting_cash:,.0f}")
    return {"ok": True, "starting_cash": req.starting_cash,
            "synced_to_github": synced,
            "note": None if synced else
            "Saved locally only — commit & push nse_portfolio.json (or set GITHUB_TOKEN "
            "with Contents read/write) so the GitHub scanner sees the reset."}


def _portfolio_html() -> str:
    try:
        v = virtual_broker.valuation(_load_portfolio(), _latest_price)
    except Exception as e:
        return f"<p class='note'>Portfolio unavailable: {e}</p>"
    pos_rows = "".join(
        f"<tr><td>{r['symbol']}</td><td>{r['qty']}</td><td>{r['avg_price']}</td>"
        f"<td>{r['price']}</td><td>{r['value']:,.0f}</td>"
        f"<td style='color:{'#4ade80' if r['pnl'] >= 0 else '#f87171'}'>{r['pnl']:,.0f} ({r['pnl_pct']}%)</td></tr>"
        for r in v["positions"]
    ) or "<tr><td colspan=6>No open positions.</td></tr>"
    ret_color = "#4ade80" if v["total_return_pct"] >= 0 else "#f87171"
    return f"""
<h2>🇮🇳 NSE virtual portfolio (paper)</h2>
<p class="note">Equity <b>₹{v['equity']:,.0f}</b> · cash ₹{v['cash']:,.0f} · invested ₹{v['market_value']:,.0f}
 · return <b style="color:{ret_color}">{v['total_return_pct']}%</b> on ₹{v['starting_cash']:,.0f} start
 · {v['n_trades']} trades</p>
<table><tr><th>Symbol</th><th>Qty</th><th>Avg buy</th><th>Price</th><th>Value</th><th>P&amp;L</th></tr>{pos_rows}</table>
<div style="margin:-14px 0 28px;display:flex;gap:8px;align-items:center">
  <input id="reset-amt" type="number" value="{int(v['starting_cash'])}" min="1000" step="1000"
    style="background:#1b1f2a;border:1px solid #2d3342;border-radius:8px;color:#e5e7eb;padding:7px 10px;width:160px"/>
  <button onclick="resetPortfolio()" style="background:#dc2626;border:none;color:#fff;border-radius:8px;padding:8px 14px;cursor:pointer;font-weight:600">Reset portfolio</button>
  <span class="note">wipes positions &amp; trades, restarts with the amount on the left</span>
</div>
<script>
async function resetPortfolio() {{
  const amt = parseFloat(document.getElementById('reset-amt').value);
  if (!confirm(`Reset the NSE virtual portfolio to ₹${{amt.toLocaleString()}}? All positions and trade history will be wiped.`)) return;
  const r = await fetch('/portfolio/reset', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{starting_cash: amt}})}});
  const d = await r.json();
  alert(d.ok ? ('Portfolio reset.' + (d.note ? '\\n\\n' + d.note : '')) : d.error);
  location.reload();
}}
</script>"""


@app.get("/performance")
def performance():
    """Scoreboard + NSE virtual portfolio. Refreshes outcomes first."""
    from fastapi.responses import HTMLResponse
    try:
        journal.update_outcomes()
    except Exception:
        pass  # show whatever is already scored even if Yahoo is flaky
    html = journal.render_html().replace("</body>", _portfolio_html() + "</body>")
    return HTMLResponse(html)


@app.get("/performance.json")
def performance_json():
    try:
        journal.update_outcomes()
    except Exception:
        pass
    return journal.performance_summary()


@app.get("/analyze")
def analyze(symbol: str):
    return _get_analysis(symbol)


@app.post("/ask")
def ask(req: AskRequest):
    analysis = _get_analysis(req.symbol)
    answer = _llm_answer(analysis, req.symbol, req.question)
    used_llm = answer is not None
    if answer is None:
        answer = _rule_based_answer(analysis, req.question)
    return {
        "symbol": analysis.get("symbol", req.symbol.upper()),
        "flag": analysis.get("flag"),
        "confidence": analysis.get("confidence"),
        "answer": answer,
        "used_llm": used_llm,
    }


def run(host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
