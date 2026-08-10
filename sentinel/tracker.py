"""TradeTracker — watches trades the USER has actually taken.

When the user accepts a suggestion ("I took this"), it's registered here.
Every scan cycle the engine calls review(): each open trade is checked
against live price and the current signal, and guidance is emitted the
moment the situation changes (SL hit, target hit, signal flipped, time to
trail, square-off due). Persisted in journal.db so a restart loses nothing.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ROOT

DB = ROOT / "journal.db"

# EXCH:UNDERLYING<yy><expiry><strike><CE|PE>  e.g. BSE:SENSEX2671678000CE
_OPT_RE = re.compile(r"^[A-Z]+:([A-Z]+)\d{2}[A-Z0-9]{3}(\d+)(CE|PE)$")


def _display_name(symbol: str) -> str:
    """Best-effort human name for a Fyers symbol (option or equity)."""
    m = _OPT_RE.match(symbol)
    if m:
        return f"{m.group(1)} {m.group(2)} {m.group(3)}"
    base = symbol.split(":", 1)[-1]
    return base.replace("-EQ", "").replace("-INDEX", "")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    symbol TEXT NOT NULL,        -- fyers symbol
    name TEXT NOT NULL,          -- display name
    side TEXT NOT NULL,          -- BUY | SELL
    qty INTEGER NOT NULL,
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    target REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    closed_at TEXT,
    exit_price REAL,
    pnl REAL
);
"""


@dataclass
class TrackedTrade:
    id: int
    opened_at: str
    symbol: str
    name: str
    side: str
    qty: int
    entry: float
    stop_loss: float
    target: float | None
    source: str = "manual"     # manual (user's own trade) | auto (wallet engine)
    status: str = "OPEN"
    closed_at: str | None = None
    exit_price: float | None = None
    pnl: float | None = None

    def unrealized(self, ltp: float) -> float:
        sign = 1 if self.side == "BUY" else -1
        return (ltp - self.entry) * self.qty * sign


