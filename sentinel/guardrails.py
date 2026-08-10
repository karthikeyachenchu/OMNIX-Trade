"""RiskGuardian — hard, code-level safety layer.

Every order passes through here AFTER the LLM/strategies and BEFORE the broker.
The LLM has read-only visibility of this state via a tool; it has no write path.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import RiskLimits

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class OrderRequest:
    symbol: str
    side: str                 # BUY | SELL
    qty: int
    entry: float              # reference price (limit or expected fill)
    stop_loss: float          # MANDATORY — orders without SL are rejected
    target: float | None = None
    product: str = "INTRADAY"
    tag: str = ""             # strategy / reason tag for the journal


@dataclass
class RiskState:
    daily_pnl: float = 0.0
    trades_today: int = 0
    open_positions: int = 0
    consecutive_losses: int = 0
    kill_switch: bool = False
    kill_reason: str = ""
    cooldown_until: datetime | None = None


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class RiskGuardian:
    def __init__(self, limits: RiskLimits, capital: float):
        self.limits = limits
        self.capital = capital
        self.state = RiskState()
        self._lock = threading.Lock()
        self._no_entry_after = _parse_hhmm(limits.no_entry_after)
        self._square_off = _parse_hhmm(limits.square_off_time)

    # ── queries ───────────────────────────────────────────────────────────
    def now_ist(self) -> datetime:
        return datetime.now(IST)

    def market_open(self, now: datetime | None = None) -> bool:
        now = now or self.now_ist()
        if now.weekday() >= 5:
            return False
        return time(9, 15) <= now.time() <= time(15, 30)

    def past_square_off(self, now: datetime | None = None) -> bool:
        now = now or self.now_ist()
        return now.time() >= self._square_off

    def snapshot(self) -> dict:
        with self._lock:
            s = self.state
            max_loss = self.capital * self.limits.max_daily_drawdown_pct / 100
            return {
                "daily_pnl": round(s.daily_pnl, 2),
                "daily_loss_limit": round(-max_loss, 2),
                "loss_budget_left": round(max(0.0, max_loss + min(s.daily_pnl, 0)), 2),
                "trades_today": s.trades_today,
                "max_trades_per_day": self.limits.max_trades_per_day,
                "open_positions": s.open_positions,
                "max_open_positions": self.limits.max_open_positions,
                "consecutive_losses": s.consecutive_losses,
                "kill_switch": s.kill_switch,
                "kill_reason": s.kill_reason,
                "cooldown_until": s.cooldown_until.strftime("%H:%M") if s.cooldown_until else None,
                "capital": self.capital,
            }

    # ── the gate ──────────────────────────────────────────────────────────
    def check_entry(self, order: OrderRequest) -> tuple[bool, str]:
        """Single gate for any NEW position. Returns (allowed, reason)."""
        now = self.now_ist()
        with self._lock:
            s = self.state
            if s.kill_switch:
                return False, f"KILL SWITCH ACTIVE: {s.kill_reason}"
            if not self.market_open(now):
                return False, "Market is closed"
            if now.time() >= self._no_entry_after:
                return False, f"No new entries after {self.limits.no_entry_after} IST"
            if s.cooldown_until and now < s.cooldown_until:
                return False, f"Cooldown after losses until {s.cooldown_until.strftime('%H:%M')}"
            if s.trades_today >= self.limits.max_trades_per_day:
                return False, f"Daily trade cap reached ({self.limits.max_trades_per_day})"
            if s.open_positions >= self.limits.max_open_positions:
                return False, f"Max open positions reached ({self.limits.max_open_positions})"

            # order-level validation
            if order.stop_loss is None or order.stop_loss <= 0:
                return False, "REJECTED: order has no stop-loss (mandatory)"
            if order.side == "BUY" and order.stop_loss >= order.entry:
                return False, "REJECTED: stop-loss must be below entry for BUY"
            if order.side == "SELL" and order.stop_loss <= order.entry:
                return False, "REJECTED: stop-loss must be above entry for SELL"

            risk_amount = abs(order.entry - order.stop_loss) * order.qty
            max_risk = self.capital * self.limits.max_risk_per_trade_pct / 100
            if risk_amount > max_risk:
                return False, (f"REJECTED: risk ₹{risk_amount:,.0f} exceeds per-trade cap "
                               f"₹{max_risk:,.0f} ({self.limits.max_risk_per_trade_pct}%)")

            if order.target:
                rr = abs(order.target - order.entry) / max(abs(order.entry - order.stop_loss), 1e-9)
                if rr < self.limits.min_reward_risk:
                    return False, f"REJECTED: reward:risk {rr:.2f} below minimum {self.limits.min_reward_risk}"

            return True, "OK"

    def max_qty_for(self, entry: float, stop_loss: float, lot_size: int = 1) -> int:
        """Position sizing: largest qty within the per-trade risk cap, in whole lots."""
        per_unit_risk = abs(entry - stop_loss)
        if per_unit_risk <= 0:
            return 0
        max_risk = self.capital * self.limits.max_risk_per_trade_pct / 100
        qty = int(max_risk / per_unit_risk)
        return (qty // lot_size) * lot_size

    # ── accounting callbacks (called by the broker layer only) ───────────
    def on_entry(self):
        with self._lock:
            self.state.trades_today += 1
            self.state.open_positions += 1

    def on_exit(self, pnl: float):
        with self._lock:
            s = self.state
            s.open_positions = max(0, s.open_positions - 1)
            s.daily_pnl += pnl
            if pnl < 0:
                s.consecutive_losses += 1
                if s.consecutive_losses >= self.limits.consecutive_losses_pause:
                    s.cooldown_until = self.now_ist() + timedelta(minutes=self.limits.cooldown_minutes)
            else:
                s.consecutive_losses = 0

            max_loss = self.capital * self.limits.max_daily_drawdown_pct / 100
            if s.daily_pnl <= -max_loss and not s.kill_switch:
                s.kill_switch = True
                s.kill_reason = (f"Daily drawdown {self.limits.max_daily_drawdown_pct}% hit "
                                 f"(P&L ₹{s.daily_pnl:,.0f}). Done for the day.")

    def top_up(self, amount: float) -> float:
        """Add paper trading capital. A fresh injection restores the daily loss
        budget, so a drawdown kill-switch is lifted if it's no longer exhausted.
        Returns the new capital. Negative amounts are ignored."""
        amt = max(0.0, float(amount))
        with self._lock:
            self.capital += amt
            max_loss = self.capital * self.limits.max_daily_drawdown_pct / 100
            if self.state.kill_switch and self.state.daily_pnl > -max_loss:
                self.state.kill_switch = False
                self.state.kill_reason = ""
            return self.capital

    def reset_day(self):
        with self._lock:
            self.state = RiskState()
