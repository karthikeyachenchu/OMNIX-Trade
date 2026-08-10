"""Fyers API v3 integration — auth flow, quotes, history, and (gated) orders.

Auth model (Fyers requirement): access tokens expire daily. Run
    python main.py --login
once each morning; the token is cached to .fyers_token for the day.

Live order placement is triple-gated:
  1. config.yaml  mode: live
  2. .env         LIVE_TRADING_CONFIRMED=YES
  3. every order passes RiskGuardian.check_entry()
Anything less falls back to paper simulation.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from .. import clock
from ..config import ROOT, Settings
from ..guardrails import RiskGuardian
from .base import Broker, Fill, Position, require_token

log = logging.getLogger("sentinel.fyers")
TOKEN_FILE = ROOT / ".fyers_token"


def _sdk():
    from fyers_apiv3 import fyersModel  # imported lazily so paper mode never needs it
    return fyersModel


class FyersSession:
    """Handles login + provides an authenticated FyersModel client."""

    def __init__(self, settings: Settings):
        self.s = settings
        self._client = None

    # ── daily login flow ──────────────────────────────────────────────────
    def login_interactive(self) -> bool:
        fyersModel = _sdk()
        session = fyersModel.SessionModel(
            client_id=self.s.fyers_client_id,
            secret_key=self.s.fyers_secret_key,
            redirect_uri=self.s.fyers_redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        url = session.generate_authcode()
        print("\n1. Open this URL, log in to Fyers, and approve:")
        print(f"\n   {url}\n")
        print("2. After redirect, copy the value of `auth_code` from the URL.")
        auth_code = input("\nPaste auth_code here: ").strip()
        session.set_token(auth_code)
        resp = session.generate_token()
        if resp.get("s") != "ok" or "access_token" not in resp:
            print(f"Login failed: {resp}")
            return False
        TOKEN_FILE.write_text(json.dumps({
            "access_token": resp["access_token"],
            "date": clock.trading_day(),
        }), encoding="utf-8")
        print("Login OK — token cached for today.")
        return True

    def token(self) -> str | None:
        if not TOKEN_FILE.exists():
            return None
        try:
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if data.get("date") != clock.trading_day():
            return None  # stale — Fyers tokens are valid for one trading day
        return data.get("access_token")

    def client(self):
        if self._client is None:
            tok = self.token()
            if not tok:
                raise RuntimeError("No valid Fyers token. Run: python main.py --login")
            fyersModel = _sdk()
            self._client = fyersModel.FyersModel(
                client_id=self.s.fyers_client_id, token=tok, is_async=False, log_path=str(ROOT)
            )
        return self._client

    def is_ready(self) -> bool:
        return bool(self.s.fyers_client_id and self.s.fyers_secret_key and self.token())


class FyersData:
    """Market data via Fyers REST (quotes + history)."""

    _INTERVAL_MAP = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "1d": "D"}

    def __init__(self, session: FyersSession):
        self.session = session

    def ltp(self, symbols: list[str]) -> dict[str, float]:
        resp = self.session.client().quotes({"symbols": ",".join(symbols)})
        out: dict[str, float] = {}
        for item in resp.get("d", []):
            v = item.get("v", {})
            if "lp" in v:
                out[item.get("n", "")] = float(v["lp"])
        return out

    def history(self, symbol: str, interval: str, days: int):
        import pandas as pd
        res = self._INTERVAL_MAP.get(interval, "5")
        to = clock.now()
        frm = to - timedelta(days=days)
        resp = self.session.client().history({
            "symbol": symbol, "resolution": res, "date_format": "1",
            "range_from": frm.strftime("%Y-%m-%d"), "range_to": to.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        })
        candles = resp.get("candles", [])
        if not candles:
            return None
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        return df.set_index("ts")

    def option_chain(self, symbol: str, strike_count: int = 10) -> dict:
        """Fyers optionchain endpoint — PCR, OI and strikes around ATM."""
        resp = self.session.client().optionchain({"symbol": symbol, "strikecount": strike_count})
        return resp.get("data", {}) if resp.get("s") == "ok" else {}

    # ── read-only account state (the user's REAL Fyers book) ────────────────
    def positions(self) -> list[dict]:
        """Live net positions from the user's Fyers account (read-only).

        Returns only currently-open legs (netQty != 0), normalised to the
        fields the engine and dashboard use. Never places or changes an order.
        """
        resp = self.session.client().positions()
        if resp.get("s") != "ok":
            return []
        out: list[dict] = []
        for p in resp.get("netPositions", []) or []:
            qty = int(p.get("netQty") or 0)
            if qty == 0:
                continue  # squared-off today — not an open position
            long = qty > 0
            out.append({
                "symbol": p.get("symbol", ""),
                "side": "BUY" if long else "SELL",
                "qty": abs(qty),
                "entry": float(p.get("netAvg") or p.get("buyAvg") or p.get("sellAvg") or 0),
                "ltp": float(p.get("ltp") or 0),
                "pnl": float(p.get("pl") or 0),
                "product": p.get("productType", ""),
                "realized": float(p.get("realized_profit") or 0),
            })
        return out

    def holdings(self) -> list[dict]:
        """Delivery/holdings (T+1 settled equity) from Fyers, read-only."""
        resp = self.session.client().holdings()
        if resp.get("s") != "ok":
            return []
        out: list[dict] = []
        for h in resp.get("holdings", []) or []:
            qty = int(h.get("quantity") or 0)
            if qty == 0:
                continue
            cost = float(h.get("costPrice") or 0)
            ltp = float(h.get("ltp") or 0)
            out.append({
                "symbol": h.get("symbol", ""),
                "qty": qty,
                "cost": cost,
                "ltp": ltp,
                "pnl": float(h.get("pl") if h.get("pl") is not None else (ltp - cost) * qty),
                "value": round(ltp * qty, 2),
            })
        return out

    def funds(self) -> dict:
        """Available balance / margin snapshot from Fyers, read-only."""
        resp = self.session.client().funds()
        if resp.get("s") != "ok":
            return {}
        # fund_limit is a list of labelled rows; pull the ones we care about.
        rows = {r.get("title", ""): r for r in resp.get("fund_limit", []) or []}
        def val(title: str) -> float:
            r = rows.get(title, {})
            return float(r.get("equityAmount") or r.get("commodityAmount") or 0)
        return {
            "total": val("Total Balance"),
            "available": val("Available Balance"),
            "utilized": val("Utilized Amount"),
            "realized_pnl": val("Realized Profit and Loss"),
        }


class FyersBroker(Broker):
    """Live order routing. Only reachable when both safety flags are set.

    Writes are token-guarded: only the ExecutionEngine can reach them, and it
    only calls them for a risk-approved OrderIntent (master prompt §79).
    """

    def __init__(self, session: FyersSession, guardian: RiskGuardian,
                 settings: Settings, costs=None):
        if not (settings.mode == "live" and settings.live_trading_confirmed):
            raise RuntimeError(
                "Live trading is not armed. Requires mode: live in config.yaml "
                "AND LIVE_TRADING_CONFIRMED=YES in .env"
            )
        self.session = session
        self.guardian = guardian
        self.s = settings
        self.costs = costs
        self._data = FyersData(session)
        self._fills: list[Fill] = []

    def execute(self, intent, *, _token) -> tuple[bool, str, Fill | None]:
        require_token(_token)
        # NOTE: productType is configurable because BO (bracket order) was
        # restricted by Indian exchanges and Fyers v3 may reject it (defect
        # D11). INTRADAY + a separate protective stop is the safe default;
        # verify against current Fyers docs before changing.
        payload = {
            "symbol": intent.symbol,
            "qty": intent.qty,
            "type": 2,  # market
            "side": 1 if intent.side.value == "BUY" else -1,
            "productType": self.s.live_product_type,
            "limitPrice": 0, "stopPrice": 0, "disclosedQty": 0,
            "validity": "DAY", "offlineOrder": False,
            "orderTag": (intent.strategy_id or "sentinel")[:20],
        }
        if self.s.live_product_type == "BO":
            payload["stopLoss"] = round(abs(intent.entry - intent.stop_loss), 1)
            payload["takeProfit"] = round(
                abs((intent.target or intent.entry) - intent.entry), 1)

        resp = self.session.client().place_order(payload)
        if resp.get("s") == "ok":
            fill = Fill(intent.symbol, intent.side.value, intent.qty, intent.entry,
                        clock.now(), reason=f"LIVE ENTRY {intent.strategy_id}",
                        broker_order_id=str(resp.get("id", "")),
                        intent_id=intent.intent_id)
            self._fills.append(fill)
            return True, f"LIVE ORDER PLACED: {resp.get('id', '')}", fill
        return False, f"Fyers rejected order: {resp}", None

    def positions(self) -> list[Position]:
        """D10 fix: this used to read `qty` and `avgPrice`, but Fyers v3 returns
        `netQty` and `netAvg`, so live positions always came back EMPTY. There
        is now exactly one normalisation layer — FyersData.positions() — and
        every consumer goes through it (master prompt §13)."""
        out = []
        for p in self._data.positions():
            out.append(Position(
                symbol=p["symbol"], side=p["side"], qty=p["qty"],
                entry=p["entry"], stop_loss=0.0, target=None,
                opened_at=clock.now(), ltp=p["ltp"],
            ))
        return out

    def mark_to_market(self, quotes: dict[str, float], *, _token) -> list[Fill]:
        """D2: the live kill switch used to be blind because this returned []
        unconditionally, so `on_exit()` was never called and daily P&L stayed
        0.00 forever. Realised P&L is now read back from the broker and fed to
        the risk engine by the reconciler (see reconcile.py), not invented here.
        """
        require_token(_token)
        return []

    def close(self, symbol: str, reason: str, price: float | None = None,
              *, _token) -> Fill | None:
        require_token(_token)
        resp = self.session.client().exit_positions({"id": symbol})
        if resp.get("s") != "ok":
            log.warning("live exit failed for %s: %s", symbol, resp)
            return None
        return Fill(symbol, "SELL", 0, price or 0.0, clock.now(),
                    reason=reason, broker_order_id=str(resp.get("id", "")))

    def close_all_positions(self, reason: str, *, _token) -> list[Fill]:
        require_token(_token)
        self.session.client().exit_positions({})
        return []  # realised P&L is reconciled from the broker, not assumed here
