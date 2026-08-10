"""Trade journal — SQLite log of every signal, alert, advice, and fill.

Review it to see which strategies actually make money; that feedback loop
is worth more than any fine-tune.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import clock
from .config import ROOT

DB = ROOT / "journal.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,          -- signal | alert | advice | fill | risk
    symbol TEXT,
    direction TEXT,
    score REAL,
    detail TEXT                  -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Journal:
    def __init__(self, path: Path = DB):
        self._path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self):
        return sqlite3.connect(self._path)

    def log(self, kind: str, symbol: str = "", direction: str = "",
            score: float = 0.0, **detail):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO events (ts, kind, symbol, direction, score, detail) VALUES (?,?,?,?,?,?)",
                (clock.now_iso(), kind, symbol,
                 direction, score, json.dumps(detail, default=str)),
            )

    def recent(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        q = "SELECT ts, kind, symbol, direction, score, detail FROM events"
        args: tuple = ()
        if kind:
            q += " WHERE kind = ?"
            args = (kind,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._lock, self._conn() as c:
            rows = c.execute(q, args + (limit,)).fetchall()
        out = []
        for ts, k, sym, d, sc, detail in rows:
            item = {"ts": ts, "kind": k, "symbol": sym, "direction": d, "score": sc}
            try:
                item.update(json.loads(detail or "{}"))
            except json.JSONDecodeError:
                pass
            out.append(item)
        return out
