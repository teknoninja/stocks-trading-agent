"""Free-tier market data access (yfinance) with multi-timeframe frames."""

from typing import Dict, Optional
import pandas as pd
import yfinance as yf

# Corporate proxies do TLS interception; trust the OS keychain so Python
# accepts the proxy's root cert (fixes bogus SSL/"rate limit" failures).
try:
    import truststore
    truststore.inject_into_ssl()
    _session = None  # default session now works through the proxy
except ImportError:
    # No truststore: fall back to a browser-impersonating curl_cffi session,
    # which also helps against Yahoo rate limiting on clean networks.
    try:
        from curl_cffi import requests as _cf_requests
        _session = _cf_requests.Session(impersonate="chrome")
    except Exception:
        _session = None


def _ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol, session=_session) if _session is not None else yf.Ticker(symbol)

# TradingView exchange prefix -> yfinance suffix
EXCHANGE_SUFFIX = {
    "NSE": ".NS", "BSE": ".BO", "LSE": ".L", "TSX": ".TO", "TSXV": ".V",
    "ASX": ".AX", "HKEX": ".HK", "SGX": ".SI", "FWB": ".F", "XETR": ".DE",
    "EURONEXT": ".PA", "MIL": ".MI", "BME": ".MC", "SIX": ".SW", "TSE": ".T",
    "KRX": ".KS", "TWSE": ".TW", "SZSE": ".SZ", "SSE": ".SS", "MYX": ".KL",
    "IDX": ".JK", "SET": ".BK", "BMV": ".MX", "B3": ".SA", "JSE": ".JO",
}
US_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS", "OTC", "CBOE"}
CRYPTO_EXCHANGES = {"BINANCE", "COINBASE", "KRAKEN", "BITSTAMP", "BYBIT", "OKX", "CRYPTO"}
QUOTE_CCYS = ("USDT", "USDC", "USD", "EUR", "BTC")


def tradingview_to_yfinance(symbol: str) -> str:
    """Map a TradingView symbol (possibly 'EXCHANGE:TICKER') to a yfinance symbol."""
    symbol = symbol.strip().upper()
    if ":" not in symbol:
        return symbol
    exchange, ticker = symbol.split(":", 1)
    if exchange in US_EXCHANGES:
        return ticker
    if exchange in CRYPTO_EXCHANGES:
        for q in QUOTE_CCYS:
            if ticker.endswith(q):
                base = ticker[: -len(q)]
                quote = "USD" if q in ("USDT", "USDC") else q
                return f"{base}-{quote}"
        return f"{ticker}-USD"
    if exchange in EXCHANGE_SUFFIX:
        return ticker + EXCHANGE_SUFFIX[exchange]
    if exchange == "FX" or exchange == "FX_IDC" or exchange == "OANDA":
        return f"{ticker}=X"
    return ticker


_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def _fetch_chart(symbol: str, range_: str, interval: str) -> pd.DataFrame:
    """Fetch OHLCV via Yahoo's public chart API with plain requests.

    More reliable than yfinance behind corporate TLS-intercepting proxies
    (yfinance's internal curl_cffi client can't use the OS trust store).
    """
    import requests

    r = requests.get(
        _CHART_URL.format(sym=symbol),
        params={"range": range_, "interval": interval, "includePrePost": "false"},
        headers=_HEADERS,
        timeout=15,
    )
    if r.status_code == 429:
        raise RuntimeError("rate limited")
    r.raise_for_status()
    result = (r.json().get("chart", {}).get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        return pd.DataFrame()
    quote = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume"),
        },
        index=pd.to_datetime(result["timestamp"], unit="s", utc=True),
    )
    tz = (result.get("meta") or {}).get("exchangeTimezoneName")
    if tz:
        try:
            df.index = df.index.tz_convert(tz)
        except Exception:
            pass
    return df.dropna(subset=["Close"])


def _history(symbol: str, range_: str, interval: str) -> pd.DataFrame:
    """Chart API first, yfinance fallback."""
    try:
        df = _fetch_chart(symbol, range_, interval)
        if not df.empty:
            return df
    except RuntimeError:
        raise
    except Exception:
        pass
    return _ticker(symbol).history(period=range_, interval=interval)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=str.capitalize)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    df = df[keep].dropna(subset=["Close"])
    return df


def _resample_4h(h1: pd.DataFrame) -> pd.DataFrame:
    if h1.empty:
        return h1
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return h1.resample("4h").agg(agg).dropna(subset=["Close"])


def fetch_frames(yf_symbol: str) -> Dict[str, pd.DataFrame]:
    """Fetch weekly / daily / 4H / 1H OHLCV frames.

    Intraday frames may be empty for symbols where yfinance has no intraday
    data — every strategy module must tolerate an empty frame.
    """
    frames: Dict[str, pd.DataFrame] = {}
    try:
        frames["1W"] = _clean(_history(yf_symbol, "5y", "1wk"))
    except Exception:
        frames["1W"] = pd.DataFrame()
    try:
        frames["1D"] = _clean(_history(yf_symbol, "2y", "1d"))
    except Exception:
        frames["1D"] = pd.DataFrame()
    try:
        h1 = _clean(_history(yf_symbol, "60d", "1h"))
    except Exception:
        h1 = pd.DataFrame()
    frames["1H"] = h1
    frames["4H"] = _resample_4h(h1)
    return frames


class DataSourceError(Exception):
    """Raised when the data source is unavailable (e.g. Yahoo rate limit)."""


def resolve_symbol(symbol: str) -> Optional[str]:
    """Resolve a TradingView-style symbol to a yfinance symbol that has data.

    Tries the direct mapping first, then common fallbacks (.NS, -USD).
    Returns None if nothing yields price data. Raises DataSourceError when
    Yahoo is rate-limiting (so callers don't report a bogus 'unknown symbol').
    """
    candidates = [tradingview_to_yfinance(symbol)]
    bare = symbol.split(":")[-1].strip().upper()
    for extra in (bare, bare + ".NS", bare + "-USD"):
        if extra not in candidates:
            candidates.append(extra)
    rate_limited = False
    for cand in candidates:
        try:
            df = _history(cand, "5d", "1d")
            if df is not None and not df.empty:
                return cand
        except Exception as e:
            if "rate limit" in str(e).lower() or type(e).__name__ == "YFRateLimitError":
                rate_limited = True
            continue
    if rate_limited:
        raise DataSourceError(
            "Yahoo Finance is rate-limiting this IP right now — wait a few minutes and retry.")
    return None