class TradeTracker:
    def __init__(self, path: Path = DB):
        self._path = str(path)
        self._lock = threading.Lock()
        self._guidance: dict[int, dict] = {}   # trade id → last guidance shown
        with self._conn() as c:
            c.executescript(_SCHEMA)
            try:
                c.execute("ALTER TABLE tracked_trades ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
            except sqlite3.OperationalError:
                pass  # column already exists
        self._trades: list[TrackedTrade] = self._load_open()

    def _conn(self):
        return sqlite3.connect(self._path)

    def _load_open(self) -> list[TrackedTrade]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, opened_at, symbol, name, side, qty, entry, stop_loss, target, source "
                "FROM tracked_trades WHERE status = 'OPEN' ORDER BY id").fetchall()
        return [TrackedTrade(*r) for r in rows]

    # ── user actions ──────────────────────────────────────────────────────
    def add(self, symbol: str, name: str, side: str, qty: int,
            entry: float, stop_loss: float, target: float | None = None,
            source: str = "manual") -> TrackedTrade:
        opened = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO tracked_trades (opened_at, symbol, name, side, qty, entry, stop_loss, target, source) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (opened, symbol, name, side.upper(), int(qty), float(entry),
                 float(stop_loss), float(target) if target else None, source))
            trade = TrackedTrade(cur.lastrowid, opened, symbol, name, side.upper(),
                                 int(qty), float(entry), float(stop_loss),
                                 float(target) if target else None, source)
            self._trades.append(trade)
        return trade

    def sync_real(self, positions: list[dict], quotes: dict[str, float],
                  sl_pct: float = 0.35, tgt_pct: float = 0.40) -> None:
        """Mirror the user's REAL Fyers positions into the tracker.

        - a real position with no tracked row → added (source='fyers') with a
          default SL/target the user can retune from the dashboard;
        - a real position that already has a tracked row (any source) → its
          entry/qty/side are refreshed from the broker, SL/target preserved;
        - a previously-mirrored ('fyers') row whose position is gone → closed.
        Manual rows the user added by hand are never auto-closed here.
        """
        real = {p["symbol"]: p for p in positions if p.get("symbol")}
        existing = {t.symbol: t for t in self.open_trades()}

        for sym, p in real.items():
            t = existing.get(sym)
            if t:  # refresh live facts, keep the user's risk levels
                if (t.entry, t.qty, t.side) != (p["entry"], p["qty"], p["side"]):
                    with self._lock, self._conn() as c:
                        c.execute("UPDATE tracked_trades SET entry=?, qty=?, side=? WHERE id=?",
                                  (p["entry"], p["qty"], p["side"], t.id))
                    t.entry, t.qty, t.side = p["entry"], p["qty"], p["side"]
                continue
            long = p["side"] == "BUY"
            entry = p["entry"] or p.get("ltp") or 0.0
            if entry <= 0:
                continue
            sl = round(entry * (1 - sl_pct), 2) if long else round(entry * (1 + sl_pct), 2)
            tgt = round(entry * (1 + tgt_pct), 2) if long else round(entry * (1 - tgt_pct), 2)
            self.add(sym, _display_name(sym), p["side"], p["qty"], entry, sl, tgt, source="fyers")

        for t in self.open_trades():
            if t.source == "fyers" and t.symbol not in real:
                self.close(t.id, quotes.get(t.symbol) or t.entry)

    def open_auto_by_symbol(self, symbol: str) -> TrackedTrade | None:
        with self._lock:
            for t in self._trades:
                if t.source == "auto" and t.symbol == symbol:
                    return t
        return None

    def update_stop(self, trade_id: int, stop_loss: float) -> bool:
        with self._lock:
            for t in self._trades:
                if t.id == trade_id:
                    t.stop_loss = float(stop_loss)
                    with self._conn() as c:
                        c.execute("UPDATE tracked_trades SET stop_loss=? WHERE id=?",
                                  (t.stop_loss, trade_id))
                    self._guidance.pop(trade_id, None)   # re-evaluate fresh
                    return True
        return False

    def close(self, trade_id: int, exit_price: float) -> TrackedTrade | None:
        with self._lock:
            for i, t in enumerate(self._trades):
                if t.id == trade_id:
                    t.status, t.exit_price = "CLOSED", float(exit_price)
                    t.closed_at = datetime.now().isoformat(timespec="seconds")
                    t.pnl = round(t.unrealized(t.exit_price), 2)
                    with self._conn() as c:
                        c.execute("UPDATE tracked_trades SET status='CLOSED', closed_at=?, "
                                  "exit_price=?, pnl=? WHERE id=?",
                                  (t.closed_at, t.exit_price, t.pnl, trade_id))
                    self._trades.pop(i)
                    self._guidance.pop(trade_id, None)
                    return t
        return None

    def open_trades(self) -> list[TrackedTrade]:
        with self._lock:
            return list(self._trades)

    def last_guidance(self, trade_id: int) -> dict | None:
        return self._guidance.get(trade_id)

    # ── the monitoring pass (called by the engine every scan) ────────────
    def review(self, signals_by_symbol: dict, quotes: dict[str, float],
               guardian=None) -> list[tuple[TrackedTrade, str, str]]:
        """Returns (trade, message, level) for guidance that is NEW this scan."""
        events = []
        for t in self.open_trades():
            if t.source == "auto":
                continue  # the engine exits these itself; no "you should act" nagging
            comp = signals_by_symbol.get(t.symbol)
            ltp = quotes.get(t.symbol) or (comp.snapshot.get("ltp") if comp else None)
            if not ltp:
                continue
            long = t.side == "BUY"
            pnl = t.unrealized(ltp)
            r_unit = abs(t.entry - t.stop_loss)
            gained_r = ((ltp - t.entry) if long else (t.entry - ltp)) / r_unit if r_unit else 0

            key, msg, level = None, "", "info"
            sl_hit = ltp <= t.stop_loss if long else ltp >= t.stop_loss
            tgt_hit = t.target and (ltp >= t.target if long else ltp <= t.target)
            flipped = comp and comp.direction == (-1 if long else 1) and comp.score >= 0.55

            if sl_hit:
                key, level = "sl_hit", "warning"
                msg = (f"🛑 STOP-LOSS HIT at {ltp:,.2f} (SL {t.stop_loss:,.2f}). "
                       f"Exit NOW — P&L ₹{pnl:,.0f}. Do not average down.")
            elif tgt_hit:
                key, level = "target_hit", "trade"
                msg = (f"🎯 TARGET HIT at {ltp:,.2f} (+₹{pnl:,.0f}). Book profit, or if riding: "
                       f"trail SL to {t.target:,.2f} so the gain can't evaporate.")
            elif guardian is not None and guardian.past_square_off():
                key, level = "square_off", "warning"
                msg = f"⏰ Square-off time — close this intraday position now (P&L ₹{pnl:,.0f})."
            elif flipped:
                key, level = f"flip_{comp.label()}", "warning"
                msg = (f"⚠️ Signal flipped {comp.label()} ({comp.score:.0%}) against your "
                       f"{t.side}. Consider exiting early at {ltp:,.2f} (P&L ₹{pnl:,.0f}).")
            elif gained_r >= 1.0:
                key, level = "trail_breakeven", "info"
                be = t.entry
                msg = (f"📈 Up {gained_r:.1f}R (+₹{pnl:,.0f}). Move your stop-loss to "
                       f"breakeven ({be:,.2f}) — the trade becomes risk-free.")

            if key and self._guidance.get(t.id, {}).get("key") != key:
                self._guidance[t.id] = {"key": key, "msg": msg, "level": level,
                                        "at": datetime.now().strftime("%H:%M:%S")}
                events.append((t, msg, level))
        return events
