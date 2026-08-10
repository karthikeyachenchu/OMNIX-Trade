"""Paper broker — a simulator that tries to be pessimistic (master prompt §7).

The original version computed `(exit - entry) * qty` and filled every order at
the exact requested price. That makes paper results systematically better than
reality and is the fastest way to talk yourself into a bad strategy.

This version models:
  - bid/ask spread — you buy at the ask and sell at the bid, always
  - slippage on market orders, adverse by construction
  - GAP-THROUGH-STOP: if price jumps past your stop, you fill at the gapped
    price, not at your stop level. This is the single most important
    difference between paper and live P&L on Indian options.
  - full transaction costs via the shared cost engine (§8)

Everything is configurable and nothing is optimistic. Fills are still assumed
immediate and complete; partial fills and rejections are modelled by the
FaultyPaperBroker used in the failure-injection tests (§65).
"""

from __future__ import annotations

import logging
import threading

from .. import clock
from ..execution.costs import CostModel, segment_for_symbol
from .base import Broker, Fill, Position, require_token

log = logging.getLogger("sentinel.paper")


class PaperBroker(Broker):
    """Simulated execution with realistic friction."""

    def __init__(self, risk_engine, costs: CostModel | None = None,
                 spread_pct: float = 0.002, slippage_pct: float = 0.001):
        """`spread_pct` is the full bid-ask spread as a fraction of price;
        half of it is paid on each leg. Defaults are deliberately conservative
        for index options, which are wider than equities."""
        self.risk = risk_engine
        self.costs = costs or CostModel()
        self.spread_pct = spread_pct
        self.slippage_pct = slippage_pct
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._lock = threading.RLock()

    # ── fill price modelling ──────────────────────────────────────────────
    def _fill_price(self, reference: float, side: str, aggressive: bool = True) -> float:
        """Adverse fill: cross the spread, then lose a little more to slippage."""
        half_spread = reference * self.spread_pct / 2
        slip = reference * self.slippage_pct if aggressive else 0.0
        if side.upper() == "BUY":
            return round(reference + half_spread + slip, 2)
        return round(max(0.05, reference - half_spread - slip), 2)

    # ── write API (token-guarded) ─────────────────────────────────────────
    def execute(self, intent, *, _token) -> tuple[bool, str, Fill | None]:
        require_token(_token)
        with self._lock:
            if intent.symbol in self._positions:
                return False, f"Already holding {intent.symbol} (no averaging/stacking)", None

            price = self._fill_price(intent.entry, intent.side.value)
            segment = segment_for_symbol(intent.symbol, intent.product)
            entry_costs = self.costs.leg(segment, intent.side.value, price, intent.qty)

            pos = Position(
                symbol=intent.symbol, side=intent.side.value, qty=intent.qty,
                entry=price, stop_loss=intent.stop_loss, target=intent.target,
                opened_at=clock.now(), tag=intent.tag, ltp=price,
                strategy_id=intent.strategy_id, intent_id=intent.intent_id,
                lot_size=intent.lot_size, entry_costs=entry_costs.total,
            )
            self._positions[intent.symbol] = pos
            fill = Fill(intent.symbol, intent.side.value, intent.qty, price,
                        clock.now(), reason=f"ENTRY {intent.strategy_id}",
                        intent_id=intent.intent_id,
                        slippage=round(price - intent.entry, 2),
                        costs=entry_costs.total,
                        cost_detail=entry_costs.as_dict())
            self._fills.append(fill)

        return True, (f"PAPER FILL: {intent.side.value} {intent.qty} {intent.symbol} "
                      f"@ {price:.2f} (asked {intent.entry:.2f}, slip "
                      f"{fill.slippage:+.2f}, costs ₹{entry_costs.total:,.0f}) "
                      f"SL {intent.stop_loss:.2f}"), fill

    def close(self, symbol: str, reason: str, price: float | None = None,
              *, _token) -> Fill | None:
        require_token(_token)
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                return None
            ref = price if price is not None else (pos.ltp or pos.entry)
            return self._close_locked(pos, ref, reason)

    def close_all_positions(self, reason: str, *, _token) -> list[Fill]:
        require_token(_token)
        with self._lock:
            return [self._close_locked(p, p.ltp or p.entry, reason)
                    for p in list(self._positions.values())]

    def mark_to_market(self, quotes: dict[str, float], *, _token) -> list[Fill]:
        """Update LTPs and close anything whose stop or target was breached.

        Gap handling: the fill price is the WORSE of the level and the observed
        price. A stop at 100 with the market at 92 fills at 92.
        """
        require_token(_token)
        closed: list[Fill] = []
        with self._lock:
            for sym, pos in list(self._positions.items()):
                ltp = quotes.get(sym)
                if ltp is None:
                    continue
                pos.ltp = ltp
                hit = None
                if pos.side == "BUY":
                    if ltp <= pos.stop_loss:
                        hit = ("STOP-LOSS", min(pos.stop_loss, ltp))   # gap-through
                    elif pos.target and ltp >= pos.target:
                        hit = ("TARGET", min(pos.target, ltp))         # no free upside
                else:
                    if ltp >= pos.stop_loss:
                        hit = ("STOP-LOSS", max(pos.stop_loss, ltp))
                    elif pos.target and ltp <= pos.target:
                        hit = ("TARGET", max(pos.target, ltp))
                if hit:
                    closed.append(self._close_locked(pos, hit[1], hit[0]))
        return closed

    # ── internals ─────────────────────────────────────────────────────────
    def _close_locked(self, pos: Position, reference: float, reason: str) -> Fill:
        exit_side = "SELL" if pos.side == "BUY" else "BUY"
        price = self._fill_price(reference, exit_side)
        segment = segment_for_symbol(pos.symbol)

        sign = 1 if pos.side == "BUY" else -1
        gross = (price - pos.entry) * pos.qty * sign
        exit_costs = self.costs.leg(segment, exit_side, price, pos.qty)
        total_costs = pos.entry_costs + exit_costs.total
        net = round(gross - exit_costs.total, 2)   # entry costs already charged

        fill = Fill(pos.symbol, exit_side, pos.qty, price, clock.now(),
                    pnl=net, gross_pnl=round(gross, 2),
                    costs=round(total_costs, 2), reason=reason,
                    intent_id=pos.intent_id,
                    slippage=round(price - reference, 2),
                    cost_detail=exit_costs.as_dict())
        self._fills.append(fill)
        del self._positions[pos.symbol]
        return fill

    # ── read-only ─────────────────────────────────────────────────────────
    def positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def fills(self) -> list[Fill]:
        with self._lock:
            return list(self._fills)
