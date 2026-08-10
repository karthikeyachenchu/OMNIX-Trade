"""Fyers real-time tick stream (data websocket).

Keeps the freshest last-traded-price for every subscribed symbol in memory,
updated tick-by-tick off a background socket thread. The engine reads these
instead of waiting for the 30s REST scan, so position monitoring is effectively
zero-latency. Falls back silently to REST polling if the socket can't connect.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("sentinel.ws")


class FyersTickStream:
    """Thread-safe live LTP cache backed by the Fyers data socket."""

    def __init__(self, session):
        self._session = session
        self._sock = None
        self._lock = threading.Lock()
        self._ltp: dict[str, float] = {}
        self._at: dict[str, float] = {}        # symbol → epoch of last tick
        self._subs: set[str] = set()
        self._connected = threading.Event()
        self._started = False
        self._connected_at = 0.0

    # ── public API ─────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self, symbols: list[str]) -> None:
        """Open the socket (idempotent) and subscribe to `symbols`."""
        with self._lock:
            self._subs.update(symbols)
            if self._started:
                if self.connected:
                    self._safe_subscribe(list(self._subs))
                return
            self._started = True
        threading.Thread(target=self._run, name="fyers-ws", daemon=True).start()

    def subscribe(self, symbols: list[str]) -> None:
        new = [s for s in symbols if s]
        if not new:
            return
        with self._lock:
            before = len(self._subs)
            self._subs.update(new)
            grew = len(self._subs) > before
        if grew and self.connected:
            self._safe_subscribe(new)

    def ltp(self, symbol: str) -> float | None:
        with self._lock:
            return self._ltp.get(symbol)

    def all(self) -> dict[str, float]:
        with self._lock:
            return dict(self._ltp)

    def age(self, symbol: str) -> float | None:
        """Seconds since the last tick for `symbol` (None if never seen)."""
        with self._lock:
            at = self._at.get(symbol)
        return (time.time() - at) if at else None

    def stats(self) -> dict:
        with self._lock:
            newest = max(self._at.values()) if self._at else 0
        return {
            "connected": self.connected,
            "symbols": len(self._subs),
            "last_tick_age": round(time.time() - newest, 1) if newest else None,
        }

    # ── socket internals ────────────────────────────────────────────────────
    def _access_token(self) -> str | None:
        tok = self._session.token()
        if not tok:
            return None
        return f"{self._session.s.fyers_client_id}:{tok}"

    def _run(self) -> None:
        access = self._access_token()
        if not access:
            log.warning("tick stream: no Fyers token — staying on REST")
            return
        try:
            from fyers_apiv3.FyersWebsocket import data_ws
        except Exception as e:  # pragma: no cover
            log.warning("tick stream unavailable (%s) — staying on REST", e)
            return

        def on_open():
            self._connected.set()
            self._connected_at = time.time()
            with self._lock:
                subs = list(self._subs)
            log.info("tick stream connected — subscribing %d symbols", len(subs))
            self._safe_subscribe(subs)

        def on_message(msg):
            # SymbolUpdate frames are dicts carrying at least symbol + ltp.
            if not isinstance(msg, dict):
                return
            sym = msg.get("symbol")
            ltp = msg.get("ltp")
            if sym is None or ltp is None:
                return
            try:
                px = float(ltp)
            except (TypeError, ValueError):
                return
            now = time.time()
            with self._lock:
                self._ltp[sym] = px
                self._at[sym] = now

        def on_error(msg):
            log.debug("tick stream error: %s", msg)

        def on_close(msg):
            self._connected.clear()
            log.info("tick stream closed: %s", msg)

        try:
            self._sock = data_ws.FyersDataSocket(
                access_token=access,
                log_path="",
                litemode=True,          # ltp-only frames — lightest payload
                write_to_file=False,
                reconnect=True,
                on_connect=on_open,
                on_close=on_close,
                on_error=on_error,
                on_message=on_message,
            )
            self._sock.connect()        # spawns its own read loop
        except Exception as e:
            log.warning("tick stream connect failed (%s) — staying on REST", e)
            self._connected.clear()

    def _safe_subscribe(self, symbols: list[str]) -> None:
        if not self._sock or not symbols:
            return
        try:
            self._sock.subscribe(symbols=symbols, data_type="SymbolUpdate")
        except Exception as e:
            log.debug("subscribe failed: %s", e)

    def close(self) -> None:
        self._connected.clear()
        try:
            if self._sock:
                self._sock.close_connection()
        except Exception:
            pass
