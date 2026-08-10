"""Transaction cost engine — ONE model, used everywhere (master prompt §8).

Every rupee of friction between a gross and a net P&L is computed here: the
paper broker, the allocator, the backtester and the wallet all call this same
function. There are deliberately no duplicate cost formulas anywhere else in
the codebase; `tests/test_costs.py` pins the arithmetic.

⚠️ RATES MUST BE VERIFIED AGAINST YOUR OWN CONTRACT NOTE.
The defaults below reflect the commonly published NSE/BSE structure for a
discount broker (flat ₹20/order). Statutory rates change — STT on option
sales and the exchange transaction charge have both been revised in recent
years. `config.yaml -> costs:` overrides every value without touching code.
Treat a mismatch with your contract note as a defect, not a rounding error.

Charge structure modelled (Indian equity & F&O):
  brokerage        flat per executed order, capped as a % of turnover
  STT/CTT          securities transaction tax — asymmetric (sell-side heavy)
  exchange txn     NSE/BSE transaction charge on turnover (premium for options)
  SEBI turnover    ₹10 per crore of turnover
  stamp duty       buy side only
  GST              18% on (brokerage + exchange txn + SEBI)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Segment(str, Enum):
    EQUITY_INTRADAY = "equity_intraday"
    EQUITY_DELIVERY = "equity_delivery"
    FUT = "futures"
    OPT = "options"


@dataclass(frozen=True)
class SegmentRates:
    """All rates are fractions of turnover unless the name says otherwise."""

    brokerage_flat: float = 20.0        # ₹ per executed order
    brokerage_pct: float = 0.0          # % of turnover (whichever is LOWER applies)
    stt_buy: float = 0.0
    stt_sell: float = 0.0
    exchange_txn: float = 0.0
    sebi_turnover: float = 0.000001     # ₹10 per crore
    stamp_duty_buy: float = 0.0
    gst_pct: float = 0.18


# Published structure as of the 2025-26 financial year. VERIFY before live use.
DEFAULT_RATES: dict[Segment, SegmentRates] = {
    # Options: turnover == premium. STT 0.1% on the SELL side of the premium.
    Segment.OPT: SegmentRates(
        brokerage_flat=20.0, brokerage_pct=0.0,
        stt_buy=0.0, stt_sell=0.001,
        exchange_txn=0.000495,          # NSE F&O options, on premium
        stamp_duty_buy=0.00003,
    ),
    Segment.FUT: SegmentRates(
        brokerage_flat=20.0, brokerage_pct=0.0,
        stt_buy=0.0, stt_sell=0.0002,
        exchange_txn=0.0000173,
        stamp_duty_buy=0.00002,
    ),
    Segment.EQUITY_INTRADAY: SegmentRates(
        brokerage_flat=20.0, brokerage_pct=0.0003,   # ₹20 or 0.03%, lower
        stt_buy=0.0, stt_sell=0.00025,
        exchange_txn=0.0000297,
        stamp_duty_buy=0.00003,
    ),
    Segment.EQUITY_DELIVERY: SegmentRates(
        brokerage_flat=0.0, brokerage_pct=0.0,
        stt_buy=0.001, stt_sell=0.001,
        exchange_txn=0.0000297,
        stamp_duty_buy=0.00015,
    ),
}


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised charges for ONE leg (a single buy or a single sell)."""

    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange_txn
                     + self.sebi + self.stamp_duty + self.gst, 2)

    def __add__(self, other: CostBreakdown) -> CostBreakdown:
        return CostBreakdown(
            brokerage=self.brokerage + other.brokerage,
            stt=self.stt + other.stt,
            exchange_txn=self.exchange_txn + other.exchange_txn,
            sebi=self.sebi + other.sebi,
            stamp_duty=self.stamp_duty + other.stamp_duty,
            gst=self.gst + other.gst,
        )

    def as_dict(self) -> dict:
        return {
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_txn": round(self.exchange_txn, 2),
            "sebi": round(self.sebi, 2),
            "stamp_duty": round(self.stamp_duty, 2),
            "gst": round(self.gst, 2),
            "total": self.total,
        }


