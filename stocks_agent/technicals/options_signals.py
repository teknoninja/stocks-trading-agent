"""Options-derived sentiment: put/call ratios, ATM implied volatility."""

from typing import Optional

import yfinance as yf


def options_sentiment(yf_symbol: str) -> Optional[dict]:
    """Free-tier options read from the nearest expiry chain.

    - Put/Call open-interest ratio: >1.3 bearish positioning, <0.7 bullish,
      >2.0 flagged as contrarian-bullish extreme.
    - ATM implied volatility reported for context.
    Returns None when the symbol has no options (non-US, crypto, etc.).
    """
    try:
        tk = yf.Ticker(yf_symbol)
        expiries = tk.options
        if not expiries:
            return None
        chain = tk.option_chain(expiries[0])
        calls, puts = chain.calls, chain.puts
        call_oi = float(calls["openInterest"].fillna(0).sum())
        put_oi = float(puts["openInterest"].fillna(0).sum())
        call_vol = float(calls["volume"].fillna(0).sum())
        put_vol = float(puts["volume"].fillna(0).sum())
        if call_oi <= 0:
            return None
        pcr_oi = put_oi / call_oi
        pcr_vol = (put_vol / call_vol) if call_vol > 0 else None

        spot = tk.fast_info.get("lastPrice") if hasattr(tk, "fast_info") else None
        atm_iv = None
        if spot:
            calls = calls.assign(dist=(calls["strike"] - spot).abs()).sort_values("dist")
            if len(calls):
                iv = calls.iloc[0].get("impliedVolatility")
                atm_iv = round(float(iv) * 100, 1) if iv == iv else None  # NaN check

        if pcr_oi >= 2.0:
            direction, read = "bullish", "extreme put positioning (contrarian bullish)"
        elif pcr_oi >= 1.3:
            direction, read = "bearish", "elevated put positioning"
        elif pcr_oi <= 0.7:
            direction, read = "bullish", "call-heavy positioning"
        else:
            direction, read = "neutral", "balanced positioning"

        return {
            "direction": direction,
            "read": read,
            "put_call_oi_ratio": round(pcr_oi, 2),
            "put_call_volume_ratio": round(pcr_vol, 2) if pcr_vol is not None else None,
            "atm_iv_pct": atm_iv,
            "expiry": expiries[0],
        }
    except Exception:
        return None
