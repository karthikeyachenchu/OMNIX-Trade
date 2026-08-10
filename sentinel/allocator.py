"""CapitalAllocator — turn "I have ₹X" into a concrete, risk-sized plan.

Given an amount, it ranks the current composite signals, sizes each position
with the same risk rules the guardrails enforce (risk per trade as a % of the
amount, SL-distance sizing), and returns a structured plan with the data
behind every pick. Pure computation — it never places orders.
"""

from __future__ import annotations

from . import clock
from .analysis.strategies import Composite
from .config import Settings

CASH_BUFFER_PCT = 5.0      # keep this % of the amount uninvested
MIN_SCORE = 0.50           # signals below this conviction are not suggested
OPTION_SL_PCT = 40.0       # exit an option if the premium drops this much
OPTION_BUDGET_PCT = 40.0   # max % of the amount to spend on option premium
OPTION_RISK_MULT = 5.0     # options risk budget = this × the per-trade risk cap (lumpy instrument)


class CapitalAllocator:
    def __init__(self, settings: Settings, chain_fn=None):
        self.s = settings
        self._has_options = {w.fyers: w.options for w in settings.watchlist}
        self._is_index = {w.fyers: w.fyers.endswith("-INDEX") for w in settings.watchlist}
        self._lot_size = {w.fyers: w.lot_size for w in settings.watchlist}
        self._chain_fn = chain_fn   # feed.option_chain — set by the engine

    # ── real option contract for an index view ───────────────────────────
    def _option_pick(self, comp: Composite, cash_left: float, risk_per_trade: float,
                     amount: float) -> dict | None:
        """ATM option with the real premium from the Fyers chain, or None."""
        if not self._chain_fn:
            return None
        chain = self._chain_fn(comp.symbol, 3) or {}
        rows = chain.get("optionsChain", [])
        want = "CE" if comp.direction > 0 else "PE"
        spot = comp.snapshot.get("ltp") or 0
        opts = [r for r in rows if r.get("option_type") == want
                and r.get("strike_price", 0) > 0 and r.get("ltp", 0) > 0]
        if not opts:
            return None
        atm = min(opts, key=lambda r: abs(r["strike_price"] - spot))
        premium, lot = float(atm["ltp"]), max(1, self._lot_size.get(comp.symbol, 1))
        expiry = (chain.get("expiryData") or [{}])[0].get("date", "")

        per_lot_cost = premium * lot
        budget = min(cash_left, amount * OPTION_BUDGET_PCT / 100)
        lots_by_budget = int(budget / per_lot_cost)
        risk_per_lot = premium * OPTION_SL_PCT / 100 * lot
        lots_by_risk = int(risk_per_trade * OPTION_RISK_MULT / risk_per_lot)
        lots = min(lots_by_budget, max(1, lots_by_risk))   # ≥1 lot if affordable; risk-capped above that
        if lots <= 0:
            return None
        # SL on the premium itself; risk beyond the 1% rule is flagged, not hidden
        sl_prem = round(premium * (1 - OPTION_SL_PCT / 100), 2)
        tgt_prem = round(premium * (1 + OPTION_SL_PCT / 100 * self.s.risk.min_reward_risk), 2)
        risk = (premium - sl_prem) * lot * lots
        warnings = [v.reason for v in comp.votes if v.direction == -comp.direction]
        if risk > risk_per_trade:
            warnings.append(f"⚠ option risk ₹{risk:,.0f} is {risk / amount * 100:.1f}% of your amount "
                            f"— above the {self.s.risk.max_risk_per_trade_pct}% rule. Options are "
                            "lumpy; only take this if you accept that.")
        return {
            "name": f"{comp.name} {atm['strike_price']:.0f} {want}",
            "symbol": atm.get("symbol", ""), "side": "BUY",
            "instrument": "option", "underlying": comp.name,
            "strike": atm["strike_price"], "option_type": want, "expiry": expiry,
            "conviction": round(comp.score, 2), "ltp": premium,
            "qty": lots * lot, "lots": lots, "lot_size": lot,
            "entry": premium, "stop_loss": sl_prem, "target": tgt_prem,
            "reward_risk": self.s.risk.min_reward_risk,
            "capital_required": round(lots * per_lot_cost, 2),
            "pct_of_amount": round(lots * per_lot_cost / amount * 100, 1),
            "max_loss_if_sl": round(-risk, 2),
            "profit_if_target": round((tgt_prem - premium) * lot * lots, 2),
            "reasons": [f"{comp.name} spot {spot:,.1f} — {comp.label()} {comp.score:.0%}"]
                       + [v.reason for v in comp.votes if v.direction == comp.direction][:2],
            "warnings": warnings,
            "note": (f"real premium ₹{premium} × lot {lot} · exit if premium falls "
                     f"{OPTION_SL_PCT:.0f}% (₹{sl_prem}) · option OI {atm.get('oi', 'n/a')}"),
        }

    def build(self, amount: float, signals: list[Composite],
              sentiment=None, guardian=None) -> dict:
        amount = float(amount)
        if amount <= 0:
            return {"error": "Amount must be positive."}

        risk_per_trade = amount * self.s.risk.max_risk_per_trade_pct / 100
        investable = amount * (1 - CASH_BUFFER_PCT / 100)
        max_picks = self.s.risk.max_open_positions

        candidates = sorted(
            (c for c in signals if c.direction != 0 and c.plan and c.score >= MIN_SCORE),
            key=lambda c: c.score, reverse=True,
        )

        picks, skipped = [], []
        cash_left = investable
        for comp in candidates:
            if len(picks) >= max_picks:
                skipped.append({"name": comp.name, "why": f"max {max_picks} concurrent positions"})
                continue
            # options-enabled underlyings → real ATM option contract with the live premium
            if self._has_options.get(comp.symbol):
                opt = self._option_pick(comp, cash_left, risk_per_trade, amount)
                if opt:
                    cash_left -= opt["capital_required"]
                    picks.append(opt)
                    continue
                # no chain data / unaffordable → fall through to unit-based math

            p = comp.plan
            entry, sl, target = p["entry"], p["stop_loss"], p["target"]
            sl_dist = abs(entry - sl)
            if sl_dist <= 0:
                continue
            qty = min(int(risk_per_trade / sl_dist), int(cash_left / entry))
            if qty <= 0:
                if self._is_index.get(comp.symbol):
                    opt = "call" if comp.direction > 0 else "put"
                    budget = min(risk_per_trade * 2, cash_left * 0.5)
                    skipped.append({"name": comp.name, "why": (
                        f"1 index unit costs ₹{entry:,.0f} — more than your remaining cash. "
                        f"For this {comp.label()} view buy an ATM {opt} instead: keep the premium "
                        f"spend under ₹{budget:,.0f} and exit if the premium falls 50%.")})
                else:
                    skipped.append({"name": comp.name,
                                    "why": f"₹{cash_left:,.0f} left can't buy 1 unit @ {entry:,.2f}"})
                continue

            capital = qty * entry
            risk = qty * sl_dist
            reward = qty * abs((target or entry) - entry)
            cash_left -= capital

            side = "BUY" if comp.direction > 0 else "SELL"
            note = ""
            if self._is_index.get(comp.symbol):
                note = ("index — not directly buyable; take exposure via "
                        f"{'calls' if side == 'BUY' else 'puts'} or futures "
                        "(qty shown in index units for tracking)")
            elif side == "SELL":
                note = "short = intraday only (MIS); must cover before square-off"

            picks.append({
                "name": comp.name, "symbol": comp.symbol, "side": side,
                "instrument": "index-units" if self._is_index.get(comp.symbol) else "equity",
                "conviction": round(comp.score, 2), "ltp": comp.snapshot.get("ltp"),
                "qty": qty, "entry": entry, "stop_loss": sl, "target": target,
                "reward_risk": p.get("reward_risk"),
                "capital_required": round(capital, 2),
                "pct_of_amount": round(capital / amount * 100, 1),
                "max_loss_if_sl": round(-risk, 2),
                "profit_if_target": round(reward, 2),
                "reasons": [v.reason for v in comp.votes if v.direction == comp.direction][:3],
                "warnings": [v.reason for v in comp.votes
                             if v.direction == -comp.direction],
                "note": note,
            })

        total_capital = sum(x["capital_required"] for x in picks)
        total_risk = sum(-x["max_loss_if_sl"] for x in picks)
        notes = []
        if guardian is not None:
            gs = guardian.snapshot()
            if gs["kill_switch"]:
                notes.append(f"KILL SWITCH ACTIVE — do not trade: {gs['kill_reason']}")
            if not guardian.market_open():
                notes.append("Market is CLOSED — prices are stale; treat this as prep for the next session.")
            elif guardian.now_ist().time() >= guardian._no_entry_after:
                notes.append(f"Past {self.s.risk.no_entry_after} IST — no fresh intraday entries today.")
        if not picks:
            notes.append("No signal is strong enough right now. The right move is to WAIT — "
                         "keep the cash and let the engine alert you when conviction returns.")

        return {
            "as_of": clock.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
            "amount": amount,
            "risk_per_trade_cap": round(risk_per_trade, 2),
            "sentiment": ({"label": sentiment.label, "bias": round(sentiment.bias, 2)}
                          if sentiment else None),
            "picks": picks,
            "skipped": skipped,
            "summary": {
                "positions": len(picks),
                "capital_deployed": round(total_capital, 2),
                "cash_kept_aside": round(amount - total_capital, 2),
                "worst_case_loss_if_all_sl": round(-total_risk, 2),
                "worst_case_pct": round(total_risk / amount * 100, 2) if amount else 0,
                "profit_if_all_targets": round(sum(x["profit_if_target"] for x in picks), 2),
            },
            "notes": notes,
        }
