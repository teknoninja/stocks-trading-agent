"""Analysis engine: run every strategy across timeframes, produce the flag."""

from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from . import scoring
from .data import DataSourceError, fetch_frames, resolve_symbol
from .divergence import detect_divergences
from .options_signals import options_sentiment
from .patterns import detect_harmonic, elliott_heuristic, wyckoff_phase
from .quant import breakout_signal, mean_reversion_signal, trend_following_signal, vwap_signal
from .scoring import Signal
from .structure import classify_trend, detect_break, find_swings
from .volume_profile import volume_profile
from .zones import detect_liquidity_sweep, find_order_block, find_zones, zone_signal

_DIR = {"bullish": 1, "bearish": -1, "neutral": 0}

# per-timeframe weight multiplier (higher timeframe = more weight)
TF_MULT = {"1W": 1.3, "1D": 1.0, "4H": 0.8, "1H": 0.6}


def _analyze_frame(tf: str, df: pd.DataFrame) -> List[Signal]:
    """Run all price-based strategies on one timeframe."""
    signals: List[Signal] = []
    if df is None or len(df) < 40:
        return signals
    m = TF_MULT.get(tf, 1.0)
    swings = find_swings(df)
    trend = classify_trend(swings)

    # 1. Market structure (HH/HL vs LH/LL)
    tdir = 1 if trend == "up" else -1 if trend == "down" else 0
    signals.append(Signal("market_structure", tf, tdir, 0.8 if tdir else 0.2, 1.5 * m,
                          f"structure is {trend} ({'HH/HL' if trend == 'up' else 'LH/LL' if trend == 'down' else 'no clear sequence'})"))

    # 2. BOS / CHOCH
    brk = detect_break(df, swings, trend)
    if brk:
        d = _DIR[brk["direction"]]
        strength = 0.9 if brk["event"] == "CHOCH" else 0.7
        signals.append(Signal("bos_choch", tf, d, strength, 1.2 * m,
                              f"{brk['event']} {brk['direction']} through {brk['level']:.2f} ({brk['bars_ago']} bars ago)",
                              brk))

    # 3. Supply/demand zones
    zones = find_zones(df)
    zs = zone_signal(df, zones)
    if zs:
        signals.append(Signal("supply_demand", tf, _DIR[zs["direction"]], 0.7, 1.0 * m,
                              f"price is in a fresh {zs['kind']} zone {zs['bottom']:.2f}-{zs['top']:.2f}", zs))

    # 4. Order block + liquidity sweep (smart-money concepts)
    ob = find_order_block(df)
    if ob:
        price = float(df["Close"].iloc[-1])
        if ob["bottom"] * 0.98 <= price <= ob["top"] * 1.02:
            signals.append(Signal("order_block", tf, _DIR[ob["direction"]], 0.6, 0.8 * m,
                                  f"price testing {ob['direction']} order block {ob['bottom']:.2f}-{ob['top']:.2f}", ob))
    sweep = detect_liquidity_sweep(df, swings)
    if sweep:
        signals.append(Signal("liquidity_sweep", tf, _DIR[sweep["direction"]], 0.75, 0.9 * m,
                              f"liquidity sweep at {sweep['level']:.2f} -> {sweep['direction']} ({sweep['bars_ago']} bars ago)",
                              sweep))

    # 5. Divergences
    for div in detect_divergences(df, swings):
        signals.append(Signal("divergence", tf, _DIR[div["direction"]], 0.7, 1.0 * m,
                              f"{div['direction']} {div['indicator']} divergence", div))

    # 6. Volume profile
    vp = volume_profile(df)
    if vp:
        if vp["position"] == "above_value":
            d, txt = 1, f"price above value area (VAH {vp['value_area_high']:.2f}, VPOC {vp['vpoc']:.2f}) — acceptance higher"
        elif vp["position"] == "below_value":
            d, txt = -1, f"price below value area (VAL {vp['value_area_low']:.2f}, VPOC {vp['vpoc']:.2f}) — acceptance lower"
        else:
            d, txt = 0, f"price inside value area (VPOC {vp['vpoc']:.2f}) — balanced"
        signals.append(Signal("volume_profile", tf, d, 0.5 if d else 0.2, 0.8 * m, txt, vp))

    # 7. Anchored VWAP
    vw = vwap_signal(df, swings)
    if vw:
        signals.append(Signal("vwap", tf, _DIR[vw["direction"]], 0.5, 0.7 * m,
                              f"price {vw['stance'].replace('_', ' ')} (anchored VWAP {vw['vwap']:.2f})", vw))

    # 8. Harmonic patterns
    h = detect_harmonic(swings, df)
    if h:
        signals.append(Signal("harmonic", tf, _DIR[h["direction"]], 0.6, 0.6 * m,
                              f"{h['direction']} {h['pattern']} completed at {h['d_price']:.2f}", h))

    # 9. Elliott wave heuristic
    e = elliott_heuristic(swings, df)
    if e:
        signals.append(Signal("elliott", tf, _DIR[e["direction"]], 0.4, 0.4 * m, e["read"], e))

    # 10. Wyckoff
    w = wyckoff_phase(df)
    if w:
        signals.append(Signal("wyckoff", tf, _DIR[w["direction"]], 0.75, 0.8 * m,
                              f"Wyckoff {w['phase']}", w))

    # 11. Mean reversion (z-score / Bollinger extremes, volatility-filtered)
    mr = mean_reversion_signal(df)
    if mr:
        strength = 0.25 if mr["suppressed_by_trend"] else 0.65
        signals.append(Signal("mean_reversion", tf, _DIR[mr["direction"]], strength, 0.8 * m,
                              f"z-score {mr['zscore']} extreme -> {mr['direction']} reversion"
                              + (" (muted: strong trend)" if mr["suppressed_by_trend"] else ""), mr))

    # 12. Trend following (EMA stack + golden/death cross + ADX)
    tfl = trend_following_signal(df)
    if tfl and tfl["direction"] != "neutral":
        strength = 0.85 if tfl.get("strength") == "strong" else 0.55
        signals.append(Signal("trend_following", tf, _DIR[tfl["direction"]], strength, 1.2 * m,
                              f"{tfl['direction']} EMA stack, {tfl['regime']}, ADX {tfl['adx']}", tfl))

    # 13. Breakout
    bo = breakout_signal(df)
    if bo:
        strength = 0.8 if bo["volume_confirmed"] else 0.5
        signals.append(Signal("breakout", tf, _DIR[bo["direction"]], strength, 0.8 * m,
                              f"{bo['direction']} 20-bar breakout past {bo['level']:.2f}"
                              + (" on volume" if bo["volume_confirmed"] else " (volume unconfirmed)"), bo))

    return signals


