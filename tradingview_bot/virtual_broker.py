"""Built-in virtual paper broker — used for markets Alpaca can't trade (NSE).

The portfolio is a JSON file tracked IN THE GIT REPO (nse_portfolio.json), so:
  - GitHub Actions runs trade on it and commit the updated file back
  - your laptop shows/reset it, syncing through the GitHub contents API
    when GITHUB_TOKEN (with Contents read/write) + GITHUB_REPO are set

Fills are simulated at the latest Yahoo price — an honest approximation for
strategy validation, same idea as any paper account. Whole shares only.
"""

import base64
import json
import os
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests

PORTFOLIO_FILE = os.getenv("TV_BOT_PORTFOLIO", "nse_portfolio.json")
START_CASH = float(os.getenv("TV_BOT_START_CASH_INR", "1000000"))   # ₹10,00,000
NOTIONAL = float(os.getenv("TV_BOT_NOTIONAL_INR", "1000"))          # ₹ budget per position
# NSE trades whole shares only. Many large-caps cost more than the budget per
# single share, so a buy falls back to exactly 1 share when the budget is short.
MIN_ONE_SHARE = os.getenv("TV_BOT_MIN_ONE_SHARE", "1") != "0"
IST = ZoneInfo("Asia/Kolkata")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def nse_market_open(now: Optional[datetime] = None) -> bool:
    """NSE regular session: Mon-Fri 09:15-15:30 IST (holidays not modeled)."""
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


# ---------------- state ------------------------------------------------------

def _default(starting_cash: float = START_CASH) -> dict:
    return {
        "currency": "INR",
        "starting_cash": starting_cash,
        "cash": starting_cash,
        "positions": {},          # symbol -> {qty, avg_price}
        "trades": [],             # [{ts, symbol, side, qty, price}]
        "equity_history": [],     # [{ts, equity}] snapshots for the chart
        "created_at": _now(),
        "updated_at": _now(),
    }


def record_snapshot(portfolio: dict, equity: float, min_gap_minutes: int = 25) -> bool:
    """Append an equity snapshot for the performance chart.

    Deduped: skipped when the last snapshot is newer than min_gap_minutes.
    Returns True when a snapshot was added (caller should save).
    """
    hist = portfolio.setdefault("equity_history", [])
    if hist:
        last = datetime.fromisoformat(hist[-1]["ts"])
        age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if age_min < min_gap_minutes:
            return False
    hist.append({"ts": _now(), "equity": round(equity, 2)})
    del hist[:-5000]  # cap
    return True


def load(path: str = PORTFOLIO_FILE) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return _default()


def save(portfolio: dict, path: str = PORTFOLIO_FILE) -> None:
    portfolio["updated_at"] = _now()
    with open(path, "w") as f:
        json.dump(portfolio, f, indent=2)


def reset(starting_cash: float, path: str = PORTFOLIO_FILE) -> dict:
    p = _default(starting_cash)
    save(p, path)
    return p


# ---------------- trading ----------------------------------------------------

