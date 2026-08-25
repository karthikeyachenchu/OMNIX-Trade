"""Expired option contracts must retire themselves.

A contract past its expiry has no quote, so stop-loss and target can never
fire: without this the trade stays OPEN forever, shows null P&L in every
snapshot, and makes the feed request a dead symbol on every scan.
"""

from datetime import date, timedelta

import pytest

from sentinel.tracker import TradeTracker, option_expiry


@pytest.mark.parametrize("symbol,expected", [
    # weekly: YY + M(1-9,O,N,D) + DD
    ("BSE:SENSEX2671678000CE", date(2026, 7, 16)),
    ("NSE:NIFTY26O0724000PE", date(2026, 10, 7)),
    ("NSE:BANKNIFTY26D3157000CE", date(2026, 12, 31)),
    ("NSE:NIFTY26N1224000CE", date(2026, 11, 12)),
    # monthly: YY + MMM -> last day of that month (never premature)
    ("NSE:NIFTY26JUL24000CE", date(2026, 7, 31)),
    ("NSE:NIFTY26FEB24000PE", date(2026, 2, 28)),
])
def test_parses_expiry(symbol, expected):
    assert option_expiry(symbol) == expected


@pytest.mark.parametrize("symbol", [
    "NSE:RELIANCE-EQ", "NSE:NIFTY50-INDEX", "BSE:SENSEX-INDEX", "garbage",
])
def test_non_options_have_no_expiry(symbol):
    assert option_expiry(symbol) is None


def test_invalid_date_does_not_raise():
    # month code 2, day 31 — no such date. Must return None, not ValueError.
    assert option_expiry("NSE:NIFTY2623124000CE") is None


def test_expired_option_is_retired(tmp_path):
    tr = TradeTracker(path=tmp_path / "t.db")
    tr.add("BSE:SENSEX2671678000CE", "SENSEX 78000 CE", "BUY", 20, 108.35, 70.0, 151.7)
    assert len(tr.open_trades()) == 1

    retired = tr.expire_stale_options()
    assert len(retired) == 1
    assert retired[0].status == "EXPIRED"
    # P&L is NOT invented: settlement depends on the underlying's close on
    # expiry day, which this process cannot know.
    assert retired[0].pnl is None
    assert retired[0].exit_price is None
    assert tr.open_trades() == []


def test_live_contract_is_left_alone(tmp_path):
    tr = TradeTracker(path=tmp_path / "t2.db")
    future = date.today() + timedelta(days=30)
    code = {10: "O", 11: "N", 12: "D"}.get(future.month, str(future.month))
    sym = f"NSE:NIFTY{future.year % 100:02d}{code}{future.day:02d}24000CE"
    tr.add(sym, "NIFTY 24000 CE", "BUY", 75, 100.0, 60.0, 160.0)

    assert tr.expire_stale_options() == []
    assert len(tr.open_trades()) == 1


def test_equity_positions_never_expire(tmp_path):
    tr = TradeTracker(path=tmp_path / "t3.db")
    tr.add("NSE:RELIANCE-EQ", "RELIANCE", "BUY", 10, 1400.0, 1350.0, 1500.0)
    assert tr.expire_stale_options() == []
    assert len(tr.open_trades()) == 1
