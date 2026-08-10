"""RiskEngine — every rejection branch, the kill switch, and persistence.

This is the authority over money, so it gets the densest coverage in the repo.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from sentinel import clock
from sentinel.execution.intent import OrderIntent, Side
from sentinel.guardrails import RiskEngine


def intent(**kw):
    base = dict(symbol="NSE:NIFTY50-INDEX", side=Side.BUY, qty=10,
                entry=100.0, stop_loss=90.0, target=125.0)
    base.update(kw)
    return OrderIntent(**base)


class TestApprovalGate:
    def test_compliant_order_is_approved(self, risk):
        d = risk.approve(intent())
        assert d.approved, d.reason
        assert d.checks["risk_per_trade"].startswith("ok")

    def test_kill_switch_blocks(self, risk):
        risk.trip_kill_switch("daily drawdown")
        d = risk.approve(intent())
        assert not d.approved and "KILL SWITCH" in d.reason

    def test_market_closed_blocks(self, risk, frozen_clock):
        frozen_clock.set(datetime(2026, 8, 10, 8, 0, tzinfo=clock.IST))
        assert not risk.approve(intent()).approved

    def test_weekend_blocks(self, risk, frozen_clock):
        frozen_clock.set(datetime(2026, 8, 8, 11, 0, tzinfo=clock.IST))  # Saturday
        d = risk.approve(intent())
        assert not d.approved and "closed" in d.reason.lower()

    def test_no_entry_after_cutoff(self, risk, frozen_clock):
        frozen_clock.set(datetime(2026, 8, 10, 14, 45, tzinfo=clock.IST))
        d = risk.approve(intent())
        assert not d.approved and "No new entries" in d.reason

    def test_trade_cap(self, risk):
        for _ in range(risk.limits.max_trades_per_day):
            risk.on_entry()
        d = risk.approve(intent())
        assert not d.approved and "trade cap" in d.reason.lower()

    def test_position_cap(self, risk):
        risk.set_open_positions(risk.limits.max_open_positions)
        d = risk.approve(intent())
        assert not d.approved and "open positions" in d.reason.lower()

    def test_risk_per_trade_cap(self, risk):
        # 10 units x ₹200 stop distance = ₹2,000 > ₹1,000 cap
        d = risk.approve(intent(entry=300.0, stop_loss=100.0, target=800.0))
        assert not d.approved and "exceeds per-trade cap" in d.reason

    def test_reward_risk_floor(self, risk):
        # R:R of 1.0, below the 1.5 minimum
        d = risk.approve(intent(entry=100.0, stop_loss=90.0, target=110.0))
        assert not d.approved and "reward:risk" in d.reason

    def test_daily_budget_shrinks_after_losses(self, risk):
        """A trade may not risk more than the REMAINING daily budget.

        After losing ₹1,600 of the ₹2,000 daily budget, only ₹400 is left. An
        order risking ₹500 is inside the ₹1,000 per-trade cap but would breach
        the day, so it must be refused — otherwise the kill switch only ever
        fires AFTER the limit is already blown.
        """
        risk.on_exit(-1600.0)
        d = risk.approve(intent(qty=10, entry=100.0, stop_loss=50.0, target=200.0))
        assert d.risk_amount == pytest.approx(500.0)
        assert not d.approved
        assert "remaining daily loss budget" in d.reason

    def test_cooldown_after_consecutive_losses(self, risk):
        risk.on_exit(-100.0)
        risk.on_exit(-100.0)           # consecutive_losses_pause = 2
        d = risk.approve(intent())
        assert not d.approved and "Cooldown" in d.reason

    def test_a_win_resets_the_loss_streak(self, risk):
        risk.on_exit(-100.0)
        risk.on_exit(+50.0)
        assert risk.state.consecutive_losses == 0
        assert risk.approve(intent()).approved

    def test_reconciling_blocks_new_entries(self, risk):
        risk.enter_reconciling("broker says +65, we say 0")
        d = risk.approve(intent())
        assert not d.approved and "RECONCILING" in d.reason
        risk.clear_reconciling()
        assert risk.approve(intent()).approved

    def test_exits_are_always_approved(self, risk):
        """Never refuse to close a position (§5)."""
        risk.trip_kill_switch("anything")
        risk.enter_reconciling("anything")
        d = risk.approve(OrderIntent(symbol="X", side=Side.SELL, qty=1, entry=10.0,
                                     stop_loss=0.0, is_exit=True))
        assert d.approved


class TestKillSwitch:
    def test_trips_at_daily_drawdown(self, risk):
        assert not risk.state.kill_switch
        risk.on_exit(-2000.0)           # exactly 2% of ₹100,000
        assert risk.state.kill_switch
        assert "Daily drawdown" in risk.state.kill_reason

    def test_does_not_trip_below_the_limit(self, risk):
        risk.on_exit(-1999.0)
        assert not risk.state.kill_switch

    def test_reset_requires_exact_confirmation(self, risk):
        risk.trip_kill_switch("test")
        ok, _ = risk.reset_kill_switch("yes")
        assert not ok and risk.state.kill_switch
        ok, _ = risk.reset_kill_switch("RESET")
        assert ok and not risk.state.kill_switch

    def test_cannot_be_cleared_by_automation(self, risk):
        """There is deliberately no automatic path that clears it."""
        risk.trip_kill_switch("test")
        risk.on_exit(+10_000.0)         # a big win must NOT resume trading
        assert risk.state.kill_switch


class TestPersistence:
    """D20: risk state used to die with the process."""

    def test_kill_switch_survives_restart(self, limits, tmp_path):
        f = tmp_path / "risk.json"
        r1 = RiskEngine(limits, 100_000.0, state_file=f)
        r1.on_exit(-2500.0)
        assert r1.state.kill_switch

        r2 = RiskEngine(limits, 100_000.0, state_file=f)      # "restart"
        assert r2.state.kill_switch, "kill switch was lost across a restart"
        assert r2.state.daily_pnl == pytest.approx(-2500.0)

    def test_trade_count_survives_restart(self, limits, tmp_path):
        f = tmp_path / "risk.json"
        r1 = RiskEngine(limits, 100_000.0, state_file=f)
        for _ in range(4):
            r1.on_entry()
        r2 = RiskEngine(limits, 100_000.0, state_file=f)
        assert r2.state.trades_today == 4

    def test_new_trading_day_resets_state(self, limits, tmp_path, frozen_clock):
        f = tmp_path / "risk.json"
        r1 = RiskEngine(limits, 100_000.0, state_file=f)
        r1.on_exit(-2500.0)
        assert r1.state.kill_switch

        frozen_clock.set(datetime(2026, 8, 11, 9, 30, tzinfo=clock.IST))
        r2 = RiskEngine(limits, 100_000.0, state_file=f)
        assert not r2.state.kill_switch, "yesterday's kill switch blocked a new day"
        assert r2.state.daily_pnl == 0.0
        assert r2.state.trading_day == "2026-08-11"


class TestInvariants:
    """Property-style invariants (§66)."""

    def test_open_positions_never_negative(self, risk):
        for _ in range(5):
            risk.on_exit(0.0)
        assert risk.state.open_positions >= 0

    def test_no_entry_approved_while_over_daily_loss(self, risk):
        risk.on_exit(-2100.0)
        assert risk.state.kill_switch
        assert not risk.approve(intent()).approved

    @pytest.mark.parametrize("pnl", [-5000, -2001, -2000])
    def test_any_breach_trips_the_switch(self, risk, pnl):
        risk.on_exit(float(pnl))
        assert risk.state.kill_switch
