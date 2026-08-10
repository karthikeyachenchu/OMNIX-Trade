"""Market data abstraction.

FyersFeed  — real-time, used automatically once you've logged in.
YFinanceFeed — delayed (~1-15 min) fallback so everything works before the
               Fyers key arrives and outside market hours.
"""

from __future__ import annotations

import logging

import pandas as pd

from ..config import Settings, WatchItem

log = logging.getLogger("sentinel.feed")


class YFinanceFeed:
    name = "yfinance (delayed)"

    def __init__(self, watchlist: list[WatchItem]):
        self._yf_map = {w.fyers: w.yf for w in watchlist}

    def candles(self, fyers_symbol: str, interval: str, days: int) -> pd.DataFrame | None:
        import yfinance as yf
        ticker = self._yf_map.get(fyers_symbol, fyers_symbol)
        try:
            df = yf.Ticker(ticker).history(period=f"{days}d", interval=interval)
        except Exception as e:
            log.warning("yfinance history failed for %s: %s", ticker, e)
            return None
        if df is None or df.empty:
            return None
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                "Close": "close", "Volume": "volume"})
        df.index.name = "ts"
        try:
            df.index = df.index.tz_convert("Asia/Kolkata")
        except (TypeError, AttributeError):
            pass
        return df[["open", "high", "low", "close", "volume"]]

    def ltp(self, fyers_symbols: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for sym in fyers_symbols:
            df = self.candles(sym, "1m", 1)
            if df is not None and not df.empty:
                out[sym] = float(df["close"].iloc[-1])
        return out

    def option_chain(self, fyers_symbol: str, strike_count: int = 5) -> dict:
        return {}  # yfinance has no NSE option chain — allocator falls back to index units

    # no brokerage account behind yfinance
    account_live = False
    def positions(self) -> list[dict]: return []
    def holdings(self) -> list[dict]: return []
    def funds(self) -> dict: return {}


class FyersFeed:
    name = "fyers (live)"

    def __init__(self, fyers_data):
        self._data = fyers_data

    def candles(self, fyers_symbol: str, interval: str, days: int) -> pd.DataFrame | None:
        try:
            return self._data.history(fyers_symbol, interval, days)
        except Exception as e:
            log.warning("fyers history failed for %s: %s", fyers_symbol, e)
            return None

    def ltp(self, fyers_symbols: list[str]) -> dict[str, float]:
        try:
            return self._data.ltp(fyers_symbols)
        except Exception as e:
            log.warning("fyers ltp failed: %s", e)
            return {}

    def option_chain(self, fyers_symbol: str, strike_count: int = 5) -> dict:
        try:
            return self._data.option_chain(fyers_symbol, strike_count)
        except Exception as e:
            log.warning("fyers option chain failed for %s: %s", fyers_symbol, e)
            return {}

    # ── the user's real brokerage account (read-only) ──────────────────────
    account_live = True

    def positions(self) -> list[dict]:
        try:
            return self._data.positions()
        except Exception as e:
            log.warning("fyers positions failed: %s", e)
            return []

    def holdings(self) -> list[dict]:
        try:
            return self._data.holdings()
        except Exception as e:
            log.warning("fyers holdings failed: %s", e)
            return []

    def funds(self) -> dict:
        try:
            return self._data.funds()
        except Exception as e:
            log.warning("fyers funds failed: %s", e)
            return {}


def build_feed(settings: Settings, fyers_session=None):
    """auto → Fyers when a valid daily token exists, else yfinance."""
    if settings.data_source in ("auto", "fyers") and fyers_session and fyers_session.is_ready():
        from ..broker.fyers import FyersData
        log.info("Data source: Fyers (live)")
        return FyersFeed(FyersData(fyers_session))
    if settings.data_source == "fyers":
        log.warning("data_source=fyers but no valid token — falling back to yfinance")
    log.info("Data source: yfinance (delayed)")
    return YFinanceFeed(settings.watchlist)
