"""Strategy engine — each strategy votes; votes combine into a composite signal.

Directions: +1 long bias, -1 short bias, 0 neutral. Confidence 0..1.
The composite drives alerts; the LLM then writes the human-readable plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import pandas as pd

from . import technicals as ta


@dataclass
class Vote:
    strategy: str
    direction: int
    confidence: float
    reason: str


@dataclass
class Composite:
    symbol: str
    name: str
    direction: int            # +1 / -1 / 0
    score: float              # 0..1 strength of conviction
    votes: list[Vote] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)   # entry / stop / target from ATR

    def label(self) -> str:
        if self.direction > 0:
            return "BULLISH"
        if self.direction < 0:
            return "BEARISH"
        return "NEUTRAL"


# ── individual strategies ──────────────────────────────────────────────────

def ema_crossover(df: pd.DataFrame, s: dict) -> Vote:
    fast, slow = s["ema9"], s["ema21"]
    spread = (fast - slow) / s["ltp"] * 100
    if fast > slow and s["ltp"] > s["ema50"]:
        return Vote("EMA trend", 1, min(1.0, abs(spread) * 4), f"9EMA>21EMA, price above 50EMA (spread {spread:.2f}%)")
    if fast < slow and s["ltp"] < s["ema50"]:
        return Vote("EMA trend", -1, min(1.0, abs(spread) * 4), f"9EMA<21EMA, price below 50EMA (spread {spread:.2f}%)")
    return Vote("EMA trend", 0, 0.3, "EMAs mixed — no clean trend")


def supertrend_strategy(df: pd.DataFrame, s: dict) -> Vote:
    st, prev = s["supertrend"], s["supertrend_prev"]
    if st == 1 and prev == -1:
        return Vote("Supertrend", 1, 0.9, "Supertrend flipped UP this bar (fresh signal)")
    if st == -1 and prev == 1:
        return Vote("Supertrend", -1, 0.9, "Supertrend flipped DOWN this bar (fresh signal)")
    return Vote("Supertrend", st, 0.5, f"Supertrend {'up' if st == 1 else 'down'} (continuing)")


def vwap_strategy(df: pd.DataFrame, s: dict) -> Vote:
    dist = (s["ltp"] - s["vwap"]) / s["vwap"] * 100
    if dist > 0.15:
        return Vote("VWAP", 1, min(1.0, dist / 0.6), f"Price {dist:.2f}% above VWAP — buyers in control")
    if dist < -0.15:
        return Vote("VWAP", -1, min(1.0, -dist / 0.6), f"Price {abs(dist):.2f}% below VWAP — sellers in control")
    return Vote("VWAP", 0, 0.4, "Hugging VWAP — indecision")


def rsi_strategy(df: pd.DataFrame, s: dict) -> Vote:
    r = s["rsi14"]
    if r >= 70:
        return Vote("RSI", -1, min(1.0, (r - 70) / 15 + 0.4), f"RSI {r:.0f} overbought — long entries risky")
    if r <= 30:
        return Vote("RSI", 1, min(1.0, (30 - r) / 15 + 0.4), f"RSI {r:.0f} oversold — bounce candidate")
    if r > 55:
        return Vote("RSI", 1, 0.3, f"RSI {r:.0f} — bullish momentum zone")
    if r < 45:
        return Vote("RSI", -1, 0.3, f"RSI {r:.0f} — bearish momentum zone")
    return Vote("RSI", 0, 0.2, f"RSI {r:.0f} neutral")


def macd_strategy(df: pd.DataFrame, s: dict) -> Vote:
    h, hp = s["macd_hist"], s["macd_hist_prev"]
    if h > 0 and hp <= 0:
        return Vote("MACD", 1, 0.8, "MACD histogram crossed positive (fresh)")
    if h < 0 and hp >= 0:
        return Vote("MACD", -1, 0.8, "MACD histogram crossed negative (fresh)")
    if h > 0:
        return Vote("MACD", 1, 0.4, "MACD histogram positive")
    if h < 0:
        return Vote("MACD", -1, 0.4, "MACD histogram negative")
    return Vote("MACD", 0, 0.2, "MACD flat")


def orb_strategy(df: pd.DataFrame, s: dict) -> Vote:
    """Opening range (first 15 min) breakout — only meaningful intraday."""
    today = df.index[-1].date()
    day = df[df.index.date == today]
    if day.empty:
        return Vote("ORB", 0, 0.0, "No intraday data")
    opening = day.between_time(time(9, 15), time(9, 30))
    if opening.empty or len(day) <= len(opening):
        return Vote("ORB", 0, 0.0, "Opening range still forming")
    hi, lo = opening["high"].max(), opening["low"].min()
    ltp = s["ltp"]
    if ltp > hi:
        return Vote("ORB", 1, 0.7, f"Above opening range high {hi:.1f}")
    if ltp < lo:
        return Vote("ORB", -1, 0.7, f"Below opening range low {lo:.1f}")
    return Vote("ORB", 0, 0.3, f"Inside opening range [{lo:.1f} – {hi:.1f}]")


def bollinger_strategy(df: pd.DataFrame, s: dict) -> Vote:
    if not s.get("bb_upper") or not s.get("bb_width_pct"):
        return Vote("Bollinger", 0, 0.0, "Insufficient data")
    squeeze = s["bb_width_pct"] < 1.0
    if s["ltp"] > s["bb_upper"]:
        note = " after squeeze — expansion likely" if squeeze else ""
        return Vote("Bollinger", 1, 0.6 + (0.2 if squeeze else 0), f"Close above upper band{note}")
    if s["ltp"] < s["bb_lower"]:
        note = " after squeeze — expansion likely" if squeeze else ""
        return Vote("Bollinger", -1, 0.6 + (0.2 if squeeze else 0), f"Close below lower band{note}")
    return Vote("Bollinger", 0, 0.2, "Inside bands")


STRATEGIES = [ema_crossover, supertrend_strategy, vwap_strategy, rsi_strategy, macd_strategy,
              orb_strategy, bollinger_strategy]

# Trend/momentum carry more weight than single-oscillator readings.
WEIGHTS = {"EMA trend": 1.2, "Supertrend": 1.2, "VWAP": 1.0, "RSI": 0.8, "MACD": 1.0,
           "ORB": 1.0, "Bollinger": 0.8}


def evaluate(symbol: str, name: str, df: pd.DataFrame, sentiment_bias: float = 0.0) -> Composite:
    """Run all strategies; blend with sentiment bias (-1..+1, weight 0.6)."""
    s = ta.snapshot(df)
    votes = [strat(df, s) for strat in STRATEGIES]

    weighted = sum(v.direction * v.confidence * WEIGHTS.get(v.strategy, 1.0) for v in votes)
    total_w = sum(WEIGHTS.get(v.strategy, 1.0) for v in votes)
    raw = weighted / total_w                       # -1..+1
    raw += 0.6 * sentiment_bias / total_w * len(votes) * 0.1  # gentle sentiment tilt

    direction = 1 if raw > 0.12 else (-1 if raw < -0.12 else 0)
    score = round(min(1.0, abs(raw) * 1.6), 3)

    # ATR-based trade plan (guardrails re-validate later; these are suggestions)
    a = s["atr14"]
    ltp = s["ltp"]
    plan = {}
    if direction != 0 and a > 0:
        sl = ltp - 1.5 * a if direction > 0 else ltp + 1.5 * a
        tgt = ltp + 2.5 * a if direction > 0 else ltp - 2.5 * a
        plan = {"entry": round(ltp, 2), "stop_loss": round(sl, 2), "target": round(tgt, 2),
                "atr": a, "reward_risk": round(2.5 / 1.5, 2)}

    return Composite(symbol=symbol, name=name, direction=direction, score=score,
                     votes=votes, snapshot=s, plan=plan)
