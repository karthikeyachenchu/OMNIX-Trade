"""Wallet — the auto-invest money pot, with a full audit ledger.

The user deposits an amount on the dashboard; the engine trades it
autonomously (through the same guardrails and broker as everything else).
Every rupee movement — deposit, entry, exit, square-off, withdrawal — is a
ledger row in journal.db, so the money is always accounted for and survives
restarts. Paper mode: simulated cash. Live mode: this is the budget cap the
engine is allowed to use of the real Fyers funds.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import clock
from .config import ROOT

DB = ROOT / "journal.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    deposited REAL NOT NULL,
    cash REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | STOPPED
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS wallet_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,       -- deposit | entry | exit | square_off | withdraw
    symbol TEXT,
    amount REAL NOT NULL,     -- signed cash movement
    balance_after REAL NOT NULL,
    note TEXT
);
"""


class Wallet:
    def __init__(self, path: Path = DB):
        self._path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)
        self._id: int | None = None
        self._deposited = self._cash = self._realized = 0.0
        self._load_active()

    def _conn(self):
        return sqlite3.connect(self._path)

    def _load_active(self):
        with self._conn() as c:
            row = c.execute("SELECT id, deposited, cash, realized_pnl FROM wallet "
                            "WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            self._id, self._deposited, self._cash, self._realized = row

    @property
    def active(self) -> bool:
        return self._id is not None

    # ── user actions ──────────────────────────────────────────────────────
    def activate(self, amount: float) -> dict:
        amount = float(amount)
        if amount <= 0:
            return {"error": "Amount must be positive."}
        with self._lock:
            if self.active:
                return {"error": f"Wallet already active with ₹{self._deposited:,.0f}. "
                                 "Stop it first to change the amount."}
            now = clock.now_iso()
            with self._conn() as c:
                cur = c.execute("INSERT INTO wallet (created_at, deposited, cash) VALUES (?,?,?)",
                                (now, amount, amount))
                self._id = cur.lastrowid
            self._deposited, self._cash, self._realized = amount, amount, 0.0
            self._ledger("deposit", "", amount, "auto-invest activated")
        return self.state()

    def add_funds(self, amount: float) -> dict:
        """Top up an ALREADY-ACTIVE wallet with more money to deploy.
        The extra cash becomes immediately investable on the next auto step."""
        amount = float(amount)
        if amount <= 0:
            return {"error": "Amount must be positive."}
        with self._lock:
            if not self.active:
                return {"error": "No active wallet — start auto-invest first."}
            self._deposited += amount
            self._cash += amount
            self._persist()
            self._ledger("deposit", "", amount, "top-up (added while running)")
        return self.state()

    def stop(self) -> dict:
        with self._lock:
            if not self.active:
                return {"error": "No active wallet."}
            final = self.state()
            cash_out = self._cash
            self._cash = 0.0
            # D7 fix: the zeroed cash was never written back, so a STOPPED
            # wallet row kept its stale balance (wallet id=6 is on disk right
            # now, STOPPED with cash 560.60). Harmless only because stopped
            # wallets are never reloaded — it corrupts any report that reads
            # wallet history. Persist BEFORE the ledger row so `balance_after`
            # and the wallet row agree.
            self._persist()
            self._ledger("withdraw", "", -cash_out, "auto-invest stopped, cash withdrawn")
            with self._conn() as c:
                c.execute("UPDATE wallet SET status='STOPPED', closed_at=? WHERE id=?",
                          (clock.now_iso(), self._id))
            self._id = None
            self._deposited = self._cash = self._realized = 0.0
        return final

    # ── engine accounting ────────────────────────────────────────────────
    def on_entry(self, cost: float, symbol: str, note: str = "") -> bool:
        with self._lock:
            if not self.active or cost > self._cash:
                return False
            self._cash -= cost
            self._persist()
            self._ledger("entry", symbol, -cost, note)
        return True

    def on_exit(self, proceeds: float, pnl: float, symbol: str, note: str = "",
                kind: str = "exit"):
        with self._lock:
            if not self.active:
                return
            self._cash += proceeds
            self._realized += pnl
            self._persist()
            self._ledger(kind, symbol, proceeds, f"{note} (P&L ₹{pnl:,.0f})")

    def _persist(self):
        with self._conn() as c:
            c.execute("UPDATE wallet SET deposited=?, cash=?, realized_pnl=? WHERE id=?",
                      (self._deposited, self._cash, self._realized, self._id))

    def _ledger(self, kind: str, symbol: str, amount: float, note: str):
        with self._conn() as c:
            c.execute("INSERT INTO wallet_ledger (wallet_id, ts, kind, symbol, amount, "
                      "balance_after, note) VALUES (?,?,?,?,?,?,?)",
                      (self._id or 0, clock.now_iso(),
                       kind, symbol, round(amount, 2), round(self._cash, 2), note))

    # ── views ─────────────────────────────────────────────────────────────
    def state(self) -> dict | None:
        if not self.active:
            return None
        invested = self._deposited + self._realized - self._cash
        return {
            "deposited": round(self._deposited, 2),
            "cash": round(self._cash, 2),
            "invested": round(max(0.0, invested), 2),
            "realized_pnl": round(self._realized, 2),
            "value": round(self._cash + max(0.0, invested), 2),
        }

    def ledger(self, limit: int = 15) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, kind, symbol, amount, balance_after, note FROM wallet_ledger "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r[0], "kind": r[1], "symbol": r[2], "amount": r[3],
                 "balance_after": r[4], "note": r[5]} for r in rows]
