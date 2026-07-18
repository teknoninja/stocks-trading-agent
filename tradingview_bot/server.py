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
from . import journal

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


@app.get("/performance")
def performance():
    """Scoreboard: how past flags actually played out. Refreshes outcomes first."""
    from fastapi.responses import HTMLResponse
    try:
        journal.update_outcomes()
    except Exception:
        pass  # show whatever is already scored even if Yahoo is flaky
    return HTMLResponse(journal.render_html())


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
