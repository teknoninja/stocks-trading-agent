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
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from . import alpaca

NOTIONAL = float(os.getenv("TV_BOT_NOTIONAL", "1000"))
MIN_CONF = float(os.getenv("TV_BOT_MIN_CONF", "0.55"))
MAX_POS = int(os.getenv("TV_BOT_MAX_POS", "10"))
MAX_DD = float(os.getenv("TV_BOT_MAX_DD", "0.03"))
WATCHLIST_FILE = os.getenv("TV_BOT_WATCHLIST", "watchlist.txt")

# --- short-swing exit rules (the engine's edge concentrates in ~5 days) ---
# Defaults (used by the NSE virtual scan and as the fallback everywhere).
TAKE_PROFIT = float(os.getenv("TV_BOT_TAKE_PROFIT", "0.05"))   # bank at +5%; 0 disables
STOP_LOSS = float(os.getenv("TV_BOT_STOP_LOSS", "0.04"))       # cut at -4%; 0 disables
MAX_HOLD_DAYS = int(os.getenv("TV_BOT_MAX_HOLD_DAYS", "7"))    # trading days; 0 disables

# US (Alpaca) profile — wide target that lets winners run, PLUS a breakeven stop.
# Backtest finding: tight stops get chopped by daily noise on US mega-caps; a wide
# target with a breakeven stop earns more AND stops faded winners from round-tripping
# to a loss. Once a position's peak gain reaches US_BREAKEVEN, the stop moves to the
# entry price (exit at ~0% instead of -stop). Set US_BREAKEVEN=0 to disable.
US_TAKE_PROFIT = float(os.getenv("TV_BOT_TAKE_PROFIT_US", "0.05"))   # bank at +5%
US_STOP_LOSS = float(os.getenv("TV_BOT_STOP_LOSS_US", "0.04"))       # cut at -4%
US_MAX_HOLD_DAYS = int(os.getenv("TV_BOT_MAX_HOLD_DAYS_US", str(MAX_HOLD_DAYS)))
US_BREAKEVEN = float(os.getenv("TV_BOT_BREAKEVEN_US", "0.025"))      # arm breakeven at +2.5%

# --- tiered breakeven floor (applies to BOTH US and NSE) ---
# Once a position's PEAK gain reaches BREAKEVEN_ARM, the stop moves up to a floor
# that depends on the symbol's tier in the watchlist:
#   winner   -> FLOOR_WINNER   (default 0%  = pure breakeven; give room to run to +5%)
#   mediocre -> FLOOR_MEDIOCRE (default +1% = lock a small profit; don't wait around)
# Tag a symbol in the watchlist file: "AAPL winner" / "TSLA mediocre".
BREAKEVEN_ARM = float(os.getenv("TV_BOT_BREAKEVEN_ARM", str(US_BREAKEVEN)))
FLOOR_WINNER = float(os.getenv("TV_BOT_FLOOR_WINNER", "0.0"))
FLOOR_MEDIOCRE = float(os.getenv("TV_BOT_FLOOR_MEDIOCRE", "0.01"))


def _norm_tier(t) -> str:
    return "mediocre" if str(t).strip().lower() in ("mediocre", "m", "med") else "winner"


def floor_for_tier(tier: str) -> float:
    return FLOOR_MEDIOCRE if _norm_tier(tier) == "mediocre" else FLOOR_WINNER


DEFAULT_TIER = _norm_tier(os.getenv("TV_BOT_DEFAULT_TIER", "winner"))


