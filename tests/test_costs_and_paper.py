"""Transaction costs (§8) and paper-execution realism (§7).

The point of these tests is that paper P&L must be PESSIMISTIC. A paper
result that beats what live would have produced is a bug, not a feature.
"""

from __future__ import annotations

import pytest

from sentinel.broker.paper import PaperBroker
from sentinel.execution.costs import CostModel, Segment, segment_for_symbol
from sentinel.execution.engine import ExecutionEngine
from sentinel.execution.intent import OrderIntent, Side


class TestSegmentClassification:
    @pytest.mark.parametrize("symbol,expected", [
        ("NSE:NIFTY2580724500CE", Segment.OPT),
        ("BSE:SENSEX2671678000PE", Segment.OPT),
        ("NSE:NIFTY25AUGFUT", Segment.FUT),
        ("NSE:RELIANCE-EQ", Segment.EQUITY_INTRADAY),
    ])
    def test_classification(self, symbol, expected):
        assert segment_for_symbol(symbol) == expected

    def test_delivery_product_changes_segment(self):
        assert segment_for_symbol("NSE:RELIANCE-EQ", "CNC") == Segment.EQUITY_DELIVERY


class TestCostArithmetic:
    def test_option_buy_leg_is_itemised(self, costs):
        # 65 lots @ ₹100 premium = ₹6,500 turnover
        c = costs.leg(Segment.OPT, "BUY", 100.0, 65)
        assert c.brokerage == pytest.approx(20.0)
        assert c.stt == pytest.approx(0.0)              # no STT on option buys
        assert c.exchange_txn == pytest.approx(6500 * 0.000495)
        assert c.stamp_duty == pytest.approx(6500 * 0.00003)
        assert c.gst == pytest.approx((c.brokerage + c.exchange_txn + c.sebi) * 0.18)
        assert c.total > 20.0

    def test_option_sell_leg_pays_stt(self, costs):
        c = costs.leg(Segment.OPT, "SELL", 100.0, 65)
        assert c.stt == pytest.approx(6500 * 0.001)
        assert c.stamp_duty == 0.0                      # buy side only

    def test_round_trip_is_the_sum_of_both_legs(self, costs):
        rt = costs.round_trip(Segment.OPT, "BUY", 100.0, 130.0, 65)
        manual = (costs.leg(Segment.OPT, "BUY", 100.0, 65)
                  + costs.leg(Segment.OPT, "SELL", 130.0, 65))
        assert rt.total == pytest.approx(manual.total)

    def test_net_pnl_is_always_worse_than_gross(self, costs):
        gross, net, c = costs.net_pnl(Segment.OPT, "BUY", 100.0, 130.0, 65)
        assert gross == pytest.approx(1950.0)
        assert net < gross
        assert gross - net == pytest.approx(c.total)

    def test_a_losing_trade_loses_more_than_gross(self, costs):
        gross, net, _ = costs.net_pnl(Segment.OPT, "BUY", 100.0, 90.0, 65)
        assert gross == pytest.approx(-650.0)
        assert net < gross, "costs must deepen a loss, not soften it"

    def test_zero_quantity_is_free(self, costs):
        assert costs.leg(Segment.OPT, "BUY", 100.0, 0).total == 0.0

    def test_breakeven_move_is_positive(self, costs):
        assert costs.breakeven_move(Segment.OPT, "BUY", 100.0, 65) > 0

    def test_overrides_apply(self):
        m = CostModel({"options": {"brokerage_flat": 0.0, "stt_sell": 0.0}})
        c = m.leg(Segment.OPT, "SELL", 100.0, 65)
        assert c.brokerage == 0.0 and c.stt == 0.0

    def test_unknown_segment_is_rejected(self):
        with pytest.raises(ValueError, match="unknown cost segment"):
            CostModel({"crypto": {"brokerage_flat": 1.0}})

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValueError, match="unknown cost field"):
            CostModel({"options": {"made_up_fee": 1.0}})


