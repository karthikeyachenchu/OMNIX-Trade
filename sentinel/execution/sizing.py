"""PositionSizer — risk-based sizing (master prompt §35).

Replaces `lots = int(cash * 0.98 / (prem * lot))` (engine.py:669, defect D1),
which committed ~98% of the wallet to a single ATM call and, combined with a
25% hard stop, risked ~24.5% of the account on one trade while the README
promised 1%.

Sizing here is driven by the STOP DISTANCE, not by available cash:

    qty = risk_budget / (stop_distance + expected_slippage + cost_per_unit)

then floored to whole lots and clamped by whatever capital is actually free.
If one lot already exceeds the risk budget, the answer is zero lots — not
"round up to one". That is the whole point of the rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import CostModel, Segment


@dataclass(frozen=True)
class SizingResult:
    qty: int
    lots: int
    capital_required: float
    max_loss: float             # at the stop, INCLUDING costs and slippage
    expected_costs: float
    risk_budget: float
    rejected_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.qty > 0

    def as_dict(self) -> dict:
        return {
            "qty": self.qty, "lots": self.lots,
            "capital_required": round(self.capital_required, 2),
            "max_loss": round(self.max_loss, 2),
            "expected_costs": round(self.expected_costs, 2),
            "risk_budget": round(self.risk_budget, 2),
            "rejected_reason": self.rejected_reason,
        }


class PositionSizer:
    def __init__(self, costs: CostModel, slippage_pct: float = 0.001):
        """`slippage_pct` is the assumed adverse fill, as a fraction of price."""
        self.costs = costs
        self.slippage_pct = slippage_pct

    def size(self, *, equity: float, risk_pct: float, entry: float,
             stop_loss: float, lot_size: int, segment: Segment, side: str,
             available_capital: float | None = None,
             margin_per_lot: float | None = None) -> SizingResult:
        """Largest whole-lot quantity whose worst case fits the risk budget.

        `margin_per_lot` covers short/margin products; for long options it is
        simply the premium outlay and can be left None.
        """
        risk_budget = max(0.0, equity * risk_pct / 100.0)
        cash = available_capital if available_capital is not None else equity

        if entry <= 0 or lot_size <= 0:
            return SizingResult(0, 0, 0, 0, 0, risk_budget, "invalid entry or lot size")
        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0:
            return SizingResult(0, 0, 0, 0, 0, risk_budget,
                                "stop-loss equals entry — undefined risk")
        if risk_budget <= 0:
            return SizingResult(0, 0, 0, 0, 0, risk_budget, "no risk budget available")

        # Per-unit loss at the stop = price move + slippage + round-trip costs.
        # breakeven_move() already returns cost per UNIT, so the three terms add.
        slip = entry * self.slippage_pct
        cost_per_unit = self.costs.breakeven_move(segment, side, entry, lot_size)
        loss_per_unit = stop_distance + slip + cost_per_unit
        if loss_per_unit <= 0:
            return SizingResult(0, 0, 0, 0, 0, risk_budget, "non-positive loss per unit")

        raw_qty = int(risk_budget / loss_per_unit)
        lots = raw_qty // lot_size
        if lots < 1:
            need = loss_per_unit * lot_size
            return SizingResult(
                0, 0, 0, 0, 0, risk_budget,
                f"one lot risks ₹{need:,.0f} which exceeds the ₹{risk_budget:,.0f} "
                f"per-trade budget")

        # Clamp by capital actually available.
        per_lot_capital = (margin_per_lot if margin_per_lot is not None
                           else entry * lot_size)
        if per_lot_capital > 0:
            affordable = int(cash / per_lot_capital)
            if affordable < 1:
                return SizingResult(
                    0, 0, 0, 0, 0, risk_budget,
                    f"one lot needs ₹{per_lot_capital:,.0f} but only "
                    f"₹{cash:,.0f} is available")
            lots = min(lots, affordable)

        qty = lots * lot_size
        expected_costs = self.costs.round_trip(segment, side, entry, entry, qty).total
        max_loss = round(loss_per_unit * qty, 2)
        return SizingResult(
            qty=qty, lots=lots,
            capital_required=round(per_lot_capital * lots, 2),
            max_loss=max_loss,
            expected_costs=expected_costs,
            risk_budget=risk_budget,
        )
