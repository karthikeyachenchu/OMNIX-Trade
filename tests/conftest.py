"""Shared fixtures. No test touches the network or the real journal.db."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sentinel import clock  # noqa: E402
from sentinel.config import RiskLimits, Settings, WatchItem  # noqa: E402
from sentinel.execution.costs import CostModel  # noqa: E402
from sentinel.guardrails import RiskEngine  # noqa: E402

# A Monday, mid-session, so market_open() is True by default in tests.
MARKET_TIME = datetime(2026, 8, 10, 11, 0, tzinfo=clock.IST)


@pytest.fixture(autouse=True)
def frozen_clock():
    """Every test runs at a deterministic IST timestamp."""
    fc = clock.FrozenClock(MARKET_TIME)
    clock.set_clock(fc)
    yield fc
    clock.reset_clock()


@pytest.fixture
def limits():
    return RiskLimits(
        max_daily_drawdown_pct=2.0,
        max_risk_per_trade_pct=1.0,
        max_open_positions=3,
        max_trades_per_day=6,
        consecutive_losses_pause=2,
        cooldown_minutes=45,
        no_entry_after="14:30",
        square_off_time="15:12",
        min_reward_risk=1.5,
    )


@pytest.fixture
def risk(limits, tmp_path):
    """A risk engine with capital 100,000 and state isolated to tmp_path."""
    return RiskEngine(limits, capital=100_000.0,
                      state_file=tmp_path / "risk_state.json")


@pytest.fixture
def costs():
    return CostModel()


@pytest.fixture
def zero_costs():
    """Cost model with everything switched off — for isolating P&L arithmetic."""
    return CostModel({
        seg: {"brokerage_flat": 0.0, "brokerage_pct": 0.0, "stt_buy": 0.0,
              "stt_sell": 0.0, "exchange_txn": 0.0, "sebi_turnover": 0.0,
              "stamp_duty_buy": 0.0, "gst_pct": 0.0}
        for seg in ("options", "futures", "equity_intraday", "equity_delivery")
    })


@pytest.fixture
def settings(limits, tmp_path):
    return Settings(
        mode="paper",
        capital=100_000.0,
        watchlist=[
            WatchItem(name="NIFTY 50", fyers="NSE:NIFTY50-INDEX", yf="^NSEI",
                      options=True, lot_size=65),
            WatchItem(name="SENSEX", fyers="BSE:SENSEX-INDEX", yf="^BSESN",
                      options=True, lot_size=20),
        ],
        auto_invest_focus=["NIFTY 50"],
        risk=limits,
        dash_host="127.0.0.1",
    )


def make_candles(n: int = 200, start: float = 100.0, trend: float = 0.0,
                 seed: int = 7, freq: str = "5min") -> pd.DataFrame:
    """Deterministic OHLCV frame with an IST index. Never random across runs."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(loc=trend, scale=0.3, size=n)
    close = np.maximum(1.0, start + np.cumsum(steps))
    high = close + np.abs(rng.normal(0.2, 0.1, n))
    low = close - np.abs(rng.normal(0.2, 0.1, n))
    open_ = np.concatenate([[start], close[:-1]])
    volume = rng.integers(1_000, 10_000, n).astype(float)
    idx = pd.date_range(start="2026-08-10 09:15", periods=n, freq=freq,
                        tz="Asia/Kolkata")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx)


@pytest.fixture
def candles():
    return make_candles()
