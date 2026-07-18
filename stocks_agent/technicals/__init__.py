"""Technical analysis engine: strategy modules + signal aggregation.

Every strategy module produces `Signal` objects (direction -1/0/+1, strength 0..1).
`engine.analyze_ticker()` runs everything across timeframes and aggregates them
into a single BUY / SELL / HOLD flag with confidence and human-readable reasons.
"""

from .engine import analyze_ticker
from .scoring import Signal

__all__ = ["analyze_ticker", "Signal"]
