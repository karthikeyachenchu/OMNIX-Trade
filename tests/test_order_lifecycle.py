"""OrderIntent validation (§51), the order state machine (§10), and
failure injection (§65).

UNKNOWN is the state that matters: a broker timeout must never be read as
"the order failed", because the order may be live at the exchange.
"""

from __future__ import annotations

import pytest

from sentinel.broker.paper import PaperBroker
from sentinel.execution.engine import ExecutionEngine
from sentinel.execution.intent import IDEMPOTENCY_BUCKET_SEC, OrderIntent, Side
from sentinel.execution.state import (
    DANGEROUS,
    OrderRecord,
    OrderStatus,
    OrderStore,
    can_transition,
)


def good(**kw):
    base = dict(symbol="NSE:NIFTY2580724500CE", side=Side.BUY, qty=65,
                entry=100.0, stop_loss=85.0, target=130.0, lot_size=65,
                strategy_id="test", signal_id="sig-1")
    base.update(kw)
    return OrderIntent(**base)


# ── §51 intent validation ─────────────────────────────────────────────────
class TestOrderIntentValidation:
    def test_valid_intent_builds(self):
        assert good().max_loss == pytest.approx(975.0)

    @pytest.mark.parametrize("kw,match", [
        (dict(qty=0), "qty must be positive"),
        (dict(qty=-5), "qty must be positive"),
        (dict(entry=0.0), "entry must be positive"),
        (dict(symbol=""), "symbol is required"),
        (dict(stop_loss=0.0), "positive stop-loss"),
        (dict(stop_loss=120.0), "BUY stop-loss must be below entry"),
        (dict(qty=30), "not a whole multiple of lot size"),
    ])
    def test_invalid_intents_are_rejected_at_construction(self, kw, match):
        with pytest.raises(ValueError, match=match):
            good(**kw)

    def test_sell_stop_must_be_above_entry(self):
        with pytest.raises(ValueError, match="SELL stop-loss must be above entry"):
            good(side=Side.SELL, stop_loss=85.0, target=60.0)

    def test_intent_is_immutable(self):
        i = good()
        with pytest.raises(Exception):
            i.qty = 130            # frozen dataclass

    def test_derived_risk_facts(self):
        i = good()
        assert i.risk_per_unit == pytest.approx(15.0)
        assert i.notional == pytest.approx(6500.0)
        assert i.reward_risk == pytest.approx(2.0)
        assert i.lots == 1

    def test_exit_intent_skips_entry_validation(self):
        e = OrderIntent(symbol="X", side=Side.SELL, qty=1, entry=10.0,
                        stop_loss=0.0, is_exit=True)
        assert e.is_exit


# ── §39 idempotency ───────────────────────────────────────────────────────
class TestIdempotencyKey:
    def test_same_logical_trade_collides(self):
        a, b = good(), good()
        assert a.idempotency_key == b.idempotency_key
        assert a.intent_id != b.intent_id, "ids must still be unique"

    def test_different_signal_does_not_collide(self):
        assert good().idempotency_key != good(signal_id="sig-2").idempotency_key

    def test_different_side_does_not_collide(self):
        other = good(side=Side.SELL, stop_loss=130.0, target=70.0)
        assert good().idempotency_key != other.idempotency_key

    def test_a_later_bucket_does_not_collide(self, frozen_clock):
        first = good()
        frozen_clock.advance(seconds=IDEMPOTENCY_BUCKET_SEC * 2)
        assert first.idempotency_key != good().idempotency_key


# ── §10 order state machine ───────────────────────────────────────────────
class TestOrderStateMachine:
    def test_happy_path(self):
        r = OrderRecord.from_intent(good())
        for s in (OrderStatus.VALIDATED, OrderStatus.SUBMITTED,
                  OrderStatus.ACKNOWLEDGED, OrderStatus.FILLED):
            r.status = s
        assert r.status is OrderStatus.FILLED
        assert r.is_terminal
        assert len(r.history) == 4

    def test_illegal_transition_raises(self):
        r = OrderRecord.from_intent(good())
        with pytest.raises(ValueError, match="illegal order transition"):
            r.status = OrderStatus.FILLED       # CREATED -> FILLED is not legal

    def test_terminal_states_are_final(self):
        r = OrderRecord.from_intent(good())
        r.status = OrderStatus.VALIDATED
        r.status = OrderStatus.REJECTED
        with pytest.raises(ValueError):
            r.status = OrderStatus.SUBMITTED

    def test_unknown_is_dangerous(self):
        r = OrderRecord.from_intent(good())
        r.status = OrderStatus.VALIDATED
        r.status = OrderStatus.SUBMITTED
        r.status = OrderStatus.UNKNOWN
        assert r.is_dangerous and OrderStatus.UNKNOWN in DANGEROUS

    def test_unknown_can_be_resolved_by_reconciliation(self):
        assert can_transition(OrderStatus.UNKNOWN, OrderStatus.RECONCILING)
        assert can_transition(OrderStatus.RECONCILING, OrderStatus.FILLED)

    def test_timeout_never_becomes_a_silent_failure(self):
        """A submitted order cannot jump straight back to CREATED."""
        assert not can_transition(OrderStatus.SUBMITTED, OrderStatus.CREATED)
        assert not can_transition(OrderStatus.FILLED, OrderStatus.CANCELLED)