def _trading_days_between(start, end) -> int:
    """Approximate trading days = weekdays between two datetimes (holidays ignored)."""
    from datetime import timedelta
    if start is None or end <= start:
        return 0
    days, cur = 0, start.date()
    while cur < end.date():
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def exit_reason(pnl_frac: Optional[float], held_tdays: Optional[int], flag: str,
                take_profit: Optional[float] = None, stop_loss: Optional[float] = None,
                max_hold_days: Optional[int] = None,
                breakeven_trigger: Optional[float] = None,
                peak_frac: Optional[float] = None,
                floor: float = 0.0) -> Optional[str]:
    """Why (if at all) a held position should be closed. Priority: risk first.

    Thresholds default to the module-level (NSE/default) values; the US scan
    passes its own set + a breakeven trigger. Priority order:
    - breakeven:   position was up >= breakeven_trigger (peak_frac) then fell back
                   to entry or below — lock in ~0% instead of riding to the stop
    - stop_loss:   position down >= stop_loss from entry (thesis failed)
    - take_profit: position up >= take_profit (the short-horizon edge, banked)
    - sell_flag:   the engine's technical picture turned bearish (emergency door)
    - time_exit:   held >= max_hold_days trading days with none of the above
    """
    tp = TAKE_PROFIT if take_profit is None else take_profit
    sl = STOP_LOSS if stop_loss is None else stop_loss
    mhd = MAX_HOLD_DAYS if max_hold_days is None else max_hold_days
    if pnl_frac is not None:
        # breakeven / profit-lock: armed once the peak gain reached the trigger;
        # fires when price has since faded to the tier's floor or below. floor=0
        # is pure breakeven (winner tier); floor>0 locks a small profit (mediocre).
        if (breakeven_trigger and peak_frac is not None
                and peak_frac >= breakeven_trigger and pnl_frac <= floor):
            return "breakeven" if floor <= 0 else "profit_lock"
        if sl > 0 and pnl_frac <= -sl:
            return "stop_loss"
        if tp > 0 and pnl_frac >= tp:
            return "take_profit"
    if flag == "SELL":
        return "sell_flag"
    if mhd > 0 and held_tdays is not None and held_tdays >= mhd:
        return "time_exit"
    return None


def _peak_gain_since(symbol: str, entry_price: Optional[float], entry_dt) -> Optional[float]:
    """Highest intraday gain fraction reached since entry — arms the breakeven
    stop. Derived from daily bars, so it needs no stored state. None if unknown
    (breakeven simply won't arm, which is safe)."""
    if not entry_price or entry_dt is None:
        return None
    try:
        import pandas as pd
        from stocks_agent.technicals.data import _clean, _history
        df = _clean(_history(symbol, "3mo", "1d"))
        if df.empty:
            return None
        ts = pd.Timestamp(entry_dt)
        highs = df.loc[df.index >= ts, "High"] if df.index.tz is not None else df["High"].tail(15)
        if highs.empty:
            highs = df["High"].tail(15)
        return float(highs.max()) / entry_price - 1
    except Exception:
        return None


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
    """Symbols only (first token per line) — tolerant of an optional tier tag."""
    return [sym for sym, _ in load_watchlist_tiered(path)]


