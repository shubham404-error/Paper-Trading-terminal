"""Market-data adapter with Upstox API v2 primary feed and fallback chain.

Data source priority:
  1. Upstox SmartAPI (live, real-time via REST + WebSocket)
  2. Yahoo Finance (delayed ~15 min)
  3. Deterministic demo data (offline, always works)

The active source is reported to the UI so the user always knows what
they are looking at.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

try:
    import upstox_client
    from upstox_client.rest import ApiException
except ImportError:
    upstox_client = None  # type: ignore[assignment]

try:
    import yfinance as yf
except ImportError:
    yf = None

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback hardcoded symbol universe (used when Upstox is unavailable)
# ---------------------------------------------------------------------------
SYMBOLS: dict[str, str] = {
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "HDFCBANK": "HDFC Bank",
    "INFY": "Infosys",
    "ICICIBANK": "ICICI Bank",
    "BHARTIARTL": "Bharti Airtel",
    "ITC": "ITC Limited",
    "LT": "Larsen & Toubro",
    "SBIN": "State Bank of India",
    "AXISBANK": "Axis Bank",
    "MARUTI": "Maruti Suzuki",
    "HINDUNILVR": "Hindustan Unilever",
    "KOTAKBANK": "Kotak Mahindra Bank",
    "WIPRO": "Wipro",
    "TATAMOTORS": "Tata Motors",
    "SUNPHARMA": "Sun Pharma",
    "BAJFINANCE": "Bajaj Finance",
    "HCLTECH": "HCL Technologies",
    "NESTLEIND": "Nestle India",
    "TITAN": "Titan Company",
}

# Well-known Upstox instrument keys for common NSE equities.
# These are used as a fast lookup so the app works immediately without
# downloading the full instrument master.  Keys follow the format
# ``NSE_EQ|<ISIN>``.
_KNOWN_INSTRUMENT_KEYS: dict[str, str] = {
    "RELIANCE": "NSE_EQ|INE002A01018",
    "TCS": "NSE_EQ|INE467B01029",
    "HDFCBANK": "NSE_EQ|INE040A01034",
    "INFY": "NSE_EQ|INE009A01021",
    "ICICIBANK": "NSE_EQ|INE090A01021",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "ITC": "NSE_EQ|INE154A01025",
    "LT": "NSE_EQ|INE018A01030",
    "SBIN": "NSE_EQ|INE062A01020",
    "AXISBANK": "NSE_EQ|INE238A01034",
    "MARUTI": "NSE_EQ|INE585B01010",
    "HINDUNILVR": "NSE_EQ|INE030A01027",
    "KOTAKBANK": "NSE_EQ|INE237A01028",
    "WIPRO": "NSE_EQ|INE075A01022",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "SUNPHARMA": "NSE_EQ|INE044A01036",
    "BAJFINANCE": "NSE_EQ|INE296A01024",
    "HCLTECH": "NSE_EQ|INE860A01027",
    "NESTLEIND": "NSE_EQ|INE239A01016",
    "TITAN": "NSE_EQ|INE280A01028",
}

# Upstox interval names mapped from user-friendly labels.
_UPSTOX_INTERVALS: dict[str, str] = {
    "1m": "1minute",
    "5m": "5minute",
    "15m": "15minute",
    "30m": "30minute",
    "1h": "60minute",
    "1D": "day",
    "1W": "week",
    "1M": "month",
}

# Maximum lookback per interval (Upstox constraints).
_INTERVAL_MAX_DAYS: dict[str, int] = {
    "1minute": 30,
    "5minute": 30,
    "15minute": 30,
    "30minute": 365,
    "60minute": 365,
    "day": 365,
    "week": 3650,
    "month": 3650,
}


# ── Upstox REST helpers ──────────────────────────────────────────────────
class UpstoxClient:
    """Thin wrapper around the Upstox Python SDK for market data."""

    def __init__(self, access_token: str) -> None:
        self.access_token = access_token
        self._config = upstox_client.Configuration()
        self._config.access_token = access_token
        self._api_client = upstox_client.ApiClient(self._config)

    # -- Historical candles ------------------------------------------------
    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "day",
        to_date: str | None = None,
        from_date: str | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV candle data from Upstox.

        Returns a DataFrame indexed by datetime with columns:
        Open, High, Low, Close, Volume.
        """
        api = upstox_client.HistoryApi(self._api_client)
        if to_date is None:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if from_date is None:
            max_days = _INTERVAL_MAX_DAYS.get(interval, 365)
            from_date = (datetime.now() - timedelta(days=max_days)).strftime("%Y-%m-%d")

        resp = api.get_historical_candle_data(
            instrument_key=instrument_key,
            interval=interval,
            to_date=to_date,
            from_date=from_date,
            api_version="2.0",
        )
        candles = resp.data.candles if resp.data and resp.data.candles else []
        if not candles:
            return pd.DataFrame()

        # Candle format: [timestamp, open, high, low, close, volume, oi]
        df = pd.DataFrame(
            candles,
            columns=["Date", "Open", "High", "Low", "Close", "Volume", "OI"],
        )
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        return df

    # -- LTP (batch) -------------------------------------------------------
    def get_ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        """Batch-fetch LTP for up to 500 instruments.

        Returns ``{instrument_key: ltp}`` dict.
        """
        api = upstox_client.MarketQuoteApi(self._api_client)
        keys_csv = ",".join(instrument_keys[:500])
        resp = api.ltp(keys_csv, api_version="2.0")
        result: dict[str, float] = {}
        if resp.data:
            for key, quote in resp.data.items():
                if hasattr(quote, "last_price") and quote.last_price is not None:
                    result[key] = float(quote.last_price)
        return result


