"""Alpaca PAPER trading client — plain REST, no SDK.

All endpoints hit the paper environment (fake money) unless ALPACA_BASE_URL
is overridden. Keys come from ALPACA_API_KEY / ALPACA_SECRET_KEY.
Docs: https://docs.alpaca.markets/reference
"""

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import pandas as pd
import requests

BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL = "https://data.alpaca.markets"
ORDER_PREFIX = "tvbot"  # marks orders placed by this project


def _keys():
    return os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")


def configured() -> bool:
    k, s = _keys()
    return bool(k and s)


def _headers() -> dict:
    k, s = _keys()
    return {"APCA-API-KEY-ID": k or "", "APCA-API-SECRET-KEY": s or ""}


def _request(method: str, path: str, base: str = BASE_URL, **kwargs):
    r = requests.request(method, base + path, headers=_headers(), timeout=20, **kwargs)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json() if r.text else {}


def account() -> dict:
    return _request("GET", "/v2/account")


def clock() -> dict:
    """{'is_open': bool, 'next_open': ..., 'next_close': ...}"""
    return _request("GET", "/v2/clock")


def positions() -> List[dict]:
    return _request("GET", "/v2/positions") or []


def position(symbol: str) -> Optional[dict]:
    return _request("GET", f"/v2/positions/{symbol.upper()}")


def tradable(symbol: str) -> bool:
    asset = _request("GET", f"/v2/assets/{symbol.upper()}")
    return bool(asset and asset.get("tradable"))


def buy_notional(symbol: str, notional: float) -> dict:
    """Market-buy a fixed dollar amount (fractional shares allowed)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return _request("POST", "/v2/orders", json={
        "symbol": symbol.upper(),
        "notional": str(round(notional, 2)),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": f"{ORDER_PREFIX}-{symbol.upper()}-{stamp}",
    })


def close_position(symbol: str) -> Optional[dict]:
    """Market-close the whole position. Returns None if no position exists."""
    return _request("DELETE", f"/v2/positions/{symbol.upper()}")


def daily_bars(symbol: str, years: int = 2) -> pd.DataFrame:
    """Free IEX-feed daily bars — fallback when Yahoo rate-limits the scanner."""
    start = (datetime.now(timezone.utc) - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    rows, token = [], None
    while True:
        params = {"timeframe": "1Day", "start": start, "limit": 10000,
                  "adjustment": "split", "feed": "iex"}
        if token:
            params["page_token"] = token
        data = _request("GET", f"/v2/stocks/{symbol.upper()}/bars", base=DATA_URL, params=params)
        if not data:
            break
        rows.extend(data.get("bars") or [])
        token = data.get("next_page_token")
        if not token:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.index = pd.to_datetime(df["t"])
    return df.rename(columns={"o": "Open", "h": "High", "l": "Low",
                              "c": "Close", "v": "Volume"})[
        ["Open", "High", "Low", "Close", "Volume"]]
