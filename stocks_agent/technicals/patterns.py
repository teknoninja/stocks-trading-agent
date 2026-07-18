"""Pattern-based methods: harmonic patterns, Elliott-wave heuristic, Wyckoff phases."""

from typing import List, Optional

import pandas as pd

from .indicators import rsi
from .structure import Swing

# Harmonic pattern specs: acceptable fib ratio windows.
# ratios: AB/XA retracement, BC/AB retracement, AD/XA extension/retracement
HARMONIC_SPECS = {
    "Gartley":   {"ab_xa": (0.55, 0.68), "bc_ab": (0.382, 0.886), "ad_xa": (0.72, 0.85)},
    "Bat":       {"ab_xa": (0.35, 0.52), "bc_ab": (0.382, 0.886), "ad_xa": (0.83, 0.95)},
    "Butterfly": {"ab_xa": (0.72, 0.83), "bc_ab": (0.382, 0.886), "ad_xa": (1.15, 1.45)},
    "Crab":      {"ab_xa": (0.35, 0.65), "bc_ab": (0.382, 0.886), "ad_xa": (1.45, 1.75)},
}


def detect_harmonic(swings: List[Swing], df: pd.DataFrame) -> Optional[dict]:
    """Match the last 5 alternating swings (X,A,B,C,D) against harmonic specs.

    Bullish pattern: D is a low (expect reversal up). Bearish: D is a high.
    Only reported if D formed within the last 10 bars.
    """
    if len(swings) < 5 or df.empty:
        return None
    x, a, b, c, d = swings[-5:]
    if len(df) - 1 - d.pos > 10:
        return None
    xa = abs(a.price - x.price)
    ab = abs(b.price - a.price)
    bc = abs(c.price - b.price)
    ad = abs(d.price - a.price)
    if xa == 0 or ab == 0:
        return None
    ab_xa, bc_ab, ad_xa = ab / xa, bc / ab, ad / xa
    for name, spec in HARMONIC_SPECS.items():
        if (spec["ab_xa"][0] <= ab_xa <= spec["ab_xa"][1]
                and spec["bc_ab"][0] <= bc_ab <= spec["bc_ab"][1]
                and spec["ad_xa"][0] <= ad_xa <= spec["ad_xa"][1]):
            direction = "bullish" if d.kind == "L" else "bearish"
            return {"pattern": name, "direction": direction, "d_price": d.price,
                    "ratios": {"AB/XA": round(ab_xa, 3), "BC/AB": round(bc_ab, 3), "AD/XA": round(ad_xa, 3)}}
    return None


def elliott_heuristic(swings: List[Swing], df: pd.DataFrame) -> Optional[dict]:
    """Lightweight Elliott read: count consecutive impulse legs; flag possible
    wave-5 exhaustion when a 3rd+ consecutive higher-high (or lower-low) comes
    with fading RSI momentum. Deliberately low-confidence."""
    if len(swings) < 6 or len(df) < 40:
        return None
    hs = [s for s in swings if s.kind == "H"]
    ls = [s for s in swings if s.kind == "L"]
    if len(hs) < 3 or len(ls) < 3:
        return None
    r = rsi(df["Close"]).values

    up_legs = sum(1 for i in (-2, -1) if hs[i].price > hs[i - 1].price) \
        + sum(1 for i in (-2, -1) if ls[i].price > ls[i - 1].price)
    down_legs = sum(1 for i in (-2, -1) if hs[i].price < hs[i - 1].price) \
        + sum(1 for i in (-2, -1) if ls[i].price < ls[i - 1].price)

    if up_legs >= 3 and hs[-1].price > hs[-2].price and r[hs[-1].pos] < r[hs[-2].pos]:
        return {"read": "possible wave-5 exhaustion (uptrend mature, momentum fading)",
                "direction": "bearish"}
    if down_legs >= 3 and ls[-1].price < ls[-2].price and r[ls[-1].pos] > r[ls[-2].pos]:
        return {"read": "possible wave-5 exhaustion (downtrend mature, momentum fading)",
                "direction": "bullish"}
    if up_legs >= 3:
        return {"read": "impulse sequence up in progress", "direction": "bullish"}
    if down_legs >= 3:
        return {"read": "impulse sequence down in progress", "direction": "bearish"}
    return None


def wyckoff_phase(df: pd.DataFrame, range_bars: int = 40) -> Optional[dict]:
    """Wyckoff heuristic: a tight range after a trend, resolved by a spring
    (accumulation, bullish) or an upthrust (distribution, bearish)."""
    if len(df) < range_bars + 30:
        return None
    rng = df.tail(range_bars)
    prior = df.iloc[-(range_bars + 30): -range_bars]
    rng_hi, rng_lo = float(rng["High"].max()), float(rng["Low"].min())
    mid = (rng_hi + rng_lo) / 2
    width = (rng_hi - rng_lo) / mid if mid else 1.0
    if width > 0.12:  # not a consolidation
        return None
    prior_change = float(prior["Close"].iloc[-1] / prior["Close"].iloc[0] - 1)
    close = rng["Close"].values
    low = rng["Low"].values
    high = rng["High"].values
    last_close = float(close[-1])

    # spring: dip below range low then close back inside upper half
    spring = any(low[i] < rng_lo * 1.001 and close[i] > rng_lo for i in range(len(rng) - 8, len(rng)))
    upthrust = any(high[i] > rng_hi * 0.999 and close[i] < rng_hi for i in range(len(rng) - 8, len(rng)))

    if prior_change < -0.05 and spring and last_close > mid:
        return {"phase": "accumulation (spring detected)", "direction": "bullish"}
    if prior_change > 0.05 and upthrust and last_close < mid:
        return {"phase": "distribution (upthrust detected)", "direction": "bearish"}
    return None