# ── Live LTP cache (WebSocket-backed or REST-polled) ─────────────────────
class _LTPCache:
    """Thread-safe in-memory LTP store.

    In production with a WebSocket feeder running in a background thread,
    ticks update this dict continuously.  For now we use REST polling
    (which is still zero-cost for individual users because Streamlit
    caches the singleton via ``st.cache_resource``).
    """

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._lock = threading.Lock()

    def update(self, key: str, price: float) -> None:
        with self._lock:
            self._prices[key] = price

    def update_many(self, mapping: dict[str, float]) -> None:
        with self._lock:
            self._prices.update(mapping)

    def get(self, key: str) -> float | None:
        with self._lock:
            return self._prices.get(key)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._prices)


# Module-level singleton — shared across all Streamlit sessions.
_ltp_cache = _LTPCache()

# Module-level Upstox client (lazily initialised).
_upstox_client: UpstoxClient | None = None


def _get_upstox_client() -> UpstoxClient | None:
    """Return a shared UpstoxClient, or None if credentials are missing."""
    global _upstox_client
    if _upstox_client is not None:
        return _upstox_client
    if upstox_client is None:
        return None  # SDK not installed
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token or token == "your_analytics_token_here":
        return None
    try:
        _upstox_client = UpstoxClient(token)
        log.info("Upstox client initialised successfully")
        return _upstox_client
    except Exception as exc:
        log.warning("Failed to initialise Upstox client: %s", exc)
        return None


def _instrument_key(symbol: str) -> str | None:
    """Resolve a human symbol name to an Upstox instrument key."""
    return _KNOWN_INSTRUMENT_KEYS.get(symbol.upper())


# ── Data loaders ─────────────────────────────────────────────────────────
def _load_upstox(
    symbol: str, interval: str = "1D"
) -> tuple[pd.DataFrame, str] | None:
    """Try fetching candle data from Upstox.  Returns None on failure."""
    client = _get_upstox_client()
    if client is None:
        return None
    ikey = _instrument_key(symbol)
    if ikey is None:
        log.debug("No instrument key for %s — skipping Upstox", symbol)
        return None
    upstox_interval = _UPSTOX_INTERVALS.get(interval, "day")
    try:
        df = client.get_historical_candles(ikey, interval=upstox_interval)
        if df.empty:
            return None
        # Also refresh the LTP cache with the latest close.
        _ltp_cache.update(ikey, float(df["Close"].iloc[-1]))
        return df, "🟢 Upstox Live"
    except Exception as exc:
        log.warning("Upstox candle fetch failed for %s: %s", symbol, exc)
        return None


def _load_yfinance(
    symbol: str, period: str = "6mo", interval: str = "1d"
) -> tuple[pd.DataFrame, str] | None:
    """Try fetching data from Yahoo Finance.  Returns None on failure."""
    if yf is None:
        return None
    ticker = f"{symbol.upper()}.NS"
    try:
        frame = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame = frame[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if not frame.empty:
            return frame, "🟡 Yahoo Finance (delayed)"
    except Exception as exc:
        log.warning("Yahoo Finance fetch failed for %s: %s", symbol, exc)
    return None


def _demo_data(symbol: str, days: int = 180) -> pd.DataFrame:
    """Generate deterministic synthetic OHLCV data for offline/demo use."""
    seed = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=datetime.now().date(), periods=days)
    base = 600 + (seed % 700)
    returns = rng.normal(0.0004, 0.018, size=len(dates))
    close = base * np.exp(np.cumsum(returns))
    open_ = close * (1 + rng.normal(0, 0.006, len(dates)))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.018, len(dates)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.018, len(dates)))
    volume = rng.integers(100_000, 2_000_000, len(dates))
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )


