"""Tool definitions the local Ollama model can call.

All tools are READ-ONLY. There is deliberately no place_order tool —
the LLM advises, the engine + guardrails decide what actually executes.
get_fyers_account exposes the user's REAL broker positions/holdings/funds.
"""

from __future__ import annotations

import json

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_quote",
            "description": "Latest price and today's change for a watchlist symbol.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "Symbol name, e.g. 'NIFTY 50' or 'RELIANCE'"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_technicals",
            "description": "Full technical snapshot: EMAs, RSI, MACD, VWAP, ATR, Bollinger, Supertrend, day high/low, plus each strategy's current vote.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sentiment",
            "description": "Aggregate news sentiment for Indian markets: bias score, label, and the most positive/negative headlines.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Recent Indian financial news headlines, optionally filtered by keyword.",
            "parameters": {
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "Optional filter, e.g. 'nifty', 'bank', a stock name"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_risk_status",
            "description": "Current risk state: daily P&L, remaining loss budget, open positions, trade count, kill-switch and cooldown status. ALWAYS check before recommending a trade.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_positions",
            "description": "Engine's own paper/simulated positions with unrealized P&L (NOT the user's real broker account — for that use get_fyers_account).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fyers_account",
            "description": ("The user's REAL Fyers brokerage account, read live: open positions with "
                            "entry/LTP/P&L, delivery holdings, and available funds/margin. Use this "
                            "whenever the user asks about their actual positions, holdings, or balance."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "allocate_capital",
            "description": ("Build a risk-sized investment plan for a given amount of money: which "
                            "symbols to trade right now, how many units, entry/stop-loss/target, "
                            "capital and worst-case loss per pick. ALWAYS call this when the user "
                            "says how much money they want to invest."),
            "parameters": {
                "type": "object",
                "properties": {"amount": {"type": "number", "description": "Capital in rupees, e.g. 50000"}},
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wallet",
            "description": ("Auto-invest wallet status: money deposited, cash available, amount invested, "
                            "realized P&L, current value, and the recent money-movement ledger. "
                            "Check when the user asks about their auto-invested money."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tracked_trades",
            "description": ("Trades the user has personally taken and is tracking, with live P&L and "
                            "the latest guidance (SL hit / target hit / signal flipped / trail stop). "
                            "Check this when the user asks how their trades are doing or what to do next."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class ToolExecutor:
    """Binds tool names to live engine state. Read-only by construction."""

    def __init__(self, engine):
        self.engine = engine

    def execute(self, name: str, args: dict) -> str:
        try:
            fn = getattr(self, f"_t_{name}", None)
            if fn is None:
                return json.dumps({"error": f"unknown tool {name}"})
            return json.dumps(fn(**(args or {})), default=str)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _resolve(self, symbol: str):
        symbol_l = symbol.lower().replace(" ", "")
        for item, comp in self.engine.latest_signals.items():
            if symbol_l in item.lower().replace(" ", "") or symbol_l in comp.symbol.lower():
                return comp
        return None

    def _t_get_quote(self, symbol: str) -> dict:
        comp = self._resolve(symbol)
        if not comp:
            return {"error": f"'{symbol}' not in watchlist", "watchlist": list(self.engine.latest_signals)}
        s = comp.snapshot
        return {"symbol": comp.name, "ltp": s["ltp"], "change_pct": s["change_pct"],
                "day_high": s["day_high"], "day_low": s["day_low"]}

    def _t_get_technicals(self, symbol: str) -> dict:
        comp = self._resolve(symbol)
        if not comp:
            return {"error": f"'{symbol}' not in watchlist", "watchlist": list(self.engine.latest_signals)}
        return {
            "symbol": comp.name,
            "composite": {"bias": comp.label(), "score": comp.score},
            "indicators": comp.snapshot,
            "strategy_votes": [{"strategy": v.strategy,
                                "vote": "LONG" if v.direction > 0 else ("SHORT" if v.direction < 0 else "NEUTRAL"),
                                "confidence": round(v.confidence, 2), "reason": v.reason}
                               for v in comp.votes],
            "suggested_plan": comp.plan or "no directional plan",
        }

    def _t_get_sentiment(self) -> dict:
        ms = self.engine.market_sentiment
        if ms is None:
            return {"status": "sentiment not computed yet"}
        return {"bias": ms.bias, "label": ms.label, "headlines_analyzed": ms.n_headlines,
                "engine": ms.engine, "top_positive": ms.top_positive, "top_negative": ms.top_negative}

    def _t_get_news(self, keyword: str = "") -> dict:
        if keyword:
            items = self.engine.news.for_symbol([keyword], limit=8)
        else:
            items = self.engine.news.headlines(limit=8)
        return {"headlines": [{"title": h.title, "source": h.source,
                               "sentiment": h.sentiment or "n/a"} for h in items]}

    def _t_get_risk_status(self) -> dict:
        # Every rupee figure in this system is INR. Without saying so, a
        # US-centric base model formats them as dollars, and an Indian options
        # product that reports "$100,000.00" to its user is simply wrong.
        return {"currency": "INR", **self.engine.guardian.snapshot()}

    def _t_allocate_capital(self, amount) -> dict:
        try:
            amount = float(str(amount).replace(",", "").replace("₹", "").strip())
        except ValueError:
            return {"error": f"could not parse amount: {amount!r}"}
        return {"currency": "INR", **self.engine.allocate(amount)}

    def _t_get_wallet(self) -> dict:
        if not self.engine.wallet.active:
            return {"status": "auto-invest is OFF — no wallet active"}
        return {"currency": "INR", **self.engine.wallet.state(),
                "recent_ledger": self.engine.wallet.ledger(8)}

    def _live_ltps(self) -> dict:
        """Freshest LTPs available: 30s scan + option quotes + real-time ticks."""
        ltp = {c.symbol: c.snapshot.get("ltp") for c in self.engine.latest_signals.values()}
        ltp.update(self.engine.extra_quotes)
        if self.engine.ticks:
            ltp.update({k: v for k, v in self.engine.ticks.all().items() if v})
        return ltp

    def _t_get_fyers_account(self) -> dict:
        feed = self.engine.feed
        if not getattr(feed, "account_live", False):
            return {"status": "Not connected to Fyers — run the daily login to read the real account."}
        # Read live so the answer is correct regardless of the 30s scan cadence,
        # falling back to the last synced snapshot if a live call hiccups.
        try:
            raw_positions = feed.positions()
            holdings = feed.holdings()
            funds = feed.funds()
        except Exception:
            raw_positions = self.engine.fyers_positions
            holdings = self.engine.fyers_holdings
            funds = self.engine.funds
        ltp = self._live_ltps()
        positions = [self.engine._tick_fresh_position(p, ltp) for p in raw_positions]
        return {
            "positions": positions or "no open positions",
            "holdings": holdings or "no delivery holdings",
            "funds": funds or "unavailable",
        }

    def _t_get_tracked_trades(self) -> dict:
        ltp = self._live_ltps()
        trades = self.engine.tracker.open_trades()
        return {"tracked_trades": [{
            "id": t.id, "symbol": t.name, "side": t.side, "qty": t.qty,
            "entry": t.entry, "stop_loss": t.stop_loss, "target": t.target,
            "ltp": ltp.get(t.symbol),
            "unrealized_pnl": round(t.unrealized(ltp[t.symbol]), 2) if ltp.get(t.symbol) else None,
            "latest_guidance": (self.engine.tracker.last_guidance(t.id) or {}).get("msg", "holding steady"),
        } for t in trades] or "none — user has not registered any trades"}

    def _t_get_positions(self) -> dict:
        pos = self.engine.broker.positions()
        return {"open_positions": [{"symbol": p.symbol, "side": p.side, "qty": p.qty,
                                    "entry": p.entry, "ltp": p.ltp, "stop_loss": p.stop_loss,
                                    "target": p.target, "unrealized_pnl": round(p.unrealized, 2)}
                                   for p in pos] or "none"}
