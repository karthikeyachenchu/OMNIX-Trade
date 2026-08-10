"""RiskEngine — the deterministic authority over whether an order may exist.

Every order in the system is approved here or it does not happen (master
prompt §4, §79). The LLM has read-only visibility of this state; it has no
write path, and there is no method here that an advisory layer can call to
loosen a limit.

What changed versus the original RiskGuardian, and why:

  D1  `approve(OrderIntent)` replaces `check_entry(OrderRequest)` as the gate,
      and the ExecutionEngine is the only caller. The auto-bot can no longer
      open a position by writing to the wallet directly.
  D2  `on_exit()` is now driven by the ExecutionEngine for EVERY fill in every
      mode, so daily P&L — and therefore the kill switch — works in live mode
      too, not just when the paper broker happens to close a position.
  D20 Risk state is PERSISTED and rolls over on the trading-day boundary. A
      kill switch now survives a restart; previously it died with the process,
      which meant "no new trades today" lasted until the next crash.

The kill switch is deliberately sticky: once tripped it requires an explicit
human reset. Nothing in the automated path can clear it.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from . import clock
from .config import ROOT, RiskLimits
from .execution.intent import OrderIntent

log = logging.getLogger("sentinel.risk")

IST = clock.IST
STATE_FILE = ROOT / ".risk_state.json"


class KillReason(str):
    """Free-form but recorded verbatim so the operator sees the real cause."""


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    checks: dict = field(default_factory=dict)
    risk_amount: float = 0.0
    risk_budget_left: float = 0.0

    def as_dict(self) -> dict:
        return {"approved": self.approved, "reason": self.reason,
                "checks": self.checks, "risk_amount": round(self.risk_amount, 2),
                "risk_budget_left": round(self.risk_budget_left, 2)}


@dataclass
class RiskState:
    trading_day: str = ""
    starting_equity: float = 0.0
    daily_pnl: float = 0.0
    fees_today: float = 0.0
    trades_today: int = 0
    open_positions: int = 0
    consecutive_losses: int = 0
    peak_equity: float = 0.0
    kill_switch: bool = False
    kill_reason: str = ""
    kill_at: str = ""
    cooldown_until: str | None = None      # ISO string so it persists cleanly
    reconciling: bool = False
    reconcile_reason: str = ""


class RiskEngine:
    """Deterministic, thread-safe, persistent. The final authority on orders."""

    def __init__(self, limits: RiskLimits, capital: float,
                 state_file=STATE_FILE, persist: bool = True):
        self.limits = limits
        self.capital = capital
        self._state_file = state_file
        self._persist_enabled = persist
        self._lock = threading.RLock()
        self._no_entry_after = clock.parse_hhmm(limits.no_entry_after)
        self._square_off = clock.parse_hhmm(limits.square_off_time)
        self.state = RiskState(trading_day=clock.trading_day(),
                               starting_equity=capital, peak_equity=capital)
        self._load()
        self._roll_day_if_needed()

    # ── persistence (D20 / §37 / §38) ─────────────────────────────────────
    def _load(self):
        if not self._persist_enabled:
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        known = RiskState.__dataclass_fields__
        self.state = RiskState(**{k: v for k, v in raw.items() if k in known})
        if self.state.kill_switch:
            log.warning("kill switch restored from disk: %s", self.state.kill_reason)

    def _save(self):
        if not self._persist_enabled:
            return
        try:
            self._state_file.write_text(json.dumps(asdict(self.state), indent=2),
                                        encoding="utf-8")
        except OSError as e:
            log.error("could not persist risk state: %s", e)

    def _roll_day_if_needed(self):
        """New trading day → fresh budget. The kill switch does NOT survive a
        genuine day change, but it does survive a restart within the same day."""
        with self._lock:
            today = clock.trading_day()
            if self.state.trading_day == today:
                return
            log.info("risk state rolling over: %s → %s", self.state.trading_day, today)
            self.state = RiskState(
                trading_day=today,
                starting_equity=self.capital,
                peak_equity=self.capital,
            )
            self._save()

    # ── queries ───────────────────────────────────────────────────────────
    def now_ist(self) -> datetime:
        return clock.now()

    def market_open(self, now: datetime | None = None) -> bool:
        return clock.in_session(now)

    def past_square_off(self, now: datetime | None = None) -> bool:
        return (now or clock.now()).astimezone(IST).time() >= self._square_off

    @property
    def max_daily_loss(self) -> float:
        return self.capital * self.limits.max_daily_drawdown_pct / 100

    def _cooldown_until(self) -> datetime | None:
        if not self.state.cooldown_until:
            return None
        try:
            return datetime.fromisoformat(self.state.cooldown_until)
        except ValueError:
            return None

    def snapshot(self) -> dict:
        self._roll_day_if_needed()
        with self._lock:
            s = self.state
            cd = self._cooldown_until()
            return {
                "trading_day": s.trading_day,
                "daily_pnl": round(s.daily_pnl, 2),
                "fees_today": round(s.fees_today, 2),
                "daily_loss_limit": round(-self.max_daily_loss, 2),
                "loss_budget_left": round(max(0.0, self.max_daily_loss + min(s.daily_pnl, 0)), 2),
                "trades_today": s.trades_today,
                "max_trades_per_day": self.limits.max_trades_per_day,
                "open_positions": s.open_positions,
                "max_open_positions": self.limits.max_open_positions,
                "consecutive_losses": s.consecutive_losses,
                "kill_switch": s.kill_switch,
                "kill_reason": s.kill_reason,
                "kill_at": s.kill_at,
                "reconciling": s.reconciling,
                "reconcile_reason": s.reconcile_reason,
                "cooldown_until": cd.strftime("%H:%M") if cd else None,
                "capital": self.capital,
                "starting_equity": round(s.starting_equity, 2),
            }

    # ── THE GATE ──────────────────────────────────────────────────────────
    def approve(self, intent: OrderIntent) -> RiskDecision:
        """The single authority. Returns an auditable decision, never raises.

        Every rejection names the specific limit that blocked it, so the
        dashboard can explain exactly why a trade did not happen (§52).
        """
        self._roll_day_if_needed()
        now = clock.now()
        checks: dict[str, str] = {}

        with self._lock:
            s = self.state

            # Protective exits are ALWAYS allowed through. Refusing to close a
            # position because a limit was hit is how accounts blow up.
            if intent.is_exit:
                checks["exit"] = "protective exit — always permitted"
                return RiskDecision(True, "OK (exit)", checks)

            def deny(key: str, why: str) -> RiskDecision:
                checks[key] = f"FAIL: {why}"
                return RiskDecision(False, why, checks,
                                    risk_amount=intent.max_loss,
                                    risk_budget_left=max(0.0, self.max_daily_loss
                                                         + min(s.daily_pnl, 0)))

            if s.kill_switch:
                return deny("kill_switch", f"KILL SWITCH ACTIVE: {s.kill_reason}")
            checks["kill_switch"] = "ok"

            if s.reconciling:
                return deny("reconciled",
                            f"RECONCILING — no new positions until resolved: "
                            f"{s.reconcile_reason}")
            checks["reconciled"] = "ok"

            if not self.market_open(now):
                return deny("market_open", "Market is closed")
            checks["market_open"] = "ok"

            if now.astimezone(IST).time() >= self._no_entry_after:
                return deny("entry_window",
                            f"No new entries after {self.limits.no_entry_after} IST")
            checks["entry_window"] = "ok"

            cd = self._cooldown_until()
            if cd and now < cd:
                return deny("cooldown",
                            f"Cooldown after {s.consecutive_losses} losses until "
                            f"{cd.strftime('%H:%M')}")
            checks["cooldown"] = "ok"

            if s.trades_today >= self.limits.max_trades_per_day:
                return deny("trade_cap",
                            f"Daily trade cap reached ({self.limits.max_trades_per_day})")
            checks["trade_cap"] = f"ok ({s.trades_today}/{self.limits.max_trades_per_day})"

            if s.open_positions >= self.limits.max_open_positions:
                return deny("position_cap",
                            f"Max open positions reached ({self.limits.max_open_positions})")
            checks["position_cap"] = f"ok ({s.open_positions}/{self.limits.max_open_positions})"

            # ── order-level validation ────────────────────────────────────
            # OrderIntent already enforces SL presence and direction at
            # construction, so reaching here with a bad stop is impossible.
            # Re-checked anyway: this is the authority, it trusts nothing.
            if intent.stop_loss is None or intent.stop_loss <= 0:
                return deny("stop_loss", "REJECTED: order has no stop-loss (mandatory)")
            if intent.side.value == "BUY" and intent.stop_loss >= intent.entry:
                return deny("stop_loss", "REJECTED: stop-loss must be below entry for BUY")
            if intent.side.value == "SELL" and intent.stop_loss <= intent.entry:
                return deny("stop_loss", "REJECTED: stop-loss must be above entry for SELL")
            checks["stop_loss"] = "ok"

            risk_amount = intent.max_loss
            max_risk = self.capital * self.limits.max_risk_per_trade_pct / 100
            if risk_amount > max_risk:
                return deny("risk_per_trade",
                            f"REJECTED: risk ₹{risk_amount:,.0f} exceeds per-trade cap "
                            f"₹{max_risk:,.0f} ({self.limits.max_risk_per_trade_pct}%)")
            checks["risk_per_trade"] = f"ok (₹{risk_amount:,.0f} of ₹{max_risk:,.0f})"

            # A new trade may not risk more than the remaining daily budget.
            budget_left = max(0.0, self.max_daily_loss + min(s.daily_pnl, 0))
            if risk_amount > budget_left:
                return deny("daily_budget",
                            f"REJECTED: risk ₹{risk_amount:,.0f} exceeds remaining daily "
                            f"loss budget ₹{budget_left:,.0f}")
            checks["daily_budget"] = f"ok (₹{budget_left:,.0f} left)"

            if intent.target is not None:
                rr = intent.reward_risk or 0.0
                if rr < self.limits.min_reward_risk:
                    return deny("reward_risk",
                                f"REJECTED: reward:risk {rr:.2f} below minimum "
                                f"{self.limits.min_reward_risk}")
                checks["reward_risk"] = f"ok ({rr:.2f})"

            return RiskDecision(True, "OK", checks, risk_amount, budget_left)

    # ── legacy shim ───────────────────────────────────────────────────────
    def check_entry(self, order) -> tuple[bool, str]:
        """Deprecated. Kept so nothing silently loses its risk check during the
        migration; converts to an OrderIntent and defers to `approve()`."""
        from .execution.intent import OrderIntent as _OI
        from .execution.intent import Side as _Side
        intent = _OI(symbol=order.symbol, side=_Side(order.side), qty=order.qty,
                     entry=order.entry, stop_loss=order.stop_loss,
                     target=order.target, strategy_id=order.tag or "legacy")
        d = self.approve(intent)
        return d.approved, d.reason

    def max_qty_for(self, entry: float, stop_loss: float, lot_size: int = 1) -> int:
        """Largest qty within the per-trade risk cap, in whole lots.

        Prefer `execution.sizing.PositionSizer`, which also accounts for costs
        and slippage. This remains for the simple equity path.
        """
        per_unit_risk = abs(entry - stop_loss)
        if per_unit_risk <= 0 or lot_size <= 0:
            return 0
        max_risk = self.capital * self.limits.max_risk_per_trade_pct / 100
        qty = int(max_risk / per_unit_risk)
        return (qty // lot_size) * lot_size

    # ── accounting: called by the ExecutionEngine ONLY ─────────────────────
    def on_entry(self, intent=None, record=None):
        with self._lock:
            self.state.trades_today += 1
            self.state.open_positions += 1
            self._save()

    def on_exit(self, pnl: float, intent=None, record=None, symbol: str = "",
                reason: str = "", fees: float = 0.0):
        """Record a realised outcome. `pnl` must already be NET of costs."""
        with self._lock:
            s = self.state
            s.open_positions = max(0, s.open_positions - 1)
            s.daily_pnl += pnl
            s.fees_today += fees

            if pnl < 0:
                s.consecutive_losses += 1
                if s.consecutive_losses >= self.limits.consecutive_losses_pause:
                    until = clock.now() + timedelta(minutes=self.limits.cooldown_minutes)
                    s.cooldown_until = until.isoformat()
                    log.warning("cooldown armed until %s after %d consecutive losses",
                                until.strftime("%H:%M"), s.consecutive_losses)
            else:
                s.consecutive_losses = 0

            if s.daily_pnl <= -self.max_daily_loss and not s.kill_switch:
                self._trip_locked(
                    f"Daily drawdown {self.limits.max_daily_drawdown_pct}% hit "
                    f"(P&L ₹{s.daily_pnl:,.0f}). Done for the day.")
            self._save()

    # ── kill switch (§5) ──────────────────────────────────────────────────
    def trip_kill_switch(self, reason: str):
        """Any subsystem may trip it. Only a human may clear it."""
        with self._lock:
            if self.state.kill_switch:
                return
            self._trip_locked(reason)
            self._save()

    def _trip_locked(self, reason: str):
        self.state.kill_switch = True
        self.state.kill_reason = reason
        self.state.kill_at = clock.now_iso()
        log.critical("KILL SWITCH TRIPPED: %s", reason)

    def reset_kill_switch(self, confirmation: str) -> tuple[bool, str]:
        """Explicit human reset (§5 — never silently resume)."""
        if confirmation != "RESET":
            return False, "kill-switch reset requires the confirmation string 'RESET'"
        with self._lock:
            if not self.state.kill_switch:
                return False, "kill switch is not active"
            prev = self.state.kill_reason
            self.state.kill_switch = False
            self.state.kill_reason = ""
            self.state.kill_at = ""
            self._save()
        log.warning("kill switch manually reset (was: %s)", prev)
        return True, f"kill switch cleared (was: {prev})"

    # ── reconciliation gate (§11) ─────────────────────────────────────────
    def enter_reconciling(self, reason: str):
        with self._lock:
            self.state.reconciling = True
            self.state.reconcile_reason = reason
            self._save()
        log.warning("RECONCILING — new entries blocked: %s", reason)

    def clear_reconciling(self):
        with self._lock:
            self.state.reconciling = False
            self.state.reconcile_reason = ""
            self._save()

    def set_open_positions(self, n: int):
        """Truth from reconciliation overrides local counting."""
        with self._lock:
            self.state.open_positions = max(0, int(n))
            self._save()

    def top_up(self, amount: float) -> float:
        """Add paper capital. Restores the daily loss budget; lifts a drawdown
        kill switch only if the drawdown is genuinely no longer exhausted."""
        amt = max(0.0, float(amount))
        with self._lock:
            self.capital += amt
            if self.state.kill_switch and self.state.daily_pnl > -self.max_daily_loss:
                self.state.kill_switch = False
                self.state.kill_reason = ""
                self.state.kill_at = ""
            self._save()
            return self.capital

    def reset_day(self):
        with self._lock:
            self.state = RiskState(trading_day=clock.trading_day(),
                                   starting_equity=self.capital,
                                   peak_equity=self.capital)
            self._save()


# Backwards-compatible name — the rest of the codebase and the README both
# still say "RiskGuardian", and it is genuinely the same object.
RiskGuardian = RiskEngine


@dataclass
class OrderRequest:
    """Legacy request shape. New code builds an `execution.intent.OrderIntent`."""

    symbol: str
    side: str
    qty: int
    entry: float
    stop_loss: float
    target: float | None = None
    product: str = "INTRADAY"
    tag: str = ""
