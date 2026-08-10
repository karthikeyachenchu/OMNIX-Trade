"""Position sizing (§35), the IST clock (§17), the wallet ledger (§43),
and the change_pct fix (§18 / defect D5).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from sentinel import clock
from sentinel.analysis import technicals as ta
from sentinel.execution.costs import Segment
from sentinel.execution.sizing import PositionSizer
from sentinel.wallet import Wallet
from tests.conftest import make_candles


# ── §35 position sizing ───────────────────────────────────────────────────
class TestPositionSizer:
    def test_size_respects_the_risk_budget(self, costs):
        s = PositionSizer(costs)
        r = s.size(equity=100_000, risk_pct=1.0, entry=100.0, stop_loss=75.0,
                   lot_size=65, segment=Segment.OPT, side="BUY",
                   available_capital=1_000_000)
        # One lot risks 65 x (25 + slippage + costs) which is well over ₹1,000.
        assert not r.ok
        assert "exceeds" in r.rejected_reason

    def test_a_tight_stop_allows_a_position(self, costs):
        s = PositionSizer(costs)
        r = s.size(equity=1_000_000, risk_pct=1.0, entry=100.0, stop_loss=95.0,
                   lot_size=65, segment=Segment.OPT, side="BUY",
                   available_capital=10_000_000)
        assert r.ok
        assert r.qty % 65 == 0, "size must be a whole number of lots"
        assert r.max_loss <= r.risk_budget * 1.001

    def test_never_rounds_up_to_one_lot(self, costs):
        """If one lot busts the budget the answer is zero, not one."""
        s = PositionSizer(costs)
        r = s.size(equity=10_000, risk_pct=1.0, entry=500.0, stop_loss=400.0,
                   lot_size=100, segment=Segment.OPT, side="BUY",
                   available_capital=10_000_000)
        assert r.qty == 0

    def test_capital_clamps_the_size(self, costs):
        s = PositionSizer(costs)
        rich = s.size(equity=10_000_000, risk_pct=1.0, entry=100.0, stop_loss=99.0,
                      lot_size=65, segment=Segment.OPT, side="BUY",
                      available_capital=10_000_000)
        poor = s.size(equity=10_000_000, risk_pct=1.0, entry=100.0, stop_loss=99.0,
                      lot_size=65, segment=Segment.OPT, side="BUY",
                      available_capital=13_000)   # 2 lots' worth
        assert poor.ok and poor.lots <= 2 and poor.lots < rich.lots

    def test_zero_stop_distance_is_rejected(self, costs):
        s = PositionSizer(costs)
        r = s.size(equity=100_000, risk_pct=1.0, entry=100.0, stop_loss=100.0,
                   lot_size=1, segment=Segment.OPT, side="BUY")
        assert not r.ok and "undefined risk" in r.rejected_reason

    def test_costs_make_the_size_smaller_not_larger(self, costs, zero_costs):
        free = PositionSizer(zero_costs, slippage_pct=0.0)
        real = PositionSizer(costs, slippage_pct=0.001)
        kw = dict(equity=1_000_000, risk_pct=1.0, entry=100.0, stop_loss=95.0,
                  lot_size=65, segment=Segment.OPT, side="BUY",
                  available_capital=10_000_000)
        assert real.size(**kw).qty <= free.size(**kw).qty

    def test_the_old_cash_times_098_rule_is_gone(self, costs):
        """Defect D1: sizing used `int(cash * 0.98 / (prem * lot))`, which with
        a 25% stop risked ~24.5% of the wallet on one trade."""
        s = PositionSizer(costs)
        cash = 100_000
        prem, lot = 100.0, 65
        old_lots = int(cash * 0.98 / (prem * lot))          # = 15 lots
        r = s.size(equity=cash, risk_pct=1.0, entry=prem, stop_loss=prem * 0.75,
                   lot_size=lot, segment=Segment.OPT, side="BUY",
                   available_capital=cash)
        assert r.lots < old_lots, "still sizing by cash rather than by risk"


# ── §17 clock ─────────────────────────────────────────────────────────────
class TestClock:
    def test_now_is_always_timezone_aware(self):
        assert clock.now().tzinfo is not None

    def test_in_session_boundaries(self, frozen_clock):
        for hhmm, expected in [((9, 14), False), ((9, 15), True), ((12, 0), True),
                               ((15, 30), True), ((15, 31), False)]:
            frozen_clock.set(datetime(2026, 8, 10, *hhmm, tzinfo=clock.IST))
            assert clock.in_session() is expected, f"{hhmm} should be {expected}"

    def test_weekend_is_never_in_session(self, frozen_clock):
        frozen_clock.set(datetime(2026, 8, 9, 11, 0, tzinfo=clock.IST))  # Sunday
        assert not clock.in_session()

    def test_trading_day_is_ist_not_machine_local(self, frozen_clock):
        """23:00 UTC on the 10th is already the 11th in IST (defect D8)."""
        frozen_clock.set(datetime(2026, 8, 11, 0, 30, tzinfo=clock.IST))
        assert clock.trading_day() == "2026-08-11"

    def test_parse_hhmm_rejects_nonsense(self):
        with pytest.raises(ValueError):
            clock.parse_hhmm("25:00")


# ── §43 wallet ledger conservation ────────────────────────────────────────
class TestWalletLedger:
    def test_ledger_conserves_money(self, tmp_path):
        w = Wallet(path=tmp_path / "w.db")
        w.activate(10_000)
        w.on_entry(3_000, "OPT", "buy")
        w.on_exit(3_500, 500, "OPT", "sell")
        st = w.state()
        # opening + deposits - withdrawals + realized = closing cash (nothing open)
        assert st["cash"] == pytest.approx(10_500)
        assert st["realized_pnl"] == pytest.approx(500)

    def test_on_entry_refuses_to_overspend(self, tmp_path):
        w = Wallet(path=tmp_path / "w.db")
        w.activate(1_000)
        assert w.on_entry(500, "A", "ok") is True
        assert w.on_entry(5_000, "B", "too much") is False
        assert w.state()["cash"] == pytest.approx(500)

    def test_stop_persists_the_zeroed_cash(self, tmp_path):
        """Defect D7: stop() zeroed cash in memory but never wrote it back."""
        import sqlite3
        path = tmp_path / "w.db"
        w = Wallet(path=path)
        w.activate(2_000)
        w.stop()
        with sqlite3.connect(str(path)) as c:
            cash = c.execute("SELECT cash FROM wallet ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert cash == pytest.approx(0.0), (
            f"stopped wallet still shows ₹{cash} on disk — the row is a lie")

    def test_ledger_balance_after_matches_state(self, tmp_path):
        w = Wallet(path=tmp_path / "w.db")
        w.activate(5_000)
        w.on_entry(2_000, "X", "buy")
        latest = w.ledger(1)[0]
        assert latest["balance_after"] == pytest.approx(w.state()["cash"])


# ── §18 / D5 change_pct ───────────────────────────────────────────────────
class TestChangePct:
    def test_session_change_uses_today_not_the_whole_lookback(self):
        """D5: change_pct was close[-1]/close[0]-1 over the FULL 5-day window
        while being labelled as today's move."""
        import pandas as pd

        day1 = make_candles(n=80, start=100.0, trend=0.0, seed=1)
        # Second session starts a day later at a very different level.
        day2 = make_candles(n=80, start=200.0, trend=0.05, seed=2)
        day2.index = day2.index + pd.Timedelta(days=1)
        df = pd.concat([day1, day2])

        s = ta.snapshot(df)
        day_open = float(day2["close"].iloc[0])
        expected_session = (s["ltp"] / day_open - 1) * 100

        assert s["session_change_pct"] == pytest.approx(expected_session, abs=0.02)
        assert s["change_pct"] == pytest.approx(s["session_change_pct"])
        # The lookback figure is still available, just no longer mislabelled.
        assert s["lookback_change_pct"] != pytest.approx(s["session_change_pct"])

    def test_day_open_high_low_are_session_scoped(self):
        df = make_candles(n=100)
        s = ta.snapshot(df)
        assert s["day_low"] <= s["ltp"] <= s["day_high"]
        assert s["day_low"] <= s["day_open"] <= s["day_high"]
