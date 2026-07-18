"""RSI / MACD divergence detection against price swing points."""

from typing import List, Optional

import pandas as pd

from .indicators import macd, rsi
from .structure import Swing


def _divergence_at_swings(price_vals, ind_vals, swings: List[Swing], recent_bars: int) -> Optional[dict]:
    n = len(price_vals)
    highs = [s for s in swings if s.kind == "H"][-2:]
    lows = [s for s in swings if s.kind == "L"][-2:]

    # bearish: price higher high, indicator lower high
    if len(highs) == 2 and n - 1 - highs[-1].pos <= recent_bars:
        p1, p2 = highs[0], highs[1]
        if p2.price > p1.price and ind_vals[p2.pos] < ind_vals[p1.pos]:
            return {"direction": "bearish", "type": "regular"}
    # bullish: price lower low, indicator higher low
    if len(lows) == 2 and n - 1 - lows[-1].pos <= recent_bars:
        p1, p2 = lows[0], lows[1]
        if p2.price < p1.price and ind_vals[p2.pos] > ind_vals[p1.pos]:
            return {"direction": "bullish", "type": "regular"}
    return None


def detect_divergences(df: pd.DataFrame, swings: List[Swing], recent_bars: int = 15) -> List[dict]:
    """Return list of active divergences, e.g. [{'indicator': 'RSI', 'direction': 'bearish'}]."""
    if len(df) < 40 or len(swings) < 4:
        return []
    out = []
    close = df["Close"].values
    r = rsi(df["Close"]).values
    d = _divergence_at_swings(close, r, swings, recent_bars)
    if d:
        out.append({"indicator": "RSI", **d})
    _, _, hist = macd(df["Close"])
    d = _divergence_at_swings(close, hist.values, swings, recent_bars)
    if d:
        out.append({"indicator": "MACD", **d})
    return out
