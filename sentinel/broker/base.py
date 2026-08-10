"""Broker interface + shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


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

    @property
    def unrealized(self) -> float:
        sign = 1 if self.side == "BUY" else -1
        return (self.ltp - self.entry) * self.qty * sign


@dataclass
class Fill:
    symbol: str
    side: str
    qty: int
    price: float
    time: datetime
    pnl: float | None = None   # set on closing fills
    reason: str = ""


class Broker(ABC):
    @abstractmethod
    def place_order(self, order) -> tuple[bool, str]: ...

    @abstractmethod
    def positions(self) -> list[Position]: ...

    @abstractmethod
    def close_all(self, reason: str) -> list[Fill]: ...

    @abstractmethod
    def mark_to_market(self, quotes: dict[str, float]) -> list[Fill]: ...
