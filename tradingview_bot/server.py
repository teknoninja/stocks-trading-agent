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
from . import alpaca, journal, virtual_broker, watchlists
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
    side: Optional[str] = None  # "buy" | "sell" | None = follow the current flag


class AutoTradeRequest(BaseModel):
    on: bool


def _alpaca_symbol(analysis: dict):
    """Alpaca trades US-listed stocks only; reject suffixed/crypto symbols."""
    sym = (analysis.get("yf_symbol") or "").upper()
    if not sym or "." in sym or "-" in sym or "=" in sym:
        return None
    return sym


def _effective_side(analysis: dict, side: Optional[str]) -> Optional[str]:
    """Explicit side wins; otherwise follow the flag (HOLD -> None)."""
    if side in ("buy", "sell"):
        return side
    return {"BUY": "buy", "SELL": "sell"}.get(analysis["flag"])


def _indian_papertrade(analysis: dict, side: Optional[str]) -> dict:
    """Manual NSE paper trade against the built-in virtual portfolio."""
    sym = analysis["yf_symbol"].upper()
    price = analysis["price"]
    act = _effective_side(analysis, side)
    if act is None:
        return {"error": "Flag is HOLD — use the Buy/Sell buttons to trade anyway"}
    p = _load_portfolio()  # freshest copy (GitHub may have traded meanwhile)
    if act == "buy":
        trade = virtual_broker.buy(p, sym, price)
        if not trade:
            return {"error": f"Not enough virtual cash (₹{p['cash']:,.0f}) for one share of {sym} @ ₹{price}"}
        action = f"BOUGHT {trade['qty']} {sym} @ ₹{price} (virtual NSE portfolio)"
    else:
        trade = virtual_broker.sell_all(p, sym, price)
        if not trade:
            return {"error": f"The virtual portfolio holds no {sym} — nothing to sell"}
        action = f"SOLD {trade['qty']} {sym} @ ₹{price}, P&L ₹{trade['pnl']:,.0f} (virtual NSE portfolio)"
    virtual_broker.save(p)
    synced = virtual_broker.gh_push(p, f"Manual paper trade: {action}")
    if not synced:
        action += " — saved locally; push nse_portfolio.json so GitHub sees it"
    return {"ok": True, "action": action, "flag": analysis["flag"]}


@app.post("/papertrade")
def papertrade(req: TradeRequest):
    """Manual paper trade of the CURRENT flag.

    US symbols -> Alpaca paper account. Indian symbols (.NS/.BO) -> the
    built-in NSE virtual portfolio shown on /performance.
    """
    analysis = _get_analysis(req.symbol)
    if "error" in analysis:
        return {"error": analysis["error"]}
    yf_sym = (analysis.get("yf_symbol") or "").upper()
    if yf_sym.endswith(".NS") or yf_sym.endswith(".BO"):
        try:
            return _indian_papertrade(analysis, req.side)
        except Exception as e:
            return {"error": f"Virtual broker error: {e}"}
    if not alpaca.configured():
        return {"error": "Alpaca not configured — set ALPACA_API_KEY / ALPACA_SECRET_KEY in .envrc"}
    sym = _alpaca_symbol(analysis)
    if not sym:
        return {"error": f"{analysis.get('yf_symbol')} isn't tradable here (US stocks via Alpaca, "
                         "Indian stocks via the virtual portfolio — other markets unsupported)"}
    flag = analysis["flag"]
    act = _effective_side(analysis, req.side)
    if act is None:
        return {"error": "Flag is HOLD — use the Buy/Sell buttons to trade anyway"}
    try:
        notional = float(os.getenv("TV_BOT_NOTIONAL", "1000"))
        if act == "buy":
            if not alpaca.tradable(sym):
                return {"error": f"{sym} not tradable on Alpaca"}
            order = alpaca.buy_notional(sym, notional)
            return {"ok": True, "action": f"BUY ${notional:.0f} of {sym} (Alpaca paper)",
                    "order_id": order.get("id"), "flag": flag}
        pos = alpaca.position(sym)
        if not pos:
            return {"error": f"You hold no {sym} — nothing to sell"}
        alpaca.close_position(sym)
        return {"ok": True, "action": f"CLOSED {pos['qty']} {sym} (Alpaca paper)", "flag": flag}
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
    v = virtual_broker.valuation(p, _latest_price)
    if virtual_broker.record_snapshot(p, v["equity"]):
        virtual_broker.save(p)  # local snapshot for the chart; cloud runs add their own
    return v


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


