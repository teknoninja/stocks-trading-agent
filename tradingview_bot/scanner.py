"""Watchlist scanner: reconcile flags with Alpaca paper positions.

Stateless by design — every run derives its decisions from (a) fresh flags for
the watchlist and (b) current positions on Alpaca. Safe to run from a laptop,
a cron job, or GitHub Actions; Alpaca itself is the persistent state.

Rules per symbol (long-only):
  BUY flag  + no position + confidence >= TV_BOT_MIN_CONF + room left -> buy $TV_BOT_NOTIONAL
  SELL flag + position held                                           -> close position
  anything else                                                       -> no action

Guardrails:
  - skips entirely when the market is closed
  - max TV_BOT_MAX_POS open positions
  - daily drawdown breaker: if account equity is down more than
    TV_BOT_MAX_DD (default 3%) vs yesterday's close, turn the
    AUTO_TRADING repo variable off and stop.
"""

import os
import time
from typing import Dict, List, Optional

import requests

from . import alpaca

NOTIONAL = float(os.getenv("TV_BOT_NOTIONAL", "1000"))
MIN_CONF = float(os.getenv("TV_BOT_MIN_CONF", "0.55"))
MAX_POS = int(os.getenv("TV_BOT_MAX_POS", "10"))
MAX_DD = float(os.getenv("TV_BOT_MAX_DD", "0.03"))
WATCHLIST_FILE = os.getenv("TV_BOT_WATCHLIST", "watchlist.txt")


# ---------------- GitHub AUTO_TRADING variable (the ON/OFF switch) ----------

def _gh_repo() -> Optional[str]:
    return os.getenv("GITHUB_REPOSITORY") or os.getenv("GITHUB_REPO")


def _gh_headers() -> Optional[dict]:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


def get_auto_trading() -> Optional[bool]:
    """Read the AUTO_TRADING repo variable. None = not configured/unknown."""
    repo, headers = _gh_repo(), _gh_headers()
    if not repo or not headers:
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/variables/AUTO_TRADING",
                         headers=headers, timeout=15)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return r.json().get("value", "").strip().lower() == "on"
    except Exception:
        return None


def set_auto_trading(on: bool) -> bool:
    """Create/update the AUTO_TRADING repo variable. Returns success."""
    repo, headers = _gh_repo(), _gh_headers()
    if not repo or not headers:
        return False
    value = "on" if on else "off"
    try:
        r = requests.patch(
            f"https://api.github.com/repos/{repo}/actions/variables/AUTO_TRADING",
            headers=headers, json={"name": "AUTO_TRADING", "value": value}, timeout=15)
        if r.status_code == 404:  # variable doesn't exist yet
            r = requests.post(f"https://api.github.com/repos/{repo}/actions/variables",
                              headers=headers,
                              json={"name": "AUTO_TRADING", "value": value}, timeout=15)
        return r.status_code in (201, 204)
    except Exception:
        return False


# ---------------- analysis with data fallback --------------------------------

def analyze_with_fallback(symbol: str) -> dict:
    """Engine on Yahoo data; if Yahoo is rate-limiting (common on shared CI
    IPs), rebuild daily/weekly frames from Alpaca's free IEX bars instead."""
    from stocks_agent.technicals import analyze_ticker

    result = analyze_ticker(symbol, include_options=False)
    if "error" not in result:
        return result
    if not alpaca.configured():
        return result
    try:
        daily = alpaca.daily_bars(symbol)
        if daily.empty or len(daily) < 260:
            return result
        from stocks_agent.technicals.backtest import _weekly_from_daily
        from stocks_agent.technicals.engine import analyze_frames
        fallback = analyze_frames({"1D": daily, "1W": _weekly_from_daily(daily)})
        price = float(daily["Close"].iloc[-1])
        prev = float(daily["Close"].iloc[-2])
        fallback.update({"symbol": symbol.upper(), "yf_symbol": symbol.upper(),
                         "price": round(price, 4),
                         "change_pct": round((price / prev - 1) * 100, 2),
                         "data_source": "alpaca_iex"})
        return fallback
    except Exception:
        return result


# ---------------- the scan ----------------------------------------------------

