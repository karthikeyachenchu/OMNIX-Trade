"""Paper broker — simulates fills, tracks positions, enforces SL/target on ticks.

This is the default. It routes through the same RiskGuardian gate as live
trading, so paper behaviour == live behaviour minus real money.
"""

from __future__ import annotations

import threading
from datetime import datetime

from ..guardrails import OrderRequest, RiskGuardian
from .base import Broker, Fill, Position


class PaperBroker(Broker):
    def __init__(self, guardian: RiskGuardian):
        self.guardian = guardian
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._lock = threading.Lock()

    def place_order(self, order: OrderRequest) -> tuple[bool, str]:
        ok, reason = self.guardian.check_entry(order)
        if not ok:
            return False, reason
        with self._lock:
            if order.symbol in self._positions:
                return False, f"Already holding {order.symbol} (no averaging/stacking)"
            pos = Position(
                symbol=order.symbol, side=order.side, qty=order.qty,
                entry=order.entry, stop_loss=order.stop_loss, target=order.target,
                opened_at=datetime.now(), tag=order.tag, ltp=order.entry,
            )
            self._positions[order.symbol] = pos
            self._fills.append(Fill(order.symbol, order.side, order.qty, order.entry,
                                    datetime.now(), reason=f"ENTRY {order.tag}"))
        self.guardian.on_entry()
        return True, f"PAPER FILL: {order.side} {order.qty} {order.symbol} @ {order.entry:.2f} SL {order.stop_loss:.2f}"

    def positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def mark_to_market(self, quotes: dict[str, float]) -> list[Fill]:
        """Update LTPs; close positions whose SL or target got hit. Returns closing fills."""
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
                        hit = ("STOP-LOSS", pos.stop_loss)
                    elif pos.target and ltp >= pos.target:
                        hit = ("TARGET", pos.target)
                else:
                    if ltp >= pos.stop_loss:
                        hit = ("STOP-LOSS", pos.stop_loss)
                    elif pos.target and ltp <= pos.target:
                        hit = ("TARGET", pos.target)
                if hit:
                    closed.append(self._close_locked(pos, hit[1], hit[0]))
        return closed

    def close_symbol(self, symbol: str, reason: str) -> Fill | None:
        with self._lock:
            pos = self._positions.get(symbol)
            return self._close_locked(pos, pos.ltp or pos.entry, reason) if pos else None

    def close_all(self, reason: str) -> list[Fill]:
        with self._lock:
            return [self._close_locked(pos, pos.ltp or pos.entry, reason)
                    for pos in list(self._positions.values())]

    def _close_locked(self, pos: Position, price: float, reason: str) -> Fill:
        sign = 1 if pos.side == "BUY" else -1
        pnl = (price - pos.entry) * pos.qty * sign
        fill = Fill(pos.symbol, "SELL" if pos.side == "BUY" else "BUY",
                    pos.qty, price, datetime.now(), pnl=pnl, reason=reason)
        self._fills.append(fill)
        del self._positions[pos.symbol]
        self.guardian.on_exit(pnl)
        return fill

    def fills(self) -> list[Fill]:
        with self._lock:
            return list(self._fills)