def _nifty_series(since_iso: Optional[str]) -> list:
    """NIFTY 50 daily closes since the first snapshot, for the benchmark line."""
    from stocks_agent.technicals.data import _history
    try:
        df = _history("^NSEI", "6mo", "1d")
        if df is None or df.empty:
            return []
        out = []
        for ts, close in df["Close"].items():
            iso = ts.isoformat()
            if since_iso and iso[:10] < since_iso[:10]:
                continue
            out.append({"t": iso, "v": round(float(close), 2)})
        return out
    except Exception:
        return []


def _portfolio_html() -> str:
    try:
        p = _load_portfolio()
        v = virtual_broker.valuation(p, _latest_price)
        if virtual_broker.record_snapshot(p, v["equity"]):
            virtual_broker.save(p)
        history = [{"t": h["ts"], "e": h["equity"]} for h in p.get("equity_history", [])]
    except Exception as e:
        return f"<p class='note'>Portfolio unavailable: {e}</p>"

    bench = _nifty_series(history[0]["t"] if history else None)
    pnl = v["equity"] - v["starting_cash"]
    up = pnl >= 0
    pnl_color = "#4ade80" if up else "#f87171"
    arrow = "▲" if up else "▼"

    def inr(x):
        return f"₹{x:,.2f}"

    def _pos_row(r):
        earned = inr(r["pnl"]) if r["pnl"] > 0 else "—"
        lost = inr(-r["pnl"]) if r["pnl"] < 0 else "—"
        return f"""<tr>
          <td><b>{r['symbol'].replace('.NS', '').replace('.BO', '')}</b>
              <span style='color:#6b7280;font-size:10px'>{'NSE' if r['symbol'].endswith('.NS') else 'BSE'}</span></td>
          <td style='text-align:right;color:#9ca3af'>{r['qty']}</td>
          <td style='text-align:right'>₹{r['avg_price']:,}</td>
          <td style='text-align:right'>₹{r['price']:,}<br>
              <span style='color:{"#4ade80" if r['pnl_pct'] >= 0 else "#f87171"};font-size:10px'>{r['pnl_pct']:+}%</span></td>
          <td style='text-align:right'>{inr(r['invested'])}</td>
          <td style='text-align:right'>{inr(r['value'])}</td>
          <td style='text-align:right;color:#4ade80'>{earned}</td>
          <td style='text-align:right;color:#f87171'>{lost}</td></tr>"""

    pos_rows = "".join(_pos_row(r) for r in v["positions"]) \
        or "<tr><td colspan=8 class='note'>No open positions yet — paper trade an NSE stock or let the scanner run.</td></tr>"
    if v["positions"]:
        pos_rows += f"""<tr style='border-top:2px solid #3a3f4d;font-weight:700'>
          <td>TOTAL</td><td></td><td></td><td></td>
          <td style='text-align:right'>{inr(v['invested_total'])}</td>
          <td style='text-align:right'>{inr(v['market_value'])}</td>
          <td style='text-align:right;color:#4ade80'>{inr(v['unrealized_earned']) if v['unrealized_earned'] else '—'}</td>
          <td style='text-align:right;color:#f87171'>{inr(v['unrealized_lost']) if v['unrealized_lost'] else '—'}</td></tr>"""

    import json as _json
    return f"""
<h2>🇮🇳 NSE virtual portfolio (paper)</h2>

<div id="pf-chart-wrap" style="background:#141722;border:1px solid #262a35;border-radius:12px;padding:14px 14px 6px;margin-bottom:18px">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:4px">
    <span class="note" id="pf-chart-title">Performance</span>
    <span style="flex:1"></span>
    <span style="font-size:11px;color:#c3c2b7"><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#3987e5;margin-right:4px"></span>Portfolio</span>
    <span style="font-size:11px;color:#c3c2b7"><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#c98500;margin-right:4px"></span>NIFTY 50</span>
  </div>
  <div id="pf-chart" style="position:relative"></div>
  <div style="display:flex;gap:6px;margin:8px 0 6px" id="pf-ranges"></div>
</div>

<table style="margin-top:0"><tr>
  <th>Asset</th><th style="text-align:right">Qty</th><th style="text-align:right">Buy price</th>
  <th style="text-align:right">Current</th><th style="text-align:right">Invested</th>
  <th style="text-align:right">Value</th><th style="text-align:right">Cash earned</th>
  <th style="text-align:right">Cash lost</th></tr>{pos_rows}</table>

<table style="margin-top:-16px">
  <tr><td style="color:#9ca3af">ALL HOLDINGS</td><td style="text-align:right"><b>{inr(v['market_value'])}</b></td></tr>
  <tr><td style="color:#9ca3af">CASH EARNED (open positions)</td><td style="text-align:right;color:#4ade80"><b>{"▲ " + inr(v['unrealized_earned']) if v['unrealized_earned'] else "—"}</b></td></tr>
  <tr><td style="color:#9ca3af">CASH LOST (open positions)</td><td style="text-align:right;color:#f87171"><b>{"▼ " + inr(v['unrealized_lost']) if v['unrealized_lost'] else "—"}</b></td></tr>
  <tr><td style="color:#9ca3af">REALISED P&amp;L (from sold stocks)</td><td style="text-align:right;color:{'#4ade80' if v['realized_pnl'] >= 0 else '#f87171'}"><b>{'▲' if v['realized_pnl'] >= 0 else '▼'} {inr(abs(v['realized_pnl']))}</b></td></tr>
  <tr><td style="color:#9ca3af">TOTAL P&amp;L</td><td style="text-align:right;color:{pnl_color}"><b>{arrow} {inr(abs(pnl))} ({v['total_return_pct']:+}%)</b></td></tr>
  <tr><td style="color:#9ca3af">CASH</td><td style="text-align:right"><b>{inr(v['cash'])}</b></td></tr>
  <tr><td style="color:#9ca3af">TOTAL</td><td style="text-align:right;font-size:15px"><b>{inr(v['equity'])}</b></td></tr>
</table>

<div style="margin:-10px 0 28px;display:flex;gap:8px;align-items:center">
  <input id="reset-amt" type="number" value="{int(v['starting_cash'])}" min="1000" step="1000"
    style="background:#1b1f2a;border:1px solid #2d3342;border-radius:8px;color:#e5e7eb;padding:7px 10px;width:160px"/>
  <button onclick="resetPortfolio()" style="background:#dc2626;border:none;color:#fff;border-radius:8px;padding:8px 14px;cursor:pointer;font-weight:600">Reset portfolio</button>
  <span class="note">wipes positions &amp; trades, restarts with the amount on the left</span>
</div>

<script>
const PF_HISTORY = {_json.dumps(history)};
const PF_BENCH = {_json.dumps(bench)};
const PF_START = {v['starting_cash']};

async function resetPortfolio() {{
  const amt = parseFloat(document.getElementById('reset-amt').value);
  if (!confirm(`Reset the NSE virtual portfolio to ₹${{amt.toLocaleString('en-IN')}}? All positions and trade history will be wiped.`)) return;
  const r = await fetch('/portfolio/reset', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{starting_cash: amt}})}});
  const d = await r.json();
  alert(d.ok ? ('Portfolio reset.' + (d.note ? '\\n\\n' + d.note : '')) : d.error);
  location.reload();
}}

// ---- performance chart: % change, portfolio vs NIFTY, one axis ----
(function () {{
  const wrap = document.getElementById('pf-chart');
  const W = Math.min(860, wrap.clientWidth || 860), H = 190, PAD = {{l: 44, r: 56, t: 10, b: 22}};
  const RANGES = {{ '1W': 7, '1M': 30, '3M': 92, 'ALL': 100000 }};
  let range = 'ALL';

  const rangesEl = document.getElementById('pf-ranges');
  Object.keys(RANGES).forEach((k) => {{
    const b = document.createElement('button');
    b.textContent = k;
    b.style.cssText = 'background:none;border:1px solid #262a35;color:#9ca3af;border-radius:14px;padding:3px 12px;cursor:pointer;font-size:11px';
    b.onclick = () => {{ range = k; draw(); paint(); }};
    rangesEl.appendChild(b);
  }});
  function paint() {{
    [...rangesEl.children].forEach((b) => {{
      const on = b.textContent === range;
      b.style.background = on ? '#1e2433' : 'none';
      b.style.color = on ? '#e5e7eb' : '#9ca3af';
    }});
  }}

  function draw() {{
    const cutoff = Date.now() - RANGES[range] * 864e5;
    let hist = PF_HISTORY.filter((h) => new Date(h.t).getTime() >= cutoff);
    if (hist.length < 2) hist = PF_HISTORY.slice();
    if (hist.length < 2) {{
      wrap.innerHTML = '<p style="color:#6b7280;font-size:12px;padding:30px 0;text-align:center">The chart appears once a few equity snapshots accumulate (every scan and page view adds one).</p>';
      return;
    }}
    const t0 = new Date(hist[0].t).getTime(), t1 = new Date(hist[hist.length - 1].t).getTime() || t0 + 1;
    const base = hist[0].e;
    const pf = hist.map((h) => ({{ t: new Date(h.t).getTime(), pct: (h.e / base - 1) * 100, raw: h.e }}));
    const b0 = PF_BENCH.filter((b) => new Date(b.t).getTime() >= t0 - 864e5);
    const bBase = b0.length ? b0[0].v : null;
    const bench = bBase ? b0.map((b) => ({{ t: new Date(b.t).getTime(), pct: (b.v / bBase - 1) * 100 }})) : [];

    const all = pf.map((p) => p.pct).concat(bench.map((b) => b.pct)).concat([0]);
    let lo = Math.min(...all), hi = Math.max(...all);
    const padY = Math.max(0.15, (hi - lo) * 0.15); lo -= padY; hi += padY;
    const X = (t) => PAD.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - PAD.l - PAD.r);
    const Y = (p) => PAD.t + (1 - (p - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
    const line = (pts) => pts.map((p, i) => (i ? 'L' : 'M') + X(p.t).toFixed(1) + ' ' + Y(p.pct).toFixed(1)).join(' ');
    const fmtD = (t) => new Date(t).toLocaleDateString('en-IN', {{ day: 'numeric', month: 'short' }});

    const gridVals = [lo + (hi - lo) * 0.25, lo + (hi - lo) * 0.75];
    let svg = `<svg viewBox="0 0 ${{W}} ${{H}}" width="100%" height="${{H}}" style="display:block">`;
    gridVals.forEach((g) => {{
      svg += `<line x1="${{PAD.l}}" x2="${{W - PAD.r}}" y1="${{Y(g)}}" y2="${{Y(g)}}" stroke="#20242f" stroke-width="1"/>`;
      if (Math.abs(Y(g) - Y(0)) > 14)  // keep the label from colliding with the 0% label
        svg += `<text x="4" y="${{Y(g) + 3}}" fill="#6b7280" font-size="10">${{g.toFixed(1)}}%</text>`;
    }});
    svg += `<line x1="${{PAD.l}}" x2="${{W - PAD.r}}" y1="${{Y(0)}}" y2="${{Y(0)}}" stroke="#3a3f4d" stroke-width="1" stroke-dasharray="3 4"/>`;
    svg += `<text x="4" y="${{Y(0) + 3}}" fill="#9ca3af" font-size="10">0%</text>`;
    if (bench.length > 1) svg += `<path d="${{line(bench)}}" fill="none" stroke="#c98500" stroke-width="2" stroke-linejoin="round"/>`;
    svg += `<path d="${{line(pf)}}" fill="none" stroke="#3987e5" stroke-width="2" stroke-linejoin="round"/>`;

    const endBadge = (pts, color, dy) => {{
      if (pts.length < 2) return '';
      const last = pts[pts.length - 1], txt = (last.pct >= 0 ? '+' : '') + last.pct.toFixed(2) + '%';
      return `<g><rect x="${{W - PAD.r + 4}}" y="${{Y(last.pct) - 9 + dy}}" rx="6" width="50" height="16" fill="${{color}}"/>
        <text x="${{W - PAD.r + 29}}" y="${{Y(last.pct) + 3 + dy}}" fill="#0b0b0b" font-size="10" font-weight="700" text-anchor="middle">${{txt}}</text></g>`;
    }};
    svg += endBadge(bench, '#c98500', -10);
    svg += endBadge(pf, '#3987e5', 10);
    svg += `<text x="${{PAD.l}}" y="${{H - 6}}" fill="#6b7280" font-size="10">${{fmtD(t0)}}</text>`;
    svg += `<text x="${{W - PAD.r}}" y="${{H - 6}}" fill="#6b7280" font-size="10" text-anchor="end">${{fmtD(t1)}}</text>`;
    svg += `<line id="pf-cross" x1="0" x2="0" y1="${{PAD.t}}" y2="${{H - PAD.b}}" stroke="#4b5563" stroke-width="1" visibility="hidden"/>`;
    svg += `<circle id="pf-dot" r="4" fill="#3987e5" stroke="#141722" stroke-width="2" visibility="hidden"/>`;
    svg += `</svg>`;
    wrap.innerHTML = svg + '<div id="pf-tip" style="position:absolute;pointer-events:none;background:#1e2433;border:1px solid #2d3342;border-radius:8px;padding:6px 9px;font-size:11px;color:#e5e7eb;visibility:hidden;white-space:nowrap"></div>';

    const svgEl = wrap.querySelector('svg'), tip = document.getElementById('pf-tip');
    const cross = document.getElementById('pf-cross'), dot = document.getElementById('pf-dot');
    svgEl.addEventListener('mousemove', (ev) => {{
      const box = svgEl.getBoundingClientRect();
      const mx = ((ev.clientX - box.left) / box.width) * W;
      let best = pf[0], bd = 1e18;
      pf.forEach((pt) => {{ const d = Math.abs(X(pt.t) - mx); if (d < bd) {{ bd = d; best = pt; }} }});
      cross.setAttribute('x1', X(best.t)); cross.setAttribute('x2', X(best.t));
      cross.setAttribute('visibility', 'visible');
      dot.setAttribute('cx', X(best.t)); dot.setAttribute('cy', Y(best.pct));
      dot.setAttribute('visibility', 'visible');
      tip.style.visibility = 'visible';
      tip.style.left = Math.min(X(best.t) / W * box.width + 12, box.width - 150) + 'px';
      tip.style.top = (Y(best.pct) / H * box.height - 40) + 'px';
      tip.innerHTML = `${{new Date(best.t).toLocaleString('en-IN', {{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}})}}<br>` +
        `<b>₹${{best.raw.toLocaleString('en-IN')}}</b> (${{best.pct >= 0 ? '+' : ''}}${{best.pct.toFixed(2)}}%)`;
    }});
    svgEl.addEventListener('mouseleave', () => {{
      cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden');
      tip.style.visibility = 'hidden';
    }});
  }}
  draw(); paint();
}})();
</script>"""