def load_watchlist(path: str = WATCHLIST_FILE) -> List[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            sym = line.split("#")[0].strip().upper()
            if sym:
                out.append(sym)
    return out


def run_scan(dry_run: bool = False) -> dict:
    """One reconciliation pass. Returns a summary dict (also printed by CLI)."""
    log: List[str] = []

    def say(msg: str):
        print(msg, flush=True)
        log.append(msg)

    if not alpaca.configured():
        say("ABORT: ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return {"status": "not_configured", "log": log}

    clk = alpaca.clock()
    if not clk.get("is_open"):
        say(f"Market closed (next open {clk.get('next_open')}) — nothing to do.")
        return {"status": "market_closed", "log": log}

    acct = alpaca.account()
    equity = float(acct["equity"])
    last_equity = float(acct["last_equity"] or equity)
    say(f"Account equity ${equity:,.2f} (prev close ${last_equity:,.2f})")

    # drawdown circuit breaker
    if last_equity > 0 and equity < last_equity * (1 - MAX_DD):
        dd = (1 - equity / last_equity) * 100
        say(f"CIRCUIT BREAKER: equity down {dd:.1f}% today (limit {MAX_DD * 100:.0f}%). "
            "Turning AUTO_TRADING off and stopping.")
        if set_auto_trading(False):
            say("AUTO_TRADING repo variable set to 'off'.")
        else:
            say("WARNING: could not flip AUTO_TRADING — no GitHub token/repo configured.")
        return {"status": "circuit_breaker", "log": log}

    watchlist = load_watchlist()
    if not watchlist:
        say(f"ABORT: watchlist empty/missing ({WATCHLIST_FILE})")
        return {"status": "no_watchlist", "log": log}

    held: Dict[str, dict] = {p["symbol"].upper(): p for p in alpaca.positions()}
    open_count = len(held)
    say(f"Watchlist: {', '.join(watchlist)} | open positions: {open_count}")

    actions = []
    for sym in watchlist:
        result = analyze_with_fallback(sym)
        if "error" in result:
            say(f"  {sym}: SKIP ({result['error'][:80]})")
            continue
        flag, conf = result["flag"], result.get("confidence", 0)
        src = result.get("data_source", "yahoo")
        pos = held.get(sym)
        say(f"  {sym}: {flag} conf={conf:.2f} score={result.get('score')} [{src}]"
            + (f" | holding {pos['qty']}" if pos else ""))

        if flag == "BUY" and not pos:
            if conf < MIN_CONF:
                say(f"    -> skip buy (confidence {conf:.2f} < {MIN_CONF})")
            elif open_count >= MAX_POS:
                say(f"    -> skip buy (max positions {MAX_POS} reached)")
            elif dry_run:
                say(f"    -> would BUY ${NOTIONAL:.0f}")
            else:
                order = alpaca.buy_notional(sym, NOTIONAL)
                open_count += 1
                actions.append({"symbol": sym, "action": "buy", "order_id": order.get("id")})
                say(f"    -> BUY ${NOTIONAL:.0f} submitted ({order.get('id', '?')[:8]})")
        elif flag == "SELL" and pos:
            if dry_run:
                say(f"    -> would CLOSE {pos['qty']}")
            else:
                alpaca.close_position(sym)
                open_count -= 1
                actions.append({"symbol": sym, "action": "close"})
                say(f"    -> position CLOSED")
        time.sleep(1.5)  # be gentle with the data APIs

    say(f"Done. {len(actions)} action(s).")
    return {"status": "ok", "actions": actions, "log": log}


# ---------------- NSE scan via the built-in virtual broker -------------------

WATCHLIST_IN_FILE = os.getenv("TV_BOT_WATCHLIST_IN", "watchlist_in.txt")


def run_virtual_scan(dry_run: bool = False) -> dict:
    """Reconcile the NSE watchlist with the built-in virtual portfolio.

    Same rules as the Alpaca scan (long-only, confidence floor, max positions);
    fills simulated at the latest Yahoo price, whole shares only. The portfolio
    JSON lives in the repo — GitHub Actions commits it after each run.
    """
    from . import virtual_broker as vb

    log: List[str] = []

    def say(msg: str):
        print(msg, flush=True)
        log.append(msg)

    if not vb.nse_market_open():
        say("NSE market closed — nothing to do.")
        return {"status": "market_closed", "log": log}

    watchlist = load_watchlist(WATCHLIST_IN_FILE)
    if not watchlist:
        say(f"ABORT: NSE watchlist empty/missing ({WATCHLIST_IN_FILE})")
        return {"status": "no_watchlist", "log": log}

    p = vb.load()
    say(f"Virtual portfolio: cash ₹{p['cash']:,.0f}, positions: {len(p['positions'])}")

    # drawdown breaker vs starting cash is too blunt; use equity vs starting cash floor
    actions = []
    open_count = len(p["positions"])
    for sym in watchlist:
        from stocks_agent.technicals import analyze_ticker
        result = analyze_ticker(sym, include_options=False)
        if "error" in result:
            say(f"  {sym}: SKIP ({result['error'][:80]})")
            continue
        flag, conf, price = result["flag"], result.get("confidence", 0), result["price"]
        pos = p["positions"].get(sym)
        say(f"  {sym}: {flag} conf={conf:.2f} score={result.get('score')} @₹{price}"
            + (f" | holding {pos['qty']}" if pos else ""))

        if flag == "BUY" and not pos:
            if conf < MIN_CONF:
                say(f"    -> skip buy (confidence {conf:.2f} < {MIN_CONF})")
            elif open_count >= MAX_POS:
                say(f"    -> skip buy (max positions {MAX_POS} reached)")
            elif dry_run:
                say(f"    -> would BUY ~₹{vb.NOTIONAL:,.0f}")
            else:
                trade = vb.buy(p, sym, price)
                if trade:
                    open_count += 1
                    actions.append(trade)
                    say(f"    -> BOUGHT {trade['qty']} @ ₹{price}")
                else:
                    say("    -> skip buy (insufficient cash for one share)")
        elif flag == "SELL" and pos:
            if dry_run:
                say(f"    -> would SELL {pos['qty']}")
            else:
                trade = vb.sell_all(p, sym, price)
                if trade:
                    open_count -= 1
                    actions.append(trade)
                    say(f"    -> SOLD {trade['qty']} @ ₹{price} (pnl ₹{trade['pnl']:,.0f})")
        time.sleep(1.5)

    if actions and not dry_run:
        vb.save(p)
        say(f"Portfolio saved to {vb.PORTFOLIO_FILE}.")
    say(f"Done. {len(actions)} action(s).")
    return {"status": "ok", "actions": actions, "log": log}
