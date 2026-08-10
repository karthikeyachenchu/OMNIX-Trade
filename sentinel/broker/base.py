"""Broker interface + shared types.

The write API is TOKEN-GUARDED. `execute()`, `close()`, `close_all_positions()`
and `mark_to_market()` all demand the ExecutionEngine's private token object.

This is the structural half of the D1 fix (master prompt §78: do not patch the
symptom, make the bypass impossible). Previously any code could call
`broker.place_order(...)`, and the auto-bot skipped even that. Now a shortcut
like:

    wallet.on_entry(...); tracker.add(...)          # never touches a broker
    broker.execute(intent)                          # raises ExecutionDenied

cannot produce a real order. Combined with
`tests/test_no_unsafe_execution_path.py`, reintroducing a bypass fails the
build rather than quietly shipping.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


class ExecutionDenied(Exception):
    """A broker write was attempted without the ExecutionEngine's token."""


def require_token(token) -> None:
    """Every broker write calls this first."""
    from ..execution.engine import _TOKEN, _ExecutionToken
    if not isinstance(token, _ExecutionToken) or token is not _TOKEN:
        raise ExecutionDenied(
            "Broker write attempted outside the ExecutionEngine. All orders must "
            "go through ExecutionEngine.submit(OrderIntent), which enforces the "
            "risk check. See docs/execution.md."
        )


@dataclass
class Position:
    symbol: str
    side: str            # BUY | SELL
    qty: int
    entry: float
    stop_loss: float
    target: float | None
    opened_at: datetime
    tag: str = ""
    ltp: float = 0.0
    strategy_id: str = ""
    intent_id: str = ""
    lot_size: int = 1
    entry_costs: float = 0.0

    @property
    def unrealized(self) -> float:
        """Gross unrealised P&L. Exit costs are applied when it actually closes."""
        sign = 1 if self.side == "BUY" else -1
        return (self.ltp - self.entry) * self.qty * sign


@dataclass
class Fill:
    symbol: str
    side: str
    qty: int
    price: float
    time: datetime
    pnl: float | None = None          # NET of costs on closing fills
    gross_pnl: float | None = None
    costs: float = 0.0
    reason: str = ""
    broker_order_id: str = ""
    intent_id: str = ""
    slippage: float = 0.0
    cost_detail: dict = field(default_factory=dict)


class Broker(ABC):
    """Contract every broker adapter implements. All writes are token-guarded."""

    @abstractmethod
    def execute(self, intent, *, _token) -> tuple[bool, str, Fill | None]:
        """Submit an APPROVED intent. Returns (ok, message, entry_fill)."""

    @abstractmethod
    def positions(self) -> list[Position]:
        """Read-only — no token required."""

    @abstractmethod
    def close(self, symbol: str, reason: str, price: float | None, *, _token) -> Fill | None:
        ...

    @abstractmethod
    def close_all_positions(self, reason: str, *, _token) -> list[Fill]:
        ...

    @abstractmethod
    def mark_to_market(self, quotes: dict[str, float], *, _token) -> list[Fill]:
        ...