def _mtf_alignment(frames: Dict[str, pd.DataFrame]) -> Optional[Signal]:
    """Multi-timeframe confluence: do 1W / 1D / 4H structures agree?"""
    trends = {}
    for tf in ("1W", "1D", "4H"):
        df = frames.get(tf)
        if df is not None and len(df) >= 40:
            trends[tf] = classify_trend(find_swings(df))
    if len(trends) < 2:
        return None
    ups = sum(1 for t in trends.values() if t == "up")
    downs = sum(1 for t in trends.values() if t == "down")
    desc = ", ".join(f"{k}:{v}" for k, v in trends.items())
    if ups == len(trends):
        return Signal("mtf_alignment", "1D", 1, 0.9, 1.5, f"all timeframes aligned up ({desc})", trends)
    if downs == len(trends):
        return Signal("mtf_alignment", "1D", -1, 0.9, 1.5, f"all timeframes aligned down ({desc})", trends)
    if ups > downs:
        return Signal("mtf_alignment", "1D", 1, 0.4, 1.5, f"timeframes mostly up ({desc})", trends)
    if downs > ups:
        return Signal("mtf_alignment", "1D", -1, 0.4, 1.5, f"timeframes mostly down ({desc})", trends)
    return Signal("mtf_alignment", "1D", 0, 0.2, 1.5, f"timeframes conflicting ({desc})", trends)


def analyze_frames(frames: Dict[str, pd.DataFrame]) -> dict:
    """Run all price-based strategies on pre-fetched frames and aggregate.

    Used by analyze_ticker (live) and by the backtester (historical slices —
    which only have 1W/1D frames; missing/short frames are skipped safely).
    """
    signals: List[Signal] = []
    for tf in ("1W", "1D", "4H", "1H"):
        signals.extend(_analyze_frame(tf, frames.get(tf)))
    mtf = _mtf_alignment(frames)
    if mtf:
        signals.append(mtf)
    return scoring.aggregate(signals)


def analyze_ticker(symbol: str, include_options: bool = True) -> dict:
    """Full multi-strategy technical analysis for a TradingView or plain symbol.

    Returns a dict with flag (BUY/SELL/HOLD), score, confidence, reasons,
    per-signal breakdown and basic price info. Pure free-tier data (yfinance).
    """
    try:
        yf_symbol = resolve_symbol(symbol)
    except DataSourceError as e:
        return {"symbol": symbol, "error": str(e)}
    if yf_symbol is None:
        return {"symbol": symbol, "error": f"Could not resolve '{symbol}' to a data source"}

    frames = fetch_frames(yf_symbol)
    daily = frames.get("1D")
    if daily is None or daily.empty:
        return {"symbol": symbol, "yf_symbol": yf_symbol, "error": "No price data available"}

    if include_options:
        opt = options_sentiment(yf_symbol)
    else:
        opt = None

    signals: List[Signal] = []
    for tf in ("1W", "1D", "4H", "1H"):
        signals.extend(_analyze_frame(tf, frames.get(tf)))
    mtf = _mtf_alignment(frames)
    if mtf:
        signals.append(mtf)
    if opt:
        signals.append(Signal("options_sentiment", "options", _DIR[opt["direction"]],
                              0.5, 0.6,
                              f"{opt['read']} (P/C OI {opt['put_call_oi_ratio']}"
                              + (f", ATM IV {opt['atm_iv_pct']}%" if opt.get("atm_iv_pct") else "") + ")",
                              opt))

    result = scoring.aggregate(signals)
    price = float(daily["Close"].iloc[-1])
    prev = float(daily["Close"].iloc[-2]) if len(daily) > 1 else price
    result.update({
        "symbol": symbol.upper(),
        "yf_symbol": yf_symbol,
        "price": round(price, 4),
        "change_pct": round((price / prev - 1) * 100, 2),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "disclaimer": "Educational analysis, not financial advice.",
    })
    return result
