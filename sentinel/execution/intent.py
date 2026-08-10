"""OrderIntent — the immutable request that every order must be built from.

Nothing in this system creates a position from loose arguments any more. A
strategy (or the autonomous controller) builds an OrderIntent; the risk engine
approves or rejects it; only an approved intent reaches a broker. The intent is
frozen, so an approval cannot be granted for one set of numbers and executed
with another (master prompt §51 step 15, §79).

The idempotency key is the duplicate-order defence (§39): two loops firing on
the same signal in the same time bucket produce the SAME key, and the execution
engine refuses the second one.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .. import clock


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


# How wide a time bucket collapses into one idempotency key. Two entry attempts
# for the same signal inside this window are the same logical trade.
IDEMPOTENCY_BUCKET_SEC = 60


@dataclass(frozen=True)
class OrderIntent:
    """An immutable, fully-specified request to open or close a position."""

    symbol: str
    side: Side
    qty: int
    entry: float                 # reference / expected fill price
    stop_loss: float             # MANDATORY for entries
    target: float | None = None
    order_type: OrderType = OrderType.MARKET
    product: str = "INTRADAY"

    # provenance — every order is traceable back to what caused it
    strategy_id: str = "unknown"
    signal_id: str = ""
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    underlying: str = ""         # index/stock behind an option leg
    lot_size: int = 1

    # bookkeeping
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: datetime = field(default_factory=clock.now)
    is_exit: bool = False
    tag: str = ""

    def __post_init__(self):
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.entry <= 0:
            raise ValueError(f"entry must be positive, got {self.entry}")
        if not self.symbol:
            raise ValueError("symbol is required")
        if not self.is_exit:
            if self.stop_loss is None or self.stop_loss <= 0:
                raise ValueError("entries require a positive stop-loss (mandatory)")
            if self.side == Side.BUY and self.stop_loss >= self.entry:
                raise ValueError("BUY stop-loss must be below entry")
            if self.side == Side.SELL and self.stop_loss <= self.entry:
                raise ValueError("SELL stop-loss must be above entry")
        if self.lot_size > 1 and self.qty % self.lot_size:
            raise ValueError(
                f"qty {self.qty} is not a whole multiple of lot size {self.lot_size}")

    # ── derived risk facts ────────────────────────────────────────────────
    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def max_loss(self) -> float:
        """Worst case at the stop, before costs and before slippage."""
        return round(self.risk_per_unit * self.qty, 2)

    @property
    def notional(self) -> float:
        return round(self.entry * self.qty, 2)

    @property
    def reward_risk(self) -> float | None:
        if self.target is None or self.risk_per_unit <= 0:
            return None
        return round(abs(self.target - self.entry) / self.risk_per_unit, 3)

    @property
    def lots(self) -> int:
        return self.qty // self.lot_size if self.lot_size > 0 else self.qty

    # ── duplicate protection ──────────────────────────────────────────────
    @property
    def idempotency_key(self) -> str:
        """Stable across retries of the SAME logical trade (§39).

        Deliberately excludes intent_id and price: two loops that fire on one
        signal a few hundred milliseconds apart must collide.
        """
        bucket = int(self.created_at.timestamp() // IDEMPOTENCY_BUCKET_SEC)
        raw = (f"{self.strategy_id}|{self.symbol}|{self.side.value}|"
               f"{self.signal_id}|{'exit' if self.is_exit else 'entry'}|{bucket}")
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def as_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "lots": self.lots,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "order_type": self.order_type.value,
            "product": self.product,
            "strategy_id": self.strategy_id,
            "signal_id": self.signal_id,
            "reason_codes": list(self.reason_codes),
            "underlying": self.underlying,
            "max_loss": self.max_loss,
            "notional": self.notional,
            "reward_risk": self.reward_risk,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "is_exit": self.is_exit,
            "tag": self.tag,
        }
