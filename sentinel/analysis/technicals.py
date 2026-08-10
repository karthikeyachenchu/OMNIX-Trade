"""Technical indicators — pure pandas/numpy, no TA-Lib dependency."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, 12) - ema(close, 26)
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP reset per trading day (index must be tz-aware IST)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = tp * df["volume"]
    day = df.index.date
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0, np.nan)
    return (cum_pv / cum_v).ffill()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns +1 (uptrend) / -1 (downtrend) per bar."""
    _atr = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + mult * _atr
    lower = hl2 - mult * _atr
    close = df["close"].values
    ub, lb = upper.values.copy(), lower.values.copy()
    trend = np.ones(len(df), dtype=int)
    for i in range(1, len(df)):
        ub[i] = min(ub[i], ub[i - 1]) if close[i - 1] <= ub[i - 1] else ub[i]
        lb[i] = max(lb[i], lb[i - 1]) if close[i - 1] >= lb[i - 1] else lb[i]
        if trend[i - 1] == 1:
            trend[i] = -1 if close[i] < lb[i] else 1
        else:
            trend[i] = 1 if close[i] > ub[i] else -1
    return pd.Series(trend, index=df.index)


def snapshot(df: pd.DataFrame) -> dict:
    """Latest values of every indicator — the raw material for strategies & the LLM."""
    close = df["close"]
    macd_line, macd_sig, macd_hist = macd(close)
    bb_u, bb_m, bb_l = bollinger(close)
    st = supertrend(df)
    vwap = session_vwap(df)
    _atr = atr(df)
    last = -1
    return {
        "ltp": round(float(close.iloc[last]), 2),
        "change_pct": round(float(close.iloc[last] / close.iloc[0] - 1) * 100, 2),
        "ema9": round(float(ema(close, 9).iloc[last]), 2),
        "ema21": round(float(ema(close, 21).iloc[last]), 2),
        "ema50": round(float(ema(close, 50).iloc[last]), 2),
        "rsi14": round(float(rsi(close).iloc[last]), 1),
        "macd_hist": round(float(macd_hist.iloc[last]), 3),
        "macd_hist_prev": round(float(macd_hist.iloc[last - 1]), 3) if len(df) > 1 else 0.0,
        "atr14": round(float(_atr.iloc[last]), 2),
        "vwap": round(float(vwap.iloc[last]), 2),
        "bb_upper": round(float(bb_u.iloc[last]), 2) if not np.isnan(bb_u.iloc[last]) else None,
        "bb_lower": round(float(bb_l.iloc[last]), 2) if not np.isnan(bb_l.iloc[last]) else None,
        "bb_width_pct": round(float((bb_u.iloc[last] - bb_l.iloc[last]) / bb_m.iloc[last]) * 100, 2)
                        if not np.isnan(bb_m.iloc[last]) and bb_m.iloc[last] else None,
        "supertrend": int(st.iloc[last]),
        "supertrend_prev": int(st.iloc[last - 1]) if len(df) > 1 else 0,
        "day_high": round(float(df["high"][df.index.date == df.index[-1].date()].max()), 2),
        "day_low": round(float(df["low"][df.index.date == df.index[-1].date()].min()), 2),
    }
