"""Quantitative/systematic signals: mean reversion, trend following, breakouts, VWAP."""

from typing import List, Optional

import pandas as pd

from .indicators import adx, anchored_vwap, atr, bollinger, ema, sma, zscore


def mean_reversion_signal(df: pd.DataFrame) -> Optional[dict]:
    """Z-score / Bollinger extreme with a volatility filter (skip if ADX says trending)."""
    if len(df) < 40:
        return None
    z = float(zscore(df["Close"]).iloc[-1])
    adx_val = float(adx(df)[0].iloc[-1] or 0)
    mid, upper, lower = bollinger(df["Close"])
    price = float(df["Close"].iloc[-1])
    if pd.isna(z):
        return None
    trending = adx_val >= 28  # strong trend: fade signals are unreliable
    if z <= -2.0 or price < float(lower.iloc[-1] or price):
        return {"direction": "bullish", "zscore": round(z, 2), "adx": round(adx_val, 1),
                "suppressed_by_trend": trending}
    if z >= 2.0 or price > float(upper.iloc[-1] or price):
        return {"direction": "bearish", "zscore": round(z, 2), "adx": round(adx_val, 1),
                "suppressed_by_trend": trending}
    return None


def trend_following_signal(df: pd.DataFrame) -> Optional[dict]:
    """EMA(20/50) alignment + 50/200 SMA cross regime + ADX strength."""
    if len(df) < 60:
        return None
    close = df["Close"]
    e20, e50 = ema(close, 20), ema(close, 50)
    s50 = sma(close, 50)
    s200 = sma(close, 200) if len(df) >= 200 else s50
    adx_val, plus_di, minus_di = adx(df)
    a = float(adx_val.iloc[-1] or 0)
    price = float(close.iloc[-1])

    bull = price > float(e20.iloc[-1]) > float(e50.iloc[-1])
    bear = price < float(e20.iloc[-1]) < float(e50.iloc[-1])
    golden = float(s50.iloc[-1]) > float(s200.iloc[-1])
    di_bull = float(plus_di.iloc[-1] or 0) > float(minus_di.iloc[-1] or 0)

    if bull and golden:
        direction = "bullish"
    elif bear and not golden:
        direction = "bearish"
    else:
        return {"direction": "neutral", "adx": round(a, 1), "regime": "golden" if golden else "death"}
    return {"direction": direction, "adx": round(a, 1),
            "strength": "strong" if a >= 25 else "weak",
            "regime": "golden_cross" if golden else "death_cross", "di_bullish": di_bull}


def breakout_signal(df: pd.DataFrame, channel: int = 20) -> Optional[dict]:
    """Donchian channel breakout confirmed by volume expansion."""
    if len(df) < channel + 10:
        return None
    hi = float(df["High"].rolling(channel).max().shift(1).iloc[-1])
    lo = float(df["Low"].rolling(channel).min().shift(1).iloc[-1])
    price = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])
    avg_vol = float(df["Volume"].rolling(channel).mean().iloc[-1] or 0)
    vol_confirm = avg_vol > 0 and vol > 1.3 * avg_vol
    if price > hi:
        return {"direction": "bullish", "level": hi, "volume_confirmed": vol_confirm}
    if price < lo:
        return {"direction": "bearish", "level": lo, "volume_confirmed": vol_confirm}
    return None


def vwap_signal(df: pd.DataFrame, swings) -> Optional[dict]:
    """Price vs VWAP anchored at the last major swing low/high."""
    if len(df) < 30:
        return None
    anchor = swings[-4].pos if len(swings) >= 4 else 0
    vwap = anchored_vwap(df, anchor)
    v = float(vwap.iloc[-1])
    price = float(df["Close"].iloc[-1])
    a = float(atr(df).iloc[-1] or 0)
    if a and abs(price - v) < 0.3 * a:
        stance = "at_vwap"
        direction = "neutral"
    elif price > v:
        stance, direction = "above_vwap", "bullish"
    else:
        stance, direction = "below_vwap", "bearish"
    return {"direction": direction, "vwap": round(v, 4), "price": price, "stance": stance}
