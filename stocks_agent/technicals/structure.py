"""Market structure: swing points, HH/HL vs LH/LL trend, BOS and CHOCH."""

from dataclasses import dataclass
from typing import List, Optional

import pandas as pd


@dataclass
class Swing:
    kind: str    # "H" or "L"
    pos: int     # integer position in the frame
    price: float


def find_swings(df: pd.DataFrame, lookback: int = 3) -> List[Swing]:
    """Fractal swing highs/lows: bar is an extreme of the +/- lookback window.

    Consecutive same-kind swings are collapsed to the more extreme one so the
    result strictly alternates H, L, H, L...
    """
    highs, lows = df["High"].values, df["Low"].values
    n = len(df)
    raw: List[Swing] = []
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback: i + lookback + 1]
        window_l = lows[i - lookback: i + lookback + 1]
        if highs[i] == window_h.max():
            raw.append(Swing("H", i, float(highs[i])))
        if lows[i] == window_l.min():
            raw.append(Swing("L", i, float(lows[i])))
    raw.sort(key=lambda s: s.pos)

    swings: List[Swing] = []
    for s in raw:
        if swings and swings[-1].kind == s.kind:
            prev = swings[-1]
            better = (s.price > prev.price) if s.kind == "H" else (s.price < prev.price)
            if better:
                swings[-1] = s
        else:
            swings.append(s)
    return swings


def classify_trend(swings: List[Swing]) -> str:
    """'up' (HH+HL), 'down' (LH+LL), or 'range' from the last few swings."""
    hs = [s for s in swings if s.kind == "H"][-3:]
    ls = [s for s in swings if s.kind == "L"][-3:]
    if len(hs) < 2 or len(ls) < 2:
        return "range"
    higher_highs = hs[-1].price > hs[-2].price
    higher_lows = ls[-1].price > ls[-2].price
    lower_highs = hs[-1].price < hs[-2].price
    lower_lows = ls[-1].price < ls[-2].price
    if higher_highs and higher_lows:
        return "up"
    if lower_highs and lower_lows:
        return "down"
    return "range"


def detect_break(df: pd.DataFrame, swings: List[Swing], trend: str) -> Optional[dict]:
    """Detect a recent BOS (trend continuation break) or CHOCH (character change).

    BOS: close breaks the last swing extreme in the direction of the trend.
    CHOCH: close breaks the last opposing swing against the prevailing trend.
    Only reports breaks that happened within the last 10 bars.
    """
    if len(swings) < 2 or df.empty:
        return None
    close = df["Close"].values
    n = len(df)
    last_high = next((s for s in reversed(swings) if s.kind == "H"), None)
    last_low = next((s for s in reversed(swings) if s.kind == "L"), None)

    events = []
    if last_high is not None:
        broke = [i for i in range(last_high.pos + 1, n) if close[i] > last_high.price]
        if broke and n - broke[0] <= 10:
            kind = "BOS" if trend == "up" else "CHOCH"
            events.append({"event": kind, "direction": "bullish", "level": last_high.price,
                           "bars_ago": n - 1 - broke[0]})
    if last_low is not None:
        broke = [i for i in range(last_low.pos + 1, n) if close[i] < last_low.price]
        if broke and n - broke[0] <= 10:
            kind = "BOS" if trend == "down" else "CHOCH"
            events.append({"event": kind, "direction": "bearish", "level": last_low.price,
                           "bars_ago": n - 1 - broke[0]})
    if not events:
        return None
    return min(events, key=lambda e: e["bars_ago"])
