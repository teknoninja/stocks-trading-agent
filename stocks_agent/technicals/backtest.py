"""Walk-forward backtester for the flag engine.

Replays the engine over historical daily+weekly data: every `step` trading
days it computes the flag using ONLY data up to that day, then measures what
price actually did 5/10/20 trading days later.

Honest limitations (stated, not hidden):
- Uses 1W + 1D frames only — intraday history (4H/1H) and options chains
  are not available historically on the free tier, so live analyses have a
  few more signals than backtested ones.
- Yahoo daily data is adjusted for splits/dividends.
"""

from typing import Dict, List, Optional

import pandas as pd

from .data import _clean, _history
from .engine import analyze_frames

HORIZONS = (5, 10, 20)
WARMUP = 250  # bars of history required before the first flag


def _weekly_from_daily(daily: pd.DataFrame) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return daily.resample("W-FRI").agg(agg).dropna(subset=["Close"])


def backtest_symbol(yf_symbol: str, years: str = "5y", step: int = 5) -> Optional[pd.DataFrame]:
    """Return one row per (sample date): flag, score, and forward returns."""
    daily = _clean(_history(yf_symbol, years, "1d"))
    if daily is None or len(daily) < WARMUP + max(HORIZONS) + step:
        return None

    rows: List[dict] = []
    closes = daily["Close"].values
    for i in range(WARMUP, len(daily) - max(HORIZONS), step):
        d_slice = daily.iloc[:i]
        frames = {"1D": d_slice, "1W": _weekly_from_daily(d_slice)}
        result = analyze_frames(frames)
        entry = float(closes[i - 1])
        row = {
            "symbol": yf_symbol,
            "date": daily.index[i - 1],
            "flag": result["flag"],
            "score": result["score"],
            "confidence": result["confidence"],
            "signals": result["signals"],  # kept for per-strategy attribution
        }
        for h in HORIZONS:
            row[f"ret_{h}d"] = (float(closes[i - 1 + h]) / entry - 1) * 100
        rows.append(row)
    return pd.DataFrame(rows)


def flag_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Hit-rate and average forward return per flag type vs the buy&hold baseline."""
    out = []
    for h in HORIZONS:
        col = f"ret_{h}d"
        baseline = df[col].mean()
        for flag in ("BUY", "SELL", "HOLD"):
            sub = df[df["flag"] == flag]
            if sub.empty:
                continue
            rets = sub[col]
            if flag == "BUY":
                hit = (rets > 0).mean() * 100
                edge = rets.mean() - baseline
            elif flag == "SELL":
                hit = (rets < 0).mean() * 100
                edge = baseline - rets.mean()  # a good SELL means below-average return
            else:
                hit, edge = None, None
            out.append({
                "flag": flag, "horizon": f"{h}d", "n": len(sub),
                "avg_return_pct": round(rets.mean(), 2),
                "baseline_pct": round(baseline, 2),
                "hit_rate_pct": round(hit, 1) if hit is not None else None,
                "edge_vs_baseline_pct": round(edge, 2) if edge is not None else None,
            })
    return pd.DataFrame(out)


def strategy_stats(df: pd.DataFrame, horizon: int = 10) -> pd.DataFrame:
    """Per-strategy edge: when strategy X voted bullish/bearish, what happened?

    Edge > 0 means the strategy's direction pointed the right way more than
    the baseline drift — evidence it deserves its weight.
    """
    col = f"ret_{horizon}d"
    baseline = df[col].mean()
    buckets: Dict[str, List[float]] = {}
    for _, row in df.iterrows():
        for sig in row["signals"]:
            if sig["direction"] == 0:
                continue
            # signed return: positive when the strategy's direction was right
            buckets.setdefault(sig["name"], []).append(sig["direction"] * row[col])
    out = []
    for name, vals in sorted(buckets.items()):
        s = pd.Series(vals)
        out.append({
            "strategy": name,
            "n_votes": len(s),
            "directional_hit_rate_pct": round((s > 0).mean() * 100, 1),
            "avg_signed_return_pct": round(s.mean(), 2),
            "baseline_drift_pct": round(baseline, 2),
        })
    return pd.DataFrame(out).sort_values("avg_signed_return_pct", ascending=False)


def run(symbols: List[str], years: str = "5y", step: int = 5) -> dict:
    """Backtest many symbols; returns combined stats + per-symbol frames."""
    all_frames = []
    skipped = []
    for sym in symbols:
        df = backtest_symbol(sym, years=years, step=step)
        if df is None or df.empty:
            skipped.append(sym)
            continue
        all_frames.append(df)
    if not all_frames:
        return {"error": "No symbols produced backtest data", "skipped": skipped}
    combined = pd.concat(all_frames, ignore_index=True)
    return {
        "samples": len(combined),
        "symbols_tested": [f["symbol"].iloc[0] for f in all_frames],
        "skipped": skipped,
        "flag_stats": flag_stats(combined),
        "strategy_stats": strategy_stats(combined),
        "raw": combined,
    }
