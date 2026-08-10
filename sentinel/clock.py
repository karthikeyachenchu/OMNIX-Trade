"""The single authoritative clock.

Every timestamp in the system comes from here. Market logic, journal rows,
tracked-trade timestamps and day-rollover keys must all agree, otherwise a
machine whose OS timezone is not IST silently rolls the trading day over at
the wrong moment (defect D8).

Rules:
  - `now()` is ALWAYS timezone-aware IST. There is no naked `datetime.now()`
    anywhere in the codebase; `tests/test_no_naive_datetime.py` enforces that.
  - `trading_day()` is the key used for anything that resets daily.
  - Session boundaries live here so market-open logic has one definition.

The clock is injectable: `set_clock()` lets tests and the backtester replay
historical time without touching global state in an unrecoverable way.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE/BSE regular equity + F&O session.
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
PRE_OPEN = time(9, 0)


class Clock:
    """Real wall-clock, pinned to IST."""

    def now(self) -> datetime:
        return datetime.now(IST)


class FrozenClock:
    """Deterministic clock for tests and replay."""

    def __init__(self, at: datetime):
        self._at = _ensure_ist(at)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._at

    def set(self, at: datetime) -> None:
        with self._lock:
            self._at = _ensure_ist(at)

    def advance(self, **kwargs) -> datetime:
        with self._lock:
            self._at = self._at + timedelta(**kwargs)
            return self._at


def _ensure_ist(dt: datetime) -> datetime:
    """Attach IST to a naive datetime; convert an aware one into IST."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


_clock: Clock | FrozenClock = Clock()
_clock_lock = threading.Lock()


def set_clock(clock: Clock | FrozenClock) -> None:
    """Swap the global clock (tests / backtest replay)."""
    global _clock
    with _clock_lock:
        _clock = clock


def get_clock() -> Clock | FrozenClock:
    return _clock


def reset_clock() -> None:
    set_clock(Clock())


# ── the API everything else uses ──────────────────────────────────────────
def now() -> datetime:
    """Current time, always timezone-aware IST."""
    return _clock.now()


def now_iso() -> str:
    """Timestamp for database rows — IST, second resolution, with offset."""
    return now().isoformat(timespec="seconds")


def today() -> date:
    return now().date()


def trading_day(at: datetime | None = None) -> str:
    """The day key for anything that resets daily (YYYY-MM-DD, IST).

    A session never crosses midnight IST, so the calendar date is a safe key.
    """
    return (at or now()).astimezone(IST).strftime("%Y-%m-%d")


def is_weekday(at: datetime | None = None) -> bool:
    return (at or now()).weekday() < 5


def in_session(at: datetime | None = None) -> bool:
    """True during the regular trading session (weekday, 09:15–15:30 IST).

    Exchange holidays are NOT modelled here — the data-quality gate catches a
    holiday by observing that no ticks are arriving, rather than by trusting a
    hard-coded calendar that goes stale.
    """
    at = at or now()
    if not is_weekday(at):
        return False
    return SESSION_OPEN <= at.astimezone(IST).time() <= SESSION_CLOSE


def parse_hhmm(s: str) -> time:
    """Parse a 'HH:MM' config value into a time object."""
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid HH:MM value: {s!r}")
    return time(h, m)


def at_time(t: time, at: datetime | None = None) -> datetime:
    """Today's `t` o'clock in IST."""
    base = (at or now()).astimezone(IST)
    return base.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def age_seconds(ts: datetime | float | None) -> float | None:
    """Seconds since `ts` (accepts an epoch float or an aware datetime)."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return max(0.0, now().timestamp() - float(ts))
    return max(0.0, (now() - _ensure_ist(ts)).total_seconds())