class TestPaperRealism:
    """§7 — paper must model friction, not wish it away."""

    def _engine(self, risk, costs, **kw):
        broker = PaperBroker(risk, costs=costs, **kw)
        return broker, ExecutionEngine(broker=broker, risk_engine=risk)

    def _intent(self, **kw):
        base = dict(symbol="NSE:NIFTY2580724500CE", side=Side.BUY, qty=65,
                    entry=100.0, stop_loss=85.0, target=130.0, lot_size=65,
                    strategy_id="test")
        base.update(kw)
        return OrderIntent(**base)

    def test_buy_fills_above_the_asked_price(self, risk, costs):
        broker, ex = self._engine(risk, costs)
        result = ex.submit(self._intent())
        assert result.ok
        pos = broker.positions()[0]
        assert pos.entry > 100.0, "a BUY filled at or better than the ask — unrealistic"

    def test_sell_fills_below_the_asked_price(self, risk, costs):
        broker, ex = self._engine(risk, costs)
        ex.submit(self._intent())
        fill = ex.close("NSE:NIFTY2580724500CE", "MANUAL", 100.0)
        assert fill.price < 100.0

    def test_no_spread_no_slippage_fills_exactly(self, risk, zero_costs):
        broker, ex = self._engine(risk, zero_costs, spread_pct=0.0, slippage_pct=0.0)
        ex.submit(self._intent())
        assert broker.positions()[0].entry == pytest.approx(100.0)

    def test_gap_through_stop_fills_at_the_gap_not_the_stop(self, risk, zero_costs):
        """The single most important paper-vs-live difference on options."""
        broker, ex = self._engine(risk, zero_costs, spread_pct=0.0, slippage_pct=0.0)
        ex.submit(self._intent())

        # Stop is 85, but the market gaps straight to 60.
        fills = ex.mark_to_market({"NSE:NIFTY2580724500CE": 60.0})

        assert len(fills) == 1
        assert fills[0].reason == "STOP-LOSS"
        assert fills[0].price == pytest.approx(60.0), (
            "filled at the stop level during a gap — this is the optimism that "
            "makes paper results lie")
        assert fills[0].pnl == pytest.approx((60.0 - 100.0) * 65)

    def test_target_does_not_fill_better_than_observed(self, risk, zero_costs):
        broker, ex = self._engine(risk, zero_costs, spread_pct=0.0, slippage_pct=0.0)
        ex.submit(self._intent())
        fills = ex.mark_to_market({"NSE:NIFTY2580724500CE": 132.0})
        assert fills[0].price == pytest.approx(130.0), "took free upside past the target"

    def test_closing_fill_reports_net_and_gross(self, risk, costs):
        broker, ex = self._engine(risk, costs)
        ex.submit(self._intent())
        fill = ex.close("NSE:NIFTY2580724500CE", "TARGET", 130.0)
        assert fill.gross_pnl is not None and fill.pnl is not None
        assert fill.pnl < fill.gross_pnl
        assert fill.costs > 0

    def test_stop_loss_direction_for_shorts(self, risk, zero_costs):
        broker, ex = self._engine(risk, zero_costs, spread_pct=0.0, slippage_pct=0.0)
        ex.submit(self._intent(side=Side.SELL, entry=100.0, stop_loss=110.0,
                               target=70.0))
        fills = ex.mark_to_market({"NSE:NIFTY2580724500CE": 115.0})
        assert fills[0].reason == "STOP-LOSS"
        assert fills[0].pnl == pytest.approx((100.0 - 115.0) * 65)

    def test_no_averaging_into_an_existing_position(self, risk, costs):
        broker, ex = self._engine(risk, costs)
        assert ex.submit(self._intent()).ok
        second = ex.submit(self._intent(signal_id="different-signal"))
        assert not second.ok and "Already holding" in second.message

    def test_risk_engine_sees_the_net_pnl(self, risk, costs):
        """The kill switch must be fed NET P&L, not gross (defect D1/D2)."""
        broker, ex = self._engine(risk, costs)
        ex.submit(self._intent())
        fill = ex.close("NSE:NIFTY2580724500CE", "STOP-LOSS", 85.0)
        assert risk.state.daily_pnl == pytest.approx(fill.pnl)
        assert risk.state.daily_pnl < 0