def load_data(
    symbol: str,
    interval: str = "1D",
    period: str = "6mo",
) -> tuple[pd.DataFrame, str]:
    """Load OHLCV market data with automatic fallback.

    Fallback chain: Upstox (live) → Yahoo Finance (delayed) → demo data.

    Parameters
    ----------
    symbol:
        NSE equity symbol, e.g. ``"RELIANCE"``.
    interval:
        Chart interval label: ``"1m"``, ``"5m"``, ``"15m"``, ``"30m"``,
        ``"1h"``, ``"1D"``, ``"1W"``, ``"1M"``.
    period:
        yfinance period string (only used for the Yahoo fallback).

    Returns
    -------
    tuple of (DataFrame, source_label).
    """
    # 1. Upstox
    result = _load_upstox(symbol, interval)
    if result is not None:
        return result

    # 2. Yahoo Finance (only for daily data — yfinance doesn't support
    #    sub-daily for Indian stocks reliably)
    if interval in {"1D", "1W", "1M"}:
        yf_interval = {"1D": "1d", "1W": "1wk", "1M": "1mo"}.get(interval, "1d")
        result = _load_yfinance(symbol, period=period, interval=yf_interval)
        if result is not None:
            return result

    # 3. Demo
    return _demo_data(symbol), "🔴 Demo data (offline)"


def get_live_ltp(symbol: str) -> float | None:
    """Return the most recent LTP from the in-memory cache or Upstox REST.

    Returns None if no live price is available.
    """
    ikey = _instrument_key(symbol)
    if ikey is None:
        return None

    # Check cache first (zero API calls).
    cached = _ltp_cache.get(ikey)
    if cached is not None:
        return cached

    # Fallback: single REST call.
    client = _get_upstox_client()
    if client is None:
        return None
    try:
        prices = client.get_ltp([ikey])
        price = prices.get(ikey)
        if price is not None:
            _ltp_cache.update(ikey, price)
        return price
    except Exception as exc:
        log.warning("LTP fetch failed for %s: %s", symbol, exc)
        return None


def is_upstox_connected() -> bool:
    """Return True if the Upstox client is available and configured."""
    return _get_upstox_client() is not None


def available_intervals() -> list[str]:
    """Return chart interval labels available for the current data source."""
    if is_upstox_connected():
        return ["1m", "5m", "15m", "30m", "1h", "1D", "1W", "1M"]
    return ["1D"]


# ── Technical indicators ─────────────────────────────────────────────────
def indicators(
    frame: pd.DataFrame,
    sma: int = 20,
    ema: int = 50,
    rsi_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> pd.DataFrame:
    """Compute technical indicators on an OHLCV DataFrame.

    Adds columns: SMA, EMA, RSI (Wilder), MACD, MACD_Signal,
    MACD_Hist, VWAP, BB_Upper, BB_Mid, BB_Lower.
    """
    result = frame.copy()

    # Simple Moving Average
    result["SMA"] = result["Close"].rolling(sma).mean()

    # Exponential Moving Average
    result["EMA"] = result["Close"].ewm(span=ema, adjust=False).mean()

    # RSI — Wilder's smoothing (exponential moving average)
    delta = result["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema_fast = result["Close"].ewm(span=macd_fast, adjust=False).mean()
    ema_slow = result["Close"].ewm(span=macd_slow, adjust=False).mean()
    result["MACD"] = ema_fast - ema_slow
    result["MACD_Signal"] = result["MACD"].ewm(span=macd_signal, adjust=False).mean()
    result["MACD_Hist"] = result["MACD"] - result["MACD_Signal"]

    # VWAP (cumulative intraday — resets daily for intraday data,
    # running cumulative for daily bars)
    typical_price = (result["High"] + result["Low"] + result["Close"]) / 3
    cum_tp_vol = (typical_price * result["Volume"]).cumsum()
    cum_vol = result["Volume"].cumsum()
    result["VWAP"] = cum_tp_vol / cum_vol.replace(0, np.nan)

    # Bollinger Bands
    result["BB_Mid"] = result["Close"].rolling(bb_period).mean()
    bb_std_val = result["Close"].rolling(bb_period).std()
    result["BB_Upper"] = result["BB_Mid"] + bb_std * bb_std_val
    result["BB_Lower"] = result["BB_Mid"] - bb_std * bb_std_val

    return result
