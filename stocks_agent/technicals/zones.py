"""Supply/demand zones, order blocks, and liquidity sweeps (smart-money concepts)."""

from typing import List, Optional

import pandas as pd

from .indicators import atr
from .structure import Swing


def find_zones(df: pd.DataFrame, impulse_atr_mult: float = 2.0, max_zones: int = 6) -> List[dict]:
    """Find unmitigated supply/demand zones.

    A zone is the range of the base candle(s) immediately before a sharp
    departure (move of >= impulse_atr_mult * ATR within 3 bars). Upward
    departure => demand zone below; downward departure => supply zone above.
    A zone is 'mitigated' (dropped) once price has closed through its far side.
    """
    if len(df) < 30:
        return []
    a = atr(df).values
    close, high, low = df["Close"].values, df["High"].values, df["Low"].values
    op = df["Open"].values
    n = len(df)
    zones: List[dict] = []

    for i in range(5, n - 3):
        move = close[i + 3] - close[i]
        if a[i] and abs(move) >= impulse_atr_mult * a[i]:
            kind = "demand" if move > 0 else "supply"
            top = max(high[i], op[i])
            bottom = min(low[i], op[i])
            mitigated = False
            for j in range(i + 4, n):
                if kind == "demand" and close[j] < bottom:
                    mitigated = True
                    break
                if kind == "supply" and close[j] > top:
                    mitigated = True
                    break
            if not mitigated:
                zones.append({"kind": kind, "top": float(top), "bottom": float(bottom), "pos": i})

    # dedupe overlapping zones, keep most recent
    zones.sort(key=lambda z: z["pos"], reverse=True)
    merged: List[dict] = []
    for z in zones:
        overlap = any(
            m["kind"] == z["kind"] and not (z["top"] < m["bottom"] or z["bottom"] > m["top"])
            for m in merged
        )
        if not overlap:
            merged.append(z)
    return merged[:max_zones]


def zone_signal(df: pd.DataFrame, zones: List[dict]) -> Optional[dict]:
    """Is current price inside/near a fresh zone? Demand => bullish reaction expected."""
    if df.empty or not zones:
        return None
    price = float(df["Close"].iloc[-1])
    a = float(atr(df).iloc[-1] or 0)
    for z in zones:
        near = a * 0.5
        if z["bottom"] - near <= price <= z["top"] + near:
            return {
                "kind": z["kind"],
                "top": z["top"],
                "bottom": z["bottom"],
                "direction": "bullish" if z["kind"] == "demand" else "bearish",
            }
    return None


def find_order_block(df: pd.DataFrame, impulse_atr_mult: float = 2.0) -> Optional[dict]:
    """Most recent order block: last opposite-colored candle before an impulse."""
    if len(df) < 30:
        return None
    a = atr(df).values
    close, op = df["Close"].values, df["Open"].values
    high, low = df["High"].values, df["Low"].values
    n = len(df)
    for i in range(n - 4, 5, -1):
        move = close[min(i + 3, n - 1)] - close[i]
        if not a[i] or abs(move) < impulse_atr_mult * a[i]:
            continue
        bullish_impulse = move > 0
        # walk back to find last opposite candle
        for j in range(i, max(i - 4, 0), -1):
            candle_bearish = close[j] < op[j]
            if bullish_impulse and candle_bearish:
                return {"direction": "bullish", "top": float(high[j]), "bottom": float(low[j]), "pos": j}
            if not bullish_impulse and not candle_bearish:
                return {"direction": "bearish", "top": float(high[j]), "bottom": float(low[j]), "pos": j}
        break
    return None


def detect_liquidity_sweep(df: pd.DataFrame, swings: List[Swing], lookback: int = 8) -> Optional[dict]:
    """Stop hunt: wick beyond a prior swing extreme but close back inside.

    Sweep of lows => bullish (sell-side liquidity grabbed), sweep of highs => bearish.
    """
    if df.empty or len(swings) < 2:
        return None
    n = len(df)
    high, low, close = df["High"].values, df["Low"].values, df["Close"].values
    recent_highs = [s for s in swings if s.kind == "H" and s.pos < n - lookback]
    recent_lows = [s for s in swings if s.kind == "L" and s.pos < n - lookback]
    for i in range(max(0, n - lookback), n):
        for s in recent_lows[-3:]:
            if low[i] < s.price and close[i] > s.price:
                return {"direction": "bullish", "level": s.price, "bars_ago": n - 1 - i}
        for s in recent_highs[-3:]:
            if high[i] > s.price and close[i] < s.price:
                return {"direction": "bearish", "level": s.price, "bars_ago": n - 1 - i}
    return None
