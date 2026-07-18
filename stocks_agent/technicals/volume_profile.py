"""Volume profile: VPOC, value area, high/low volume nodes."""

from typing import Optional

import numpy as np
import pandas as pd


def volume_profile(df: pd.DataFrame, bins: int = 40, lookback: int = 250) -> Optional[dict]:
    """Build a volume-by-price histogram spreading each bar's volume across its range."""
    df = df.tail(lookback)
    if len(df) < 30 or df["Volume"].sum() <= 0:
        return None
    lo = float(df["Low"].min())
    hi = float(df["High"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    hist = np.zeros(bins)
    for h, l, v in zip(df["High"].values, df["Low"].values, df["Volume"].values):
        if v <= 0:
            continue
        lo_bin = int(np.clip(np.searchsorted(edges, l, side="right") - 1, 0, bins - 1))
        hi_bin = int(np.clip(np.searchsorted(edges, h, side="right") - 1, 0, bins - 1))
        span = hi_bin - lo_bin + 1
        hist[lo_bin: hi_bin + 1] += v / span

    vpoc_bin = int(hist.argmax())
    vpoc = float((edges[vpoc_bin] + edges[vpoc_bin + 1]) / 2)

    # value area: expand around VPOC until 70% of volume covered
    total = hist.sum()
    covered = hist[vpoc_bin]
    lo_b, hi_b = vpoc_bin, vpoc_bin
    while covered < 0.70 * total and (lo_b > 0 or hi_b < bins - 1):
        left = hist[lo_b - 1] if lo_b > 0 else -1
        right = hist[hi_b + 1] if hi_b < bins - 1 else -1
        if right >= left:
            hi_b += 1
            covered += hist[hi_b]
        else:
            lo_b -= 1
            covered += hist[lo_b]
    val = float(edges[lo_b])          # value area low
    vah = float(edges[hi_b + 1])      # value area high

    price = float(df["Close"].iloc[-1])
    if price > vah:
        position = "above_value"
    elif price < val:
        position = "below_value"
    else:
        position = "inside_value"

    return {
        "vpoc": vpoc,
        "value_area_high": vah,
        "value_area_low": val,
        "price": price,
        "position": position,
    }
