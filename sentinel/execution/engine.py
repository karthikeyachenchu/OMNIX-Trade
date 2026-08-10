"""ExecutionEngine — the single authoritative order path (master prompt §9, §79).

    Signal → OrderIntent → RiskCheck → ApprovedOrder → Broker → Fill → Reconcile

Architecturally enforced, not merely documented (§78). `Broker.place_order()`
now requires an execution token that only this class holds, so a future
`wallet.on_entry(); tracker.add()` shortcut cannot reach a broker at all — it
raises. `tests/test_no_unsafe_execution_path.py` (§73) fails the build if
anyone reintroduces one.

Guarantees:
  - every order is risk-approved before submission, no exceptions
  - orders are SERIALISED — one lock, so the 2s fast loop and the 30s scan
    loop cannot both open the same position (defect D15)
  - duplicate logical trades are refused by idempotency key (§39)
  - a submission that errors is recorded as UNKNOWN, never silently retried
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .. import clock
from .intent import OrderIntent
from .state import OrderRecord, OrderStatus

log = logging.getLogger("sentinel.execution")


class ExecutionDenied(Exception):
    """Raised when something tries to reach a broker without approval."""


class _ExecutionToken:
    """Unforgeable proof that a call came from the ExecutionEngine.

    Brokers refuse a write whose token is not this exact object.
    """

    __slots__ = ()


# Module-private singleton. Not exported in __all__; a caller that imports it
# to fake authorisation is doing so deliberately, and the §73 test catches it.
_TOKEN = _ExecutionToken()


@dataclass
class ExecutionResult:
    ok: bool
    message: str
    record: OrderRecord | None = None
    intent: OrderIntent | None = None
    rejected_by: str = ""          # "risk" | "duplicate" | "broker" | "state"

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "message": self.message,
            "rejected_by": self.rejected_by,
            "order": self.record.as_dict() if self.record else None,
            "intent": self.intent.as_dict() if self.intent else None,
        }


class ExecutionEngine:
    """Owns the broker. Nothing else is allowed to touch its write API."""

    def __init__(self, broker, risk_engine, order_store=None, journal=None,
                 on_event=None):
        self._broker = broker
        self._risk = risk_engine
        self._store = order_store
        self._journal = journal
        self._on_event = on_event
        # ONE lock for all order submission — this is the serialisation point.
        self._lock = threading.RLock()
        self._seen_keys: dict[str, str] = {}     # idempotency key → intent_id
        self._records: dict[str, OrderRecord] = {}

    # ── the only way an order is ever created ─────────────────────────────
    def submit(self, intent: OrderIntent) -> ExecutionResult:
        """Validate → risk-check → submit → record. Serialised and idempotent."""
        with self._lock:
            return self._submit_locked(intent)

    def _submit_locked(self, intent: OrderIntent) -> ExecutionResult:
        # STEP 1 — duplicate protection (§39)
        key = intent.idempotency_key
        if key in self._seen_keys:
            msg = (f"DUPLICATE refused: {intent.symbol} {intent.side.value} already "
                   f"submitted as intent {self._seen_keys[key]} in this time bucket")
            log.warning(msg)
            self._emit("risk", intent, msg)
            return ExecutionResult(False, msg, intent=intent, rejected_by="duplicate")

        record = OrderRecord.from_intent(intent)
        self._records[intent.intent_id] = record
        record.status = OrderStatus.VALIDATED

        # STEP 2 — risk authority. The engine cannot proceed without approval.
        decision = self._risk.approve(intent)
        record.risk_decision = decision.as_dict()
        if not decision.approved:
            record.status = OrderStatus.REJECTED
            record.reject_reason = decision.reason
            self._persist(record)
            self._emit("risk", intent, decision.reason)
            log.info("order rejected by risk: %s (%s)", decision.reason, intent.symbol)
            return ExecutionResult(False, decision.reason, record, intent, "risk")

        # STEP 3 — submit. This is the ONLY broker write call in the codebase.
        self._seen_keys[key] = intent.intent_id
        record.status = OrderStatus.SUBMITTED
        record.submitted_at = clock.now()
        try:
            ok, message, fill = self._broker.execute(intent, _token=_TOKEN)
        except Exception as e:
            # A transport failure does NOT mean the order failed (§10). The
            # order is UNKNOWN until reconciliation proves otherwise, and we
            # deliberately do not retry here.
            record.status = OrderStatus.UNKNOWN
            record.reject_reason = f"broker error: {e}"
            self._persist(record)
            self._emit("risk", intent, f"UNKNOWN order state: {e}")
            log.error("broker submission failed for %s — state UNKNOWN, not retrying: %s",
                      intent.symbol, e)
            return ExecutionResult(False, f"UNKNOWN order state: {e}", record,
                                   intent, "broker")

        if not ok:
            record.status = OrderStatus.REJECTED
            record.reject_reason = message
            self._persist(record)
            self._emit("risk", intent, message)
            return ExecutionResult(False, message, record, intent, "broker")

        # STEP 4 — fill accounting. The risk engine is told about EVERY fill,
        # which is what makes the kill switch work (defects D1/D2).
        record.status = OrderStatus.FILLED
        record.filled_at = clock.now()
        if fill is not None:
            record.fill_price = fill.price
            record.filled_qty = fill.qty
            record.broker_order_id = getattr(fill, "broker_order_id", "") or ""
            record.slippage = round((fill.price - intent.entry)
                                    * (1 if intent.side.value == "BUY" else -1), 4)
        else:
            record.fill_price = intent.entry
            record.filled_qty = intent.qty

        if intent.is_exit:
            self._risk.on_exit(record.realized_pnl or 0.0, intent=intent, record=record)
        else:
            self._risk.on_entry(intent=intent, record=record)

        self._persist(record)
        self._emit("fill", intent, message)
        return ExecutionResult(True, message, record, intent)

    # ── exits go through the same authority ───────────────────────────────
    def close(self, symbol: str, reason: str, price: float | None = None):
        """Close an open position. Protective exits are never risk-blocked."""
        with self._lock:
            try:
                fill = self._broker.close(symbol, reason, price, _token=_TOKEN)
            except Exception as e:
                log.error("close failed for %s: %s", symbol, e)
                return None
            if fill is not None:
                self._risk.on_exit(fill.pnl or 0.0, symbol=symbol, reason=reason)
            return fill

    def close_all(self, reason: str):
        with self._lock:
            try:
                fills = self._broker.close_all_positions(reason, _token=_TOKEN) or []
            except Exception as e:
                log.error("close_all failed: %s", e)
                return []
            for f in fills:
                self._risk.on_exit(f.pnl or 0.0, symbol=f.symbol, reason=reason)
            return fills

    # ── views ─────────────────────────────────────────────────────────────
    def positions(self):
        return self._broker.positions()

    def orders(self) -> list[OrderRecord]:
        with self._lock:
            return list(self._records.values())

    def mark_to_market(self, quotes: dict[str, float]):
        """Let the broker enforce SL/target; route resulting fills through risk."""
        with self._lock:
            fills = self._broker.mark_to_market(quotes, _token=_TOKEN) or []
            for f in fills:
                self._risk.on_exit(f.pnl or 0.0, symbol=f.symbol, reason=f.reason)
            return fills

    def reset_day(self):
        """Clear the idempotency window at the day boundary."""
        with self._lock:
            self._seen_keys.clear()

    # ── plumbing ──────────────────────────────────────────────────────────
    def _persist(self, record: OrderRecord):
        if self._store is not None:
            try:
                self._store.save(record)
            except Exception as e:
                log.warning("order store write failed: %s", e)

    def _emit(self, kind: str, intent: OrderIntent, message: str):
        if self._journal is not None:
            try:
                self._journal.log(kind, intent.symbol, intent.side.value, 0.0,
                                  message=message, **intent.as_dict())
            except Exception as e:
                log.debug("journal write failed: %s", e)
        if self._on_event is not None:
            try:
                self._on_event(kind, intent, message)
            except Exception:
                log.debug("execution event hook failed", exc_info=True)