# ── §41 persistence ───────────────────────────────────────────────────────
class TestOrderStore:
    def test_save_and_read_back(self, tmp_path):
        store = OrderStore(path=tmp_path / "o.db")
        r = OrderRecord.from_intent(good())
        r.status = OrderStatus.VALIDATED
        store.save(r)
        assert store.seen_key(r.idempotency_key)
        assert len(store.recent()) == 1

    def test_update_is_idempotent_not_duplicated(self, tmp_path):
        store = OrderStore(path=tmp_path / "o.db")
        r = OrderRecord.from_intent(good())
        r.status = OrderStatus.VALIDATED
        store.save(r)
        r.status = OrderStatus.SUBMITTED
        r.status = OrderStatus.FILLED
        r.fill_price = 101.0
        store.save(r)
        rows = store.recent()
        assert len(rows) == 1, "an order update created a duplicate row"
        assert rows[0]["status"] == "FILLED"

    def test_dangerous_orders_are_findable_after_restart(self, tmp_path):
        """Restart recovery (§38) must be able to find UNKNOWN orders."""
        path = tmp_path / "o.db"
        store = OrderStore(path=path)
        r = OrderRecord.from_intent(good())
        r.status = OrderStatus.VALIDATED
        r.status = OrderStatus.SUBMITTED
        r.status = OrderStatus.UNKNOWN
        store.save(r)

        reopened = OrderStore(path=path)          # "restart"
        assert len(reopened.dangerous_orders()) == 1
        assert len(reopened.open_orders()) == 1


# ── §65 failure injection ─────────────────────────────────────────────────
class ExplodingBroker(PaperBroker):
    """Simulates a broker whose network call dies after possibly executing."""

    def execute(self, intent, *, _token):
        raise TimeoutError("read timeout after 10s")


class RejectingBroker(PaperBroker):
    def execute(self, intent, *, _token):
        return False, "Fyers rejected order: insufficient margin", None


class TestFailureInjection:
    def test_broker_timeout_becomes_unknown_not_failed(self, risk, costs):
        broker = ExplodingBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)

        result = ex.submit(good())

        assert not result.ok
        assert result.record.status is OrderStatus.UNKNOWN, (
            "a timeout was treated as a clean failure — the order may be live "
            "at the exchange")
        assert "UNKNOWN" in result.message
        assert risk.state.trades_today == 0

    def test_timeout_is_not_blindly_retried(self, risk, costs):
        """§9: the same logical trade must not be resubmitted after a timeout."""
        broker = ExplodingBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)
        intent = good()

        ex.submit(intent)
        second = ex.submit(intent)

        assert second.rejected_by == "duplicate"

    def test_broker_rejection_does_not_count_as_a_trade(self, risk, costs):
        broker = RejectingBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)

        result = ex.submit(good())

        assert not result.ok and result.rejected_by == "broker"
        assert result.record.status is OrderStatus.REJECTED
        assert risk.state.trades_today == 0
        assert risk.state.open_positions == 0

    def test_concurrent_submissions_open_only_one_position(self, risk, costs):
        """§40: the 2s fast loop and the 30s scan loop must not double-enter."""
        import threading

        broker = PaperBroker(risk, costs=costs)
        ex = ExecutionEngine(broker=broker, risk_engine=risk)
        intent = good()
        results = []
        barrier = threading.Barrier(8)

        def fire():
            barrier.wait()
            results.append(ex.submit(intent))

        threads = [threading.Thread(target=fire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for r in results if r.ok) == 1, "concurrent double entry"
        assert len(broker.positions()) == 1
        assert risk.state.open_positions == 1
