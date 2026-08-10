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
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

from ..config import ROOT, Settings
from ..guardrails import OrderRequest, RiskGuardian
from .base import Broker, Fill, Position

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
            "date": datetime.now().strftime("%Y-%m-%d"),
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
        if data.get("date") != datetime.now().strftime("%Y-%m-%d"):
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
        to = datetime.now()
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
    """Live order routing. Only reachable when both safety flags are set."""

    def __init__(self, session: FyersSession, guardian: RiskGuardian, settings: Settings):
        if not (settings.mode == "live" and settings.live_trading_confirmed):
            raise RuntimeError(
                "Live trading is not armed. Requires mode: live in config.yaml "
                "AND LIVE_TRADING_CONFIRMED=YES in .env"
            )
        self.session = session
        self.guardian = guardian
        self._fills: list[Fill] = []

    def place_order(self, order: OrderRequest) -> tuple[bool, str]:
        ok, reason = self.guardian.check_entry(order)
        if not ok:
            return False, reason
        # Bracket-style: market entry + hard SL and target attached at the exchange.
        payload = {
            "symbol": order.symbol,
            "qty": order.qty,
            "type": 2,  # market
            "side": 1 if order.side == "BUY" else -1,
            "productType": "BO",
            "limitPrice": 0, "stopPrice": 0, "disclosedQty": 0,
            "validity": "DAY", "offlineOrder": False,
            "stopLoss": round(abs(order.entry - order.stop_loss), 1),
            "takeProfit": round(abs((order.target or order.entry) - order.entry), 1),
            "orderTag": (order.tag or "sentinel")[:20],
        }
        resp = self.session.client().place_order(payload)
        if resp.get("s") == "ok":
            self.guardian.on_entry()
            self._fills.append(Fill(order.symbol, order.side, order.qty, order.entry,
                                    datetime.now(), reason=f"LIVE ENTRY {order.tag}"))
            return True, f"LIVE ORDER PLACED: {resp.get('id', '')}"
        return False, f"Fyers rejected order: {resp}"

    def positions(self) -> list[Position]:
        resp = self.session.client().positions()
        out = []
        for p in resp.get("netPositions", []):
            if p.get("qty", 0) == 0:
                continue
            out.append(Position(
                symbol=p["symbol"], side="BUY" if p["qty"] > 0 else "SELL",
                qty=abs(p["qty"]), entry=p.get("avgPrice", 0.0),
                stop_loss=0.0, target=None, opened_at=datetime.now(),
                ltp=p.get("ltp", 0.0),
            ))
        return out

    def mark_to_market(self, quotes: dict[str, float]) -> list[Fill]:
        return []  # exchange-side BO legs handle SL/target in live mode

    def close_all(self, reason: str) -> list[Fill]:
        self.session.client().exit_positions({})
        return []  # live P&L settles in the Fyers account, not the local wallet