def buy(portfolio: dict, symbol: str, price: float, notional: float = NOTIONAL) -> Optional[dict]:
    """Buy whole shares worth ~notional. Returns the trade or None if not possible."""
    if price <= 0:
        return None
    qty = int(min(notional, portfolio["cash"]) // price)
    if qty <= 0 and MIN_ONE_SHARE and portfolio["cash"] >= price:
        qty = 1  # budget below share price: take the smallest possible position
    if qty <= 0:
        return None
    cost = qty * price
    pos = portfolio["positions"].get(symbol, {"qty": 0, "avg_price": 0.0})
    if pos["qty"] == 0:
        pos["opened_at"] = _now()  # entry date for the time-exit rule
    total_qty = pos["qty"] + qty
    pos["avg_price"] = (pos["avg_price"] * pos["qty"] + cost) / total_qty
    pos["qty"] = total_qty
    portfolio["positions"][symbol] = pos
    portfolio["cash"] -= cost
    trade = {"ts": _now(), "symbol": symbol, "side": "buy", "qty": qty, "price": price}
    portfolio["trades"].append(trade)
    return trade


def sell_all(portfolio: dict, symbol: str, price: float, reason: str = "manual",
             tier: str = "") -> Optional[dict]:
    pos = portfolio["positions"].pop(symbol, None)
    if not pos or price <= 0:
        return None
    portfolio["cash"] += pos["qty"] * price
    trade = {"ts": _now(), "symbol": symbol, "side": "sell", "qty": pos["qty"],
             "price": price, "pnl": round((price - pos["avg_price"]) * pos["qty"], 2),
             "reason": reason}
    if tier:
        trade["tier"] = tier  # for journal analysis: which floor made more money
    portfolio["trades"].append(trade)
    return trade


def valuation(portfolio: dict, price_fn: Callable[[str], Optional[float]]) -> dict:
    """Mark-to-market using price_fn(symbol) -> latest price (or None)."""
    rows, market_value, invested_total = [], 0.0, 0.0
    for sym, pos in portfolio["positions"].items():
        price = price_fn(sym) or pos["avg_price"]
        value = pos["qty"] * price
        invested = pos["qty"] * pos["avg_price"]
        market_value += value
        invested_total += invested
        rows.append({
            "symbol": sym, "qty": pos["qty"], "avg_price": round(pos["avg_price"], 2),
            "price": round(price, 2), "invested": round(invested, 2), "value": round(value, 2),
            "pnl": round((price - pos["avg_price"]) * pos["qty"], 2),
            "pnl_pct": round((price / pos["avg_price"] - 1) * 100, 2) if pos["avg_price"] else 0,
        })
    equity = portfolio["cash"] + market_value
    start = portfolio["starting_cash"] or 1
    # realized P&L: booked on every sell trade; unrealized split: winners vs losers
    realized = round(sum(t.get("pnl", 0) or 0 for t in portfolio["trades"]
                         if t.get("side") == "sell"), 2)
    earned = round(sum(r["pnl"] for r in rows if r["pnl"] > 0), 2)
    lost = round(sum(-r["pnl"] for r in rows if r["pnl"] < 0), 2)
    return {
        "invested_total": round(invested_total, 2),
        "unrealized_earned": earned,
        "unrealized_lost": lost,
        "realized_pnl": realized,
        "currency": portfolio["currency"],
        "starting_cash": portfolio["starting_cash"],
        "cash": round(portfolio["cash"], 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "total_return_pct": round((equity / start - 1) * 100, 2),
        "positions": rows,
        "n_trades": len(portfolio["trades"]),
        "recent_trades": portfolio["trades"][-10:][::-1],
        "updated_at": portfolio.get("updated_at"),
    }


# ---------------- GitHub file sync (canonical copy lives in the repo) ---------

def _gh_config():
    repo = os.getenv("GITHUB_REPOSITORY") or os.getenv("GITHUB_REPO")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if not repo or not token:
        return None, None
    return repo, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def gh_fetch() -> Optional[dict]:
    """Latest portfolio from the repo (None if unavailable/not configured)."""
    repo, headers = _gh_config()
    if not repo:
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{PORTFOLIO_FILE}",
                         headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        return json.loads(base64.b64decode(r.json()["content"]))
    except Exception:
        return None


def gh_push(portfolio: dict, message: str) -> bool:
    """Write the portfolio file to the repo via the contents API."""
    repo, headers = _gh_config()
    if not repo:
        return False
    try:
        url = f"https://api.github.com/repos/{repo}/contents/{PORTFOLIO_FILE}"
        r = requests.get(url, headers=headers, timeout=15)
        sha = r.json().get("sha") if r.status_code == 200 else None
        body = {
            "message": message,
            "content": base64.b64encode(
                json.dumps(portfolio, indent=2).encode()).decode(),
        }
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=headers, json=body, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False