class WatchRequest(BaseModel):
    symbol: str


@app.get("/watchlist")
def watchlist_json():
    return watchlists.get_lists()


@app.get("/watchlist/status")
def watchlist_status(symbol: str):
    return watchlists.status(symbol)


@app.post("/watchlist/toggle")
def watchlist_toggle(req: WatchRequest):
    return watchlists.toggle(req.symbol)


_NAV = ("<div style='margin-bottom:18px;font-size:12px'>"
        "<a href='/performance' style='color:#60a5fa;margin-right:14px'>📊 Performance</a>"
        "<a href='/watchlists' style='color:#60a5fa'>⭐ Watchlists</a></div>")


@app.get("/watchlists")
def watchlists_page():
    from fastapi.responses import HTMLResponse
    lists = watchlists.get_lists()

    def section(title, market, symbols, note):
        rows = "".join(
            f"<tr><td><b>{s}</b></td>"
            f"<td style='text-align:right'><button onclick=\"toggleSym('{s}')\" "
            f"style='background:none;border:1px solid #3a2d2d;color:#f87171;border-radius:6px;"
            f"padding:2px 10px;cursor:pointer;font-size:11px'>✕ remove</button></td></tr>"
            for s in symbols
        ) or "<tr><td colspan=2 class='note'>Empty — add a symbol below or use ★ in the sidebar.</td></tr>"
        return f"""<h2>{title}</h2><p class="note">{note}</p>
<table style="max-width:420px"><tr><th>Symbol</th><th></th></tr>{rows}</table>"""

    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Watchlists</title>