class CostModel:
    """Broker/segment-aware charge calculator.

    Instantiate once and share. `overrides` comes straight from config so a
    user on a different brokerage plan never edits Python.
    """

    def __init__(self, overrides: dict | None = None):
        self.rates = dict(DEFAULT_RATES)
        for seg_name, fields in (overrides or {}).items():
            try:
                seg = Segment(seg_name)
            except ValueError:
                raise ValueError(
                    f"unknown cost segment {seg_name!r}; "
                    f"expected one of {[s.value for s in Segment]}"
                ) from None
            known = SegmentRates.__dataclass_fields__
            bad = set(fields) - set(known)
            if bad:
                raise ValueError(f"unknown cost field(s) for {seg_name}: {sorted(bad)}")
            self.rates[seg] = replace(self.rates[seg],
                                      **{k: float(v) for k, v in fields.items()})

    # ── one leg ───────────────────────────────────────────────────────────
    def leg(self, segment: Segment, side: str, price: float, qty: int) -> CostBreakdown:
        """Charges for a single executed leg. `side` is BUY or SELL."""
        if price < 0 or qty < 0:
            raise ValueError("price and qty must be non-negative")
        r = self.rates[segment]
        turnover = price * qty
        if turnover <= 0:
            return CostBreakdown()

        is_buy = side.upper() == "BUY"

        if r.brokerage_pct > 0:
            brokerage = min(r.brokerage_flat, turnover * r.brokerage_pct)
        else:
            brokerage = r.brokerage_flat

        stt = turnover * (r.stt_buy if is_buy else r.stt_sell)
        exchange_txn = turnover * r.exchange_txn
        sebi = turnover * r.sebi_turnover
        stamp = turnover * r.stamp_duty_buy if is_buy else 0.0
        # GST applies to brokerage + exchange charges + SEBI fees. Not to STT/stamp.
        gst = (brokerage + exchange_txn + sebi) * r.gst_pct

        return CostBreakdown(brokerage=brokerage, stt=stt, exchange_txn=exchange_txn,
                             sebi=sebi, stamp_duty=stamp, gst=gst)

    # ── full round trip ───────────────────────────────────────────────────
    def round_trip(self, segment: Segment, side: str, entry: float,
                   exit_price: float, qty: int) -> CostBreakdown:
        """Total charges for entering AND exiting a position.

        `side` is the side of the ENTRY; the exit is the opposite leg.
        """
        entry_side = side.upper()
        exit_side = "SELL" if entry_side == "BUY" else "BUY"
        return (self.leg(segment, entry_side, entry, qty)
                + self.leg(segment, exit_side, exit_price, qty))

    def net_pnl(self, segment: Segment, side: str, entry: float,
                exit_price: float, qty: int) -> tuple[float, float, CostBreakdown]:
        """(gross_pnl, net_pnl, costs) for a completed round trip.

        This is the ONLY place a net P&L is defined. Paper fills, the wallet,
        the journal and the backtester all report the net figure.
        """
        sign = 1 if side.upper() == "BUY" else -1
        gross = (exit_price - entry) * qty * sign
        costs = self.round_trip(segment, side, entry, exit_price, qty)
        return round(gross, 2), round(gross - costs.total, 2), costs

    # ── helpers for sizing / expectancy ───────────────────────────────────
    def breakeven_move(self, segment: Segment, side: str, entry: float, qty: int) -> float:
        """Price move (in points) needed just to cover the round-trip costs.

        Used by the entry-quality gate: a plan whose target does not clear
        this by a sensible margin is not a trade, it's a donation.
        """
        if qty <= 0 or entry <= 0:
            return 0.0
        costs = self.round_trip(segment, side, entry, entry, qty)
        return round(costs.total / qty, 4)


def segment_for_symbol(symbol: str, product: str = "INTRADAY") -> Segment:
    """Classify a Fyers symbol into a cost segment.

    Fyers option symbols end in CE/PE; futures end in FUT. Anything else is
    treated as equity, intraday unless the product says otherwise.
    """
    s = (symbol or "").upper()
    if s.endswith(("CE", "PE")):
        return Segment.OPT
    if s.endswith("FUT"):
        return Segment.FUT
    if product.upper() in ("CNC", "DELIVERY"):
        return Segment.EQUITY_DELIVERY
    return Segment.EQUITY_INTRADAY
