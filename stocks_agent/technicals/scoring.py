"""Aggregate strategy signals into a single BUY / SELL / HOLD flag."""

from dataclasses import dataclass, field, asdict
from typing import List


@dataclass
class Signal:
    name: str          # e.g. "market_structure"
    timeframe: str     # "1W" | "1D" | "4H" | "1H" | "options"
    direction: int     # -1 bearish, 0 neutral, +1 bullish
    strength: float    # 0..1
    weight: float      # relative importance of this strategy
    detail: str        # human-readable explanation
    data: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d["direction_label"] = {1: "bullish", 0: "neutral", -1: "bearish"}[self.direction]
        return d


# Flag thresholds on the weighted score in [-1, 1]
BUY_THRESHOLD = 0.22
SELL_THRESHOLD = -0.22


def aggregate(signals: List[Signal]) -> dict:
    """Weighted vote across all signals -> flag, score, confidence, reasons."""
    active = [s for s in signals if s.direction != 0]
    total_weight = sum(s.weight for s in signals) or 1.0
    score = sum(s.direction * s.strength * s.weight for s in signals) / total_weight

    if score >= BUY_THRESHOLD:
        flag = "BUY"
    elif score <= SELL_THRESHOLD:
        flag = "SELL"
    else:
        flag = "HOLD"

    # confidence: magnitude of score blended with directional agreement
    if active:
        bulls = sum(s.weight for s in active if s.direction > 0)
        bears = sum(s.weight for s in active if s.direction < 0)
        agreement = abs(bulls - bears) / (bulls + bears)
    else:
        agreement = 0.0
    confidence = round(min(1.0, 0.6 * min(1.0, abs(score) / 0.5) + 0.4 * agreement), 2)

    bullish = sorted((s for s in active if s.direction > 0),
                     key=lambda s: s.weight * s.strength, reverse=True)
    bearish = sorted((s for s in active if s.direction < 0),
                     key=lambda s: s.weight * s.strength, reverse=True)

    return {
        "flag": flag,
        "score": round(score, 3),
        "confidence": confidence,
        "bullish_reasons": [f"[{s.timeframe}] {s.detail}" for s in bullish[:6]],
        "bearish_reasons": [f"[{s.timeframe}] {s.detail}" for s in bearish[:6]],
        "signal_counts": {"bullish": len(bullish), "bearish": len(bearish),
                          "neutral": len(signals) - len(active)},
        "signals": [s.to_dict() for s in signals],
    }