<style>
 body{{background:#0f1117;color:#e5e7eb;font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:24px;max-width:900px;margin:auto}}
 table{{border-collapse:collapse;width:100%;margin:12px 0 28px;font-size:13px}}
 th,td{{padding:6px 10px;border-bottom:1px solid #262a35;text-align:left}}
 th{{color:#9ca3af;font-weight:600}} h1{{font-size:20px}} h2{{font-size:15px;color:#e5e7eb}}
 .note{{color:#6b7280;font-size:12px}}
</style></head><body>
<h1>⭐ Scanner watchlists</h1>{_NAV}
<p style="background:#1e2433;border:1px solid #2d3342;border-radius:8px;padding:10px 12px;font-size:12.5px;color:#e5e7eb">
⚠️ When <b>Auto trading is ON</b>, the scanner will auto buy/sell <b>only the stocks listed below</b>.
Remove a stock from its watchlist if you don't want the scanner to act on it —
removing it stops future scanner actions but does <b>not</b> sell anything you already hold.</p>
<p class="note">Changes sync to the GitHub repo, so the cloud scanner uses your updated lists from its next run.
Manual Buy/Sell buttons in the sidebar are unaffected by these lists.</p>
{section("🇺🇸 US — Alpaca paper account", "us", lists["us"], "Traded during US market hours (9:30 PM–4 AM SGT).")}
{section("🇮🇳 India — NSE virtual portfolio", "in", lists["in"], "Traded during NSE hours (11:45 AM–6 PM SGT). Yahoo .NS form.")}
<div style="display:flex;gap:8px;align-items:center;max-width:420px">
  <input id="new-sym" placeholder="e.g. NVDA or NSE:WIPRO or WIPRO.NS"
    style="flex:1;background:#1b1f2a;border:1px solid #2d3342;border-radius:8px;color:#e5e7eb;padding:8px 10px"/>
  <button onclick="addSym()" style="background:#2962ff;border:none;color:#fff;border-radius:8px;padding:8px 14px;cursor:pointer;font-weight:600">Add</button>
</div>
<p class="note" style="margin-top:8px">The market (US / India) is detected automatically from the symbol.</p>
<script>
async function toggleSym(s) {{
  const r = await fetch('/watchlist/toggle', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{symbol: s}})}});
  const d = await r.json();
  if (d.error) alert(d.error); else if (d.note) alert(d.action + '\\n\\n' + d.note);
  location.reload();
}}
function addSym() {{
  const s = document.getElementById('new-sym').value.trim();
  if (s) toggleSym(s);
}}
document.getElementById('new-sym').addEventListener('keydown', (e) => {{ if (e.key === 'Enter') addSym(); }});
</script>
</body></html>"""
    return HTMLResponse(html)


@app.get("/performance")
def performance():
    """Scoreboard + NSE virtual portfolio. Refreshes outcomes first."""
    from fastapi.responses import HTMLResponse
    try:
        journal.update_outcomes()
    except Exception:
        pass  # show whatever is already scored even if Yahoo is flaky
    html = journal.render_html().replace("</body>", _portfolio_html() + "</body>")
    html = html.replace("</h1>", "</h1>" + _NAV, 1)
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
