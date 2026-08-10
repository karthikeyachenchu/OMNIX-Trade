"""Order state machine + persistent order records (master prompt §10, §41).

UNKNOWN is the important state. When a broker call times out we do NOT assume
the order failed — an order may well be live at the exchange. UNKNOWN blocks
new entries until reconciliation resolves it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from .. import clock
from ..config import ROOT
from .intent import OrderIntent

DB = ROOT / "journal.db"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"


#: States that mean "this order will never trade" — safe to forget.
TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED}

#: States that must block new entries until resolved (§11).
DANGEROUS = {OrderStatus.UNKNOWN, OrderStatus.RECONCILING}

#: Legal transitions. Anything else is a bug and raises.
_ALLOWED: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.VALIDATED, OrderStatus.REJECTED},
    OrderStatus.VALIDATED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED},
    OrderStatus.SUBMITTED: {OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED,
                            OrderStatus.FILLED, OrderStatus.REJECTED,
                            OrderStatus.UNKNOWN, OrderStatus.CANCELLED,
                            OrderStatus.EXPIRED},
    OrderStatus.ACKNOWLEDGED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                               OrderStatus.CANCEL_PENDING, OrderStatus.CANCELLED,
                               OrderStatus.REJECTED, OrderStatus.EXPIRED,
                               OrderStatus.UNKNOWN},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.FILLED, OrderStatus.CANCEL_PENDING,
                                   OrderStatus.CANCELLED, OrderStatus.EXPIRED,
                                   OrderStatus.UNKNOWN},
    OrderStatus.CANCEL_PENDING: {OrderStatus.CANCELLED, OrderStatus.FILLED,
                                 OrderStatus.UNKNOWN},
    OrderStatus.UNKNOWN: {OrderStatus.RECONCILING, OrderStatus.FILLED,
                          OrderStatus.CANCELLED, OrderStatus.REJECTED,
                          OrderStatus.EXPIRED},
    OrderStatus.RECONCILING: {OrderStatus.FILLED, OrderStatus.CANCELLED,
                              OrderStatus.REJECTED, OrderStatus.EXPIRED,
                              OrderStatus.UNKNOWN},
    OrderStatus.FILLED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.EXPIRED: set(),
}


def can_transition(src: OrderStatus, dst: OrderStatus) -> bool:
    return dst in _ALLOWED.get(src, set())


@dataclass
class OrderRecord:
    """The full life story of one order (§9 — every field is mandatory data)."""

    intent_id: str
    idempotency_key: str
    symbol: str
    side: str
    qty: int
    intended_price: float
    stop_loss: float
    target: float | None
    strategy_id: str
    signal_id: str
    is_exit: bool = False

    _status: OrderStatus = OrderStatus.CREATED
    broker_order_id: str = ""
    fill_price: float | None = None
    filled_qty: int = 0
    slippage: float | None = None
    realized_pnl: float | None = None
    reject_reason: str = ""
    risk_decision: dict = field(default_factory=dict)

    created_at: datetime = field(default_factory=clock.now)
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    history: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_intent(cls, intent: OrderIntent) -> OrderRecord:
        return cls(
            intent_id=intent.intent_id,
            idempotency_key=intent.idempotency_key,
            symbol=intent.symbol,
            side=intent.side.value,
            qty=intent.qty,
            intended_price=intent.entry,
            stop_loss=intent.stop_loss,
            target=intent.target,
            strategy_id=intent.strategy_id,
            signal_id=intent.signal_id,
            is_exit=intent.is_exit,
        )

    # status is a property so an illegal transition is impossible to write
    @property
    def status(self) -> OrderStatus:
        return self._status

    @status.setter
    def status(self, new: OrderStatus):
        if new == self._status:
            return
        if not can_transition(self._status, new):
            raise ValueError(
                f"illegal order transition {self._status.value} → {new.value} "
                f"for {self.symbol} ({self.intent_id})")
        self.history.append((clock.now_iso(), f"{self._status.value}->{new.value}"))
        self._status = new

    @property
    def is_dangerous(self) -> bool:
        return self._status in DANGEROUS

    @property
    def is_terminal(self) -> bool:
        return self._status in TERMINAL

    def as_dict(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol, "side": self.side, "qty": self.qty,
            "status": self._status.value,
            "intended_price": self.intended_price,
            "fill_price": self.fill_price,
            "filled_qty": self.filled_qty,
            "slippage": self.slippage,
            "stop_loss": self.stop_loss, "target": self.target,
            "realized_pnl": self.realized_pnl,
            "strategy_id": self.strategy_id, "signal_id": self.signal_id,
            "is_exit": self.is_exit,
            "reject_reason": self.reject_reason,
            "risk_decision": self.risk_decision,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "submitted_at": (self.submitted_at.isoformat(timespec="seconds")
                             if self.submitted_at else None),
            "filled_at": (self.filled_at.isoformat(timespec="seconds")
                          if self.filled_at else None),
            "history": self.history,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    intent_id        TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL,
    broker_order_id  TEXT,
    ts               TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    status           TEXT NOT NULL,
    intended_price   REAL,
    fill_price       REAL,
    filled_qty       INTEGER DEFAULT 0,
    slippage         REAL,
    stop_loss        REAL,
    target           REAL,
    realized_pnl     REAL,
    strategy_id      TEXT,
    signal_id        TEXT,
    is_exit          INTEGER DEFAULT 0,
    reject_reason    TEXT,
    detail           TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_orders_key ON orders(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


class OrderStore:
    """Durable order history. Append-then-update; nothing is ever deleted."""

    def __init__(self, path: Path = DB):
        self._path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self):
        return sqlite3.connect(self._path, timeout=10)

    def save(self, record: OrderRecord) -> None:
        d = record.as_dict()
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO orders (intent_id, idempotency_key, broker_order_id, ts,
                       symbol, side, qty, status, intended_price, fill_price, filled_qty,
                       slippage, stop_loss, target, realized_pnl, strategy_id, signal_id,
                       is_exit, reject_reason, detail)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(intent_id) DO UPDATE SET
                       broker_order_id=excluded.broker_order_id,
                       status=excluded.status,
                       fill_price=excluded.fill_price,
                       filled_qty=excluded.filled_qty,
                       slippage=excluded.slippage,
                       realized_pnl=excluded.realized_pnl,
                       reject_reason=excluded.reject_reason,
                       detail=excluded.detail""",
                (d["intent_id"], d["idempotency_key"], d["broker_order_id"],
                 d["created_at"], d["symbol"], d["side"], d["qty"], d["status"],
                 d["intended_price"], d["fill_price"], d["filled_qty"], d["slippage"],
                 d["stop_loss"], d["target"], d["realized_pnl"], d["strategy_id"],
                 d["signal_id"], int(d["is_exit"]), d["reject_reason"],
                 json.dumps(d, default=str)))

    def open_orders(self) -> list[dict]:
        """Orders that are neither filled nor dead — what restart recovery reads."""
        terminal = tuple(s.value for s in TERMINAL)
        q = (f"SELECT detail FROM orders WHERE status NOT IN "
             f"({','.join('?' * len(terminal))}) ORDER BY ts")
        with self._lock, self._conn() as c:
            rows = c.execute(q, terminal).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def dangerous_orders(self) -> list[dict]:
        states = tuple(s.value for s in DANGEROUS)
        q = (f"SELECT detail FROM orders WHERE status IN "
             f"({','.join('?' * len(states))}) ORDER BY ts")
        with self._lock, self._conn() as c:
            rows = c.execute(q, states).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT detail FROM orders ORDER BY ts DESC LIMIT ?",
                             (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def seen_key(self, key: str) -> bool:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT 1 FROM orders WHERE idempotency_key=? LIMIT 1",
                            (key,)).fetchone()
        return row is not None