def load_watchlist_tiered(path: str = WATCHLIST_FILE):
    """[(symbol, tier)] — a line is 'SYMBOL' or 'SYMBOL winner|mediocre'.
    Untagged symbols get DEFAULT_TIER. Tier decides the breakeven floor."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            sym = parts[0].upper()
            tier = _norm_tier(parts[1]) if len(parts) > 1 else DEFAULT_TIER
            out.append((sym, tier))
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

    tier_map = dict(load_watchlist_tiered())
    watchlist = list(tier_map)
    if not watchlist:
        say(f"ABORT: watchlist empty/missing ({WATCHLIST_FILE})")
        return {"status": "no_watchlist", "log": log}

    held: Dict[str, dict] = {p["symbol"].upper(): p for p in alpaca.positions()}
    open_count = len(held)
    say(f"Watchlist: {', '.join(watchlist)} | open positions: {open_count}")
    try:
        entry_times = alpaca.last_buy_fill_times() if held else {}
    except Exception:
        entry_times = {}

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

        if pos:
            # short-swing exits: stop-loss / take-profit / SELL flag / time-exit
            try:
                pnl_frac = float(pos.get("unrealized_plpc") or 0)
            except (TypeError, ValueError):
                pnl_frac = 0.0
            entry = entry_times.get(sym)
            held_days = _trading_days_between(entry, datetime.now(timezone.utc)) if entry else None
            try:
                entry_px = float(pos.get("avg_entry_price") or 0) or None
            except (TypeError, ValueError):
                entry_px = None
            tier = tier_map.get(sym, DEFAULT_TIER)
            floor = floor_for_tier(tier)
            peak = _peak_gain_since(sym, entry_px, entry) if BREAKEVEN_ARM else None
            reason = exit_reason(pnl_frac, held_days, flag,
                                 take_profit=US_TAKE_PROFIT, stop_loss=US_STOP_LOSS,
                                 max_hold_days=US_MAX_HOLD_DAYS,
                                 breakeven_trigger=BREAKEVEN_ARM, peak_frac=peak, floor=floor)
            if reason:
                if dry_run:
                    say(f"    -> would CLOSE {pos['qty']} ({reason}, {tier}, pnl {pnl_frac * 100:+.1f}%)")
                else:
                    alpaca.close_position(sym)
                    open_count -= 1
                    actions.append({"symbol": sym, "action": "close", "reason": reason, "tier": tier})
                    say(f"    -> position CLOSED ({reason}, {tier}, pnl {pnl_frac * 100:+.1f}%)")
            else:
                held_txt = f"day {held_days}/{MAX_HOLD_DAYS}" if held_days is not None else "entry date unknown"
                say(f"    -> holding [{tier}] (pnl {pnl_frac * 100:+.1f}%, {held_txt})")
        elif flag == "BUY":
            if conf < MIN_CONF:
                say(f"    -> skip buy (confidence {conf:.2f} < {MIN_CONF})")
            elif open_count >= MAX_POS:
                say(f"    -> skip buy (max positions {MAX_POS} reached)")
            elif dry_run:
                say(f"    -> would BUY ${NOTIONAL:.0f}")
            else:
                tier = tier_map.get(sym, DEFAULT_TIER)
                order = alpaca.buy_notional(sym, NOTIONAL, tier=tier)
                open_count += 1
                actions.append({"symbol": sym, "action": "buy", "order_id": order.get("id"), "tier": tier})
                say(f"    -> BUY ${NOTIONAL:.0f} [{tier}] submitted ({order.get('id', '?')[:8]})")
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

    tier_map = dict(load_watchlist_tiered(WATCHLIST_IN_FILE))
    watchlist = list(tier_map)
    if not watchlist:
        say(f"ABORT: NSE watchlist empty/missing ({WATCHLIST_IN_FILE})")
        return {"status": "no_watchlist", "log": log}

    p = vb.load()
    say(f"Virtual portfolio: cash ₹{p['cash']:,.0f}, positions: {len(p['positions'])}")

    actions = []
    prices: Dict[str, float] = {}
    open_count = len(p["positions"])
    for sym in watchlist:
        from stocks_agent.technicals import analyze_ticker
        result = analyze_ticker(sym, include_options=False)
        if "error" in result:
            say(f"  {sym}: SKIP ({result['error'][:80]})")
            continue
        flag, conf, price = result["flag"], result.get("confidence", 0), result["price"]
        prices[sym] = price
        pos = p["positions"].get(sym)
        say(f"  {sym}: {flag} conf={conf:.2f} score={result.get('score')} @₹{price}"
            + (f" | holding {pos['qty']}" if pos else ""))

        if pos:
            # short-swing exits: stop-loss / take-profit / SELL flag / time-exit
            pnl_frac = (price / pos["avg_price"] - 1) if pos.get("avg_price") else None
            opened = pos.get("opened_at")
            if not opened:  # position predates exit-rule tracking: start the clock now
                pos["opened_at"] = datetime.now(timezone.utc).isoformat()
                held_days = 0
            else:
                held_days = _trading_days_between(datetime.fromisoformat(opened),
                                                  datetime.now(timezone.utc))
            tier = tier_map.get(sym, DEFAULT_TIER)
            floor = floor_for_tier(tier)
            peak = _peak_gain_since(sym, pos.get("avg_price"),
                                    datetime.fromisoformat(pos["opened_at"])) if BREAKEVEN_ARM else None
            reason = exit_reason(pnl_frac, held_days, flag,
                                 breakeven_trigger=BREAKEVEN_ARM, peak_frac=peak, floor=floor)
            pnl_frac = pnl_frac or 0.0
            if reason:
                if dry_run:
                    say(f"    -> would SELL {pos['qty']} ({reason}, {tier}, pnl {pnl_frac * 100:+.1f}%)")
                else:
                    trade = vb.sell_all(p, sym, price, reason=reason, tier=tier)
                    if trade:
                        open_count -= 1
                        actions.append(trade)
                        say(f"    -> SOLD {trade['qty']} @ ₹{price} ({reason}, {tier}, pnl ₹{trade['pnl']:,.0f})")
            else:
                say(f"    -> holding [{tier}] (pnl {pnl_frac * 100:+.1f}%, day {held_days}/{MAX_HOLD_DAYS})")
        elif flag == "BUY":
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
        time.sleep(1.5)

    # snapshot equity for the performance chart (positions without a fresh
    # price fall back to their average buy price)
    equity = p["cash"] + sum(
        pos["qty"] * prices.get(s, pos["avg_price"]) for s, pos in p["positions"].items())
    snapped = False if dry_run else vb.record_snapshot(p, equity)

    if (actions or snapped) and not dry_run:
        vb.save(p)
        say(f"Portfolio saved to {vb.PORTFOLIO_FILE} (equity ₹{equity:,.0f}).")
    say(f"Done. {len(actions)} action(s).")
    return {"status": "ok", "actions": actions, "log": log}
