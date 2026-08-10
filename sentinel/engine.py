"""TradingEngine — the orchestration loop.

Every scan_interval_sec:
  1. Pull candles for each watchlist symbol (Fyers live / yfinance fallback)
  2. Compute technicals + run all strategies → composite signal
  3. Refresh news sentiment on its own timer
  4. On a strong NEW signal: alert → (optionally) ask the LLM for a plan →
     size the position → route through RiskGuardian → paper/live broker
  5. Mark open positions to market (SL/target enforcement)
  6. Square off everything at the configured time
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
from datetime import datetime

from .alerts.notifier import Notifier
from .allocator import CapitalAllocator
from .analysis import strategies
from .analysis.sentiment import MarketSentiment, SentimentEngine
from .broker.base import Fill
from .broker.paper import PaperBroker
from .config import ROOT, Settings
from .data.feed import build_feed
from .data.news import NewsFetcher
from .guardrails import OrderRequest, RiskGuardian
from .journal import Journal
from .tracker import TradeTracker
from .wallet import Wallet

log = logging.getLogger("sentinel.engine")

MAX_REENTRIES_PER_DAY = 2   # per symbol — stops a choppy day from bleeding SL by SL
REENTRY_COOLDOWN_SEC = 120  # let the dust settle after a stop-out before re-entering

OPTION_SL_PCT = 0.40        # default option SL: premium −40% (long) / +40% (short)
OPTION_SL_FLOOR = 0.10      # skip if the risk cap would force the SL tighter than 10%
STRONG_SCORE = 0.70         # ≥ this → buy the option (momentum); else sell the far side (theta)
SHORT_MARGIN_PCT = 0.15     # rough exchange margin for a short: 15% of notional + premium

# ── auto-bot (wallet) tuning: buy the uptrend, ride it, exit the moment it turns ──
AUTOBOT_TARGET_PCT = 0.30      # book "reasonable profit" at +30% on the premium
AUTOBOT_HARD_SL_PCT = 0.25     # cut a bad entry at −25% (safety floor)
AUTOBOT_ARM_PROFIT = 0.06      # only start trailing once up ≥6% (let it breathe first)
AUTOBOT_TRAIL_PCT = 0.10       # after arming, exit if premium falls 10% from its peak
AUTOBOT_ENTRY_COOLDOWN = 6.0   # seconds between entry attempts (throttles chain calls)
DEFAULT_AUTOBOT_FOCUS = ["NIFTY 50", "SENSEX"]
BLOCKED_FILE = ROOT / ".auto_blocked.json"   # margin/cost blocked per auto trade (restart-safe)
AI_FILE = ROOT / ".auto_invest.json"         # auto-invest mode/preference (survives restarts)
CAPITAL_FILE = ROOT / ".capital.json"        # paper-trading capital top-ups (survives restarts)


class TradingEngine:
    def __init__(self, settings: Settings, fyers_session=None):
        self.s = settings
        self._fyers_session = fyers_session
        self.guardian = RiskGuardian(settings.risk, settings.capital)
        try:  # restore any paper-capital top-ups from previous sessions
            saved = json.loads(CAPITAL_FILE.read_text(encoding="utf-8")).get("capital")
            if saved and float(saved) > 0:
                self.guardian.capital = float(saved)
        except Exception:
            pass
        self.feed = build_feed(settings, fyers_session)
        # real-time tick stream (only when a live Fyers token is present)
        self.ticks = None
        if fyers_session is not None and fyers_session.is_ready():
            from .data.ws import FyersTickStream
            self.ticks = FyersTickStream(fyers_session)
        self.news = NewsFetcher(cache_minutes=settings.sentiment_refresh_min)
        self.sentiment_engine = SentimentEngine()
        self.notifier = Notifier(settings.alert_desktop, settings.alert_sound,
                                 ntfy_topic=settings.ntfy_topic)
        self.journal = Journal()
        self.tracker = TradeTracker()
        self.wallet = Wallet()

        self.broker = self._build_broker(fyers_session)
        self.allocator = CapitalAllocator(settings, chain_fn=getattr(self.feed, "option_chain", None))

        # shared state (read by dashboard + LLM tools)
        self.latest_signals: dict[str, strategies.Composite] = {}
        self.market_sentiment: MarketSentiment | None = None
        self.last_advice: dict[str, str] = {}
        self.status = "starting"
        self.last_scan: str = ""
        self.extra_quotes: dict[str, float] = {}   # LTPs for tracked non-watchlist symbols (options)
        self.fyers_positions: list[dict] = []      # user's REAL open Fyers positions (read-only)
        self.fyers_holdings: list[dict] = []        # user's REAL equity holdings (read-only)
        self.funds: dict = {}                       # available balance / margin (read-only)
        self._account_at = 0.0                      # last account-refresh timestamp
        self._last_alert_dir: dict[str, int] = {}
        self._opt_underlying: dict[str, str] = {}  # option symbol → underlying fyers symbol
        self._auto_blocked: dict[str, float] = {}  # auto symbol → wallet cash blocked (cost/margin)
        self._auto_peak: dict[str, float] = {}     # auto symbol → highest premium seen (trailing exit)
        self._autobot_last_entry = 0.0             # throttle for entry attempts
        self._autobot_broke_at = 0.0               # throttle for "needs top-up" alerts
        try:
            self._auto_blocked = json.loads(BLOCKED_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        # auto-invest mode: "auto" = whichever qualifies, "preference" = wait for picks
        self.ai_mode = "preference" if settings.auto_invest_focus else "auto"
        self.ai_preference: list[str] = list(settings.auto_invest_focus)
        try:
            st = json.loads(AI_FILE.read_text(encoding="utf-8"))
            if st.get("mode") in ("auto", "preference"):
                self.ai_mode = st["mode"]
                self.ai_preference = [str(x) for x in st.get("preference", [])]
        except Exception:
            pass
        self._reentry: dict[str, dict] = {}        # symbol → watch armed by a stop-out
        self._reentry_count: dict[str, int] = {}   # symbol → re-entries taken today
        self._reentry_day = ""
        self._sentiment_at = 0.0
        self._squared_off = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.advisor = None  # attached by main after ToolExecutor is wired

    def _build_broker(self, fyers_session):
        if self.s.mode == "live" and self.s.live_trading_confirmed and fyers_session and fyers_session.is_ready():
            from .broker.fyers import FyersBroker
            log.warning("LIVE trading mode ARMED")
            return FyersBroker(fyers_session, self.guardian, self.s)
        if self.s.mode == "live":
            log.warning("mode: live requested but not fully armed — using PAPER broker")
        return PaperBroker(self.guardian)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self):
        if self.ticks:
            init = [w.fyers for w in self.s.watchlist]
            init += [t.symbol for t in self.tracker.open_trades() if t.symbol]
            self.ticks.start(init)
            threading.Thread(target=self._fast_loop, daemon=True, name="fast-monitor").start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="engine")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.ticks:
            self.ticks.close()

    def _fast_loop(self):
        """Tick-driven position monitoring (~2s), independent of the 30s scan.

        Uses the freshest socket LTPs so a stop-loss / target on a live option
        is caught within a couple of seconds instead of up to 30. Alerts are
        de-duplicated by the tracker, so overlapping with _scan is harmless."""
        while not self._stop.wait(2.0):
            if not (self.ticks and self.ticks.connected):
                continue
            want = [t.symbol for t in self.tracker.open_trades() if t.symbol]
            if want:
                self.ticks.subscribe(want)
            quotes = self.ticks.all()
            if not quotes:
                continue
            # the auto-bot rides/exits positions and redeploys on live ticks
            self._autobot_step(quotes)
            signals_by_symbol = {c.symbol: c for c in self.latest_signals.values()}
            try:
                for trade, msg, level in self.tracker.review(signals_by_symbol, quotes, self.guardian):
                    self.notifier.notify(f"Your trade — {trade.name}", msg, level)
                    self.journal.log("guidance", trade.name, trade.side, 0.0,
                                     message=msg, trade_id=trade.id)
            except Exception:
                log.debug("fast monitor failed", exc_info=True)

    def _loop(self):
        self.status = "running"
        self.notifier.notify("Engine started",
                             f"mode={self.s.mode} · feed={self.feed.name} · "
                             f"{len(self.s.watchlist)} symbols", "info")
        while not self._stop.is_set():
            started = time.time()
            try:
                self._scan()
            except Exception:
                log.error("scan failed:\n%s", traceback.format_exc())
            elapsed = time.time() - started
            self._stop.wait(max(2.0, self.s.scan_interval_sec - elapsed))
        self.status = "stopped"

    # ── one scan cycle ────────────────────────────────────────────────────
    def _scan(self):
        self._refresh_sentiment()
        bias = self.market_sentiment.bias if self.market_sentiment else 0.0

        quotes: dict[str, float] = {}
        for item in self.s.watchlist:
            df = self.feed.candles(item.fyers, self.s.candle_interval, self.s.lookback_days)
            if df is None or len(df) < 60:
                log.warning("insufficient data for %s", item.name)
                continue
            comp = strategies.evaluate(item.fyers, item.name, df, sentiment_bias=bias)
            self.latest_signals[item.name] = comp
            quotes[item.fyers] = comp.snapshot["ltp"]
            self._maybe_alert(item, comp)

        # enforce SL/targets on open paper positions
        for fill in self.broker.mark_to_market(quotes):
            level = "trade" if (fill.pnl or 0) >= 0 else "warning"
            self.notifier.notify(
                f"{fill.reason} hit — {fill.symbol}",
                f"Closed {fill.qty} @ {fill.price:.2f} · P&L ₹{fill.pnl:,.0f}", level)
            self.journal.log("fill", fill.symbol, fill.side, 0.0,
                             price=fill.price, qty=fill.qty, pnl=fill.pnl, reason=fill.reason)
            self._settle_auto_exit(fill)
            if "STOP-LOSS" in (fill.reason or ""):
                # keep watching — dips often reverse; re-enter if the chart turns back
                self.watch_reentry_for(fill.symbol,
                                       "BUY" if fill.side == "SELL" else "SELL", fill.price)

        # mirror the user's REAL Fyers account (read-only) into the tracker
        self._sync_fyers_account(quotes)

        # live premiums for tracked option/off-watchlist symbols
        extra_syms = {t.symbol for t in self.tracker.open_trades() if t.symbol} - set(quotes)
        if extra_syms:
            try:
                self.extra_quotes = self.feed.ltp(list(extra_syms))
            except Exception as e:
                log.debug("extra quotes failed: %s", e)
        else:
            self.extra_quotes = {}
        quotes.update(self.extra_quotes)
        # freshest of all: overlay real-time socket ticks where we have them
        if self.ticks:
            quotes.update({k: v for k, v in self.ticks.all().items() if v})

        # auto trades whose broker position vanished (engine restart) still need
        # SL/target/square-off enforcement — settle them straight from quotes
        self._enforce_orphan_autos(quotes)

        # guidance for the user's own (tracked) trades
        signals_by_symbol = {c.symbol: c for c in self.latest_signals.values()}
        for trade, msg, level in self.tracker.review(signals_by_symbol, quotes, self.guardian):
            self.notifier.notify(f"Your trade — {trade.name}", msg, level)
            self.journal.log("guidance", trade.name, trade.side, 0.0,
                             message=msg, trade_id=trade.id)

        # stop-out recovery: re-enter when the chart turns back in our favour
        self._check_reentries()

        # auto-bot fallback tick (the fast loop is primary; this covers a
        # scan cycle where the socket was briefly down)
        self._autobot_step(quotes)

        # square-off discipline
        if self.guardian.past_square_off() and not self._squared_off and self.broker.positions():
            for fill in self.broker.close_all("SQUARE-OFF") or []:
                self._settle_auto_exit(fill, kind="square_off")
            self._squared_off = True
            self.notifier.notify("Square-off", "All intraday positions closed (time rule)", "warning")

        if not self.guardian.past_square_off():
            self._squared_off = False

        self.last_scan = datetime.now().strftime("%H:%M:%S")

    def _refresh_sentiment(self):
        if time.time() - self._sentiment_at < self.s.sentiment_refresh_min * 60:
            return
        try:
            headlines = self.news.headlines(limit=40)
            self.market_sentiment = self.sentiment_engine.analyze(headlines)
            self._sentiment_at = time.time()
            log.info("sentiment: %s (%.2f) from %d headlines",
                     self.market_sentiment.label, self.market_sentiment.bias,
                     self.market_sentiment.n_headlines)
        except Exception as e:
            log.warning("sentiment refresh failed: %s", e)

    # ── signal → alert → (guarded) trade ─────────────────────────────────
    def _maybe_alert(self, item, comp: strategies.Composite):
        strong = comp.score >= self.s.signal_threshold and comp.direction != 0
        changed = self._last_alert_dir.get(item.name) != comp.direction
        if not (strong and changed):
            return
        self._last_alert_dir[item.name] = comp.direction

        reasons = "; ".join(v.reason for v in comp.votes if v.direction == comp.direction)[:200]
        self.notifier.notify(
            f"{comp.label()} signal — {item.name} ({comp.score:.0%})",
            f"LTP {comp.snapshot['ltp']} · {reasons}", "signal")
        self.journal.log("signal", item.name, comp.label(), comp.score,
                         plan=comp.plan, reasons=reasons)

        # LLM second opinion (non-blocking for the scan loop)
        if self.s.advise_on_signal and self.advisor:
            threading.Thread(target=self._get_advice, args=(item, comp), daemon=True).start()

        # paper-trade the plan through the guardrails
        self._paper_execute(item, comp)

    def _get_advice(self, item, comp):
        try:
            advice = self.advisor.advise_on_signal(comp)
            self.last_advice[item.name] = advice
            self.journal.log("advice", item.name, comp.label(), comp.score, advice=advice)
            first_line = advice.splitlines()[0] if advice else ""
            self.notifier.notify(f"Sentinel on {item.name}", first_line[:200], "info")
        except Exception as e:
            log.warning("advisor failed: %s", e)

    def _paper_execute(self, item, comp):
        # When the auto-invest wallet is active, the continuous auto-bot
        # (_autobot_step, in the fast loop) owns all deployment — the old
        # signal-triggered path is disabled so the two never fight.
        if not comp.plan or self.wallet.active:
            return
        p = comp.plan
        side = "BUY" if comp.direction > 0 else "SELL"
        qty = self.guardian.max_qty_for(p["entry"], p["stop_loss"])
        if qty <= 0:
            return

        order = OrderRequest(symbol=item.fyers, side=side, qty=qty,
                             entry=p["entry"], stop_loss=p["stop_loss"],
                             target=p["target"], tag=f"composite-{comp.score:.2f}")
        ok, msg = self.broker.place_order(order)
        level = "trade" if ok else "warning"
        self.notifier.notify("Order" if ok else "Order blocked", msg, level)
        self.journal.log("fill" if ok else "risk", item.name, side, comp.score, message=msg)

    def _enforce_orphan_autos(self, quotes: dict[str, float]):
        """SL/target discipline for auto trades the broker no longer holds
        (in-memory positions are lost on restart; tracked trades persist)."""
        # while auto-invest is on, the auto-bot manages its own exits (trailing +
        # trend-flip) and keeps the risk-guardian out of it — don't double-handle
        if self.wallet.active:
            return
        held = {p.symbol for p in self.broker.positions()}
        for t in list(self.tracker.open_trades()):
            if t.source != "auto" or t.symbol in held:
                continue
            ltp = quotes.get(t.symbol)
            if not ltp:
                continue
            long = t.side == "BUY"
            hit = None
            if long and ltp <= t.stop_loss:
                hit = ("STOP-LOSS", t.stop_loss)
            elif long and t.target and ltp >= t.target:
                hit = ("TARGET", t.target)
            elif not long and ltp >= t.stop_loss:
                hit = ("STOP-LOSS", t.stop_loss)
            elif not long and t.target and ltp <= t.target:
                hit = ("TARGET", t.target)
            elif self.guardian.past_square_off():
                hit = ("SQUARE-OFF", ltp)
            if not hit:
                continue
            reason, price = hit
            pnl = (price - t.entry) * t.qty * (1 if long else -1)
            fill = Fill(t.symbol, "SELL" if long else "BUY", t.qty, price,
                        datetime.now(), pnl=pnl, reason=reason)
            self.guardian.on_exit(pnl)
            self.notifier.notify(
                f"{reason} hit — {t.name}",
                f"Closed {t.qty} @ {price:.2f} · P&L ₹{pnl:,.0f}",
                "trade" if pnl >= 0 else "warning")
            self.journal.log("fill", t.symbol, fill.side, 0.0,
                             price=price, qty=t.qty, pnl=pnl, reason=reason)
            self._settle_auto_exit(fill, kind="square_off" if reason == "SQUARE-OFF" else "exit")
            if reason == "STOP-LOSS":
                self.watch_reentry_for(t.symbol, t.side, price)

    # ── auto-invest: real option execution on the focus indices ──────────
    def _save_blocked(self):
        try:
            BLOCKED_FILE.write_text(json.dumps(self._auto_blocked), encoding="utf-8")
        except Exception as e:
            log.debug("blocked-file save failed: %s", e)

    def _option_leg_candidates(self, item, comp, cash: float, max_risk: float) -> list[dict]:
        """All tradable ATM legs for one index view: BUY the move (CE/PE) and
        SELL the far side (theta income), each sized to the cash available."""
        chain_fn = getattr(self.feed, "option_chain", None)
        if not chain_fn:
            return []
        try:
            chain = chain_fn(item.fyers, 3) or {}
        except Exception as e:
            log.debug("chain fetch failed for %s: %s", item.name, e)
            return []
        rows = chain.get("optionsChain", [])
        spot = comp.snapshot["ltp"]

        def atm(ot):
            cands = [r for r in rows if r.get("option_type") == ot
                     and r.get("strike_price", 0) > 0 and r.get("ltp", 0) > 0]
            return min(cands, key=lambda r: abs(r["strike_price"] - spot)) if cands else None

        bullish = comp.direction > 0
        lot = max(1, item.lot_size)
        rr = self.s.risk.min_reward_risk
        legs: list[dict] = []
        for action, ot in (("BUY", "CE" if bullish else "PE"),      # own the move
                           ("SELL", "PE" if bullish else "CE")):    # collect theta
            row = atm(ot)
            if row is None:
                continue
            prem = float(row["ltp"])
            blocked_per_lot = (prem * lot if action == "BUY"
                               else prem * lot + SHORT_MARGIN_PCT * row["strike_price"] * lot)
            lots = int(cash * 0.98 / blocked_per_lot)
            if lots <= 0:
                legs.append({"skip": f"{item.name} {action} ATM {ot}: 1 lot needs "
                                     f"₹{blocked_per_lot:,.0f}"})
                continue
            qty = lots * lot
            sl_dist = min(prem * OPTION_SL_PCT, max_risk / qty)
            if action == "SELL":
                sl_dist = min(sl_dist, max(0.0, prem - 0.05) / rr)  # target premium stays > 0
            if sl_dist < prem * OPTION_SL_FLOOR:
                legs.append({"skip": f"{item.name} {action} ATM {ot}: risk cap would force the "
                                     f"SL tighter than {OPTION_SL_FLOOR:.0%} of premium (churn)"})
                continue
            sl = round(prem - sl_dist, 2) if action == "BUY" else round(prem + sl_dist, 2)
            tgt = round(prem + sl_dist * rr, 2) if action == "BUY" else round(prem - sl_dist * rr, 2)
            profit_if_tgt = sl_dist * rr * qty
            blocked = round(blocked_per_lot * lots, 2)
            # momentum favours owning the option; moderate conviction favours theta
            pref = 1.0 if ((action == "BUY") == (comp.score >= STRONG_SCORE)) else 0.85
            legs.append({
                "item": item, "comp": comp, "action": action, "ot": ot,
                "symbol": row.get("symbol", ""), "strike": row["strike_price"],
                "premium": prem, "lot": lot, "lots": lots, "qty": qty,
                "sl": sl, "target": tgt, "blocked": blocked,
                "profit_if_target": round(profit_if_tgt, 2),
                "score": comp.score * pref * (profit_if_tgt / blocked),
            })
        return legs

    def _wallet_deploy_best(self):
        """Rank every affordable option play across the focus indices by
        conviction × return-on-blocked-capital and take the best one."""
        w = self.wallet.state()
        cash = w["cash"]
        max_risk = self.guardian.capital * self.s.risk.max_risk_per_trade_pct / 100
        focus = set(self.s.auto_invest_focus or [x.name for x in self.s.watchlist])
        items = {x.name: x for x in self.s.watchlist}
        cands, skips = [], []
        for name, comp in self.latest_signals.items():
            item = items.get(name)
            if (item is None or name not in focus or not item.fyers.endswith("-INDEX")
                    or comp.direction == 0 or comp.score < self.s.signal_threshold):
                continue
            for leg in self._option_leg_candidates(item, comp, cash, max_risk):
                (skips if "skip" in leg else cands).append(leg)
        for sk in skips:
            self.journal.log("risk", "auto-invest", "", 0.0, message=sk["skip"])
        if not cands:
            if skips:
                self.notifier.notify(
                    "Auto-invest needs a top-up",
                    f"Wallet cash ₹{cash:,.0f} can't afford any focus-index option — "
                    f"cheapest: {skips[0]['skip']}", "info")
            return
        best = max(cands, key=lambda c: c["score"])
        order = OrderRequest(symbol=best["symbol"], side=best["action"], qty=best["qty"],
                             entry=best["premium"], stop_loss=best["sl"],
                             target=best["target"], tag=f"auto-opt-{best['comp'].score:.2f}")
        ok, msg = self.broker.place_order(order)
        self.journal.log("fill" if ok else "risk", best["item"].name, best["action"],
                         best["comp"].score, message=msg,
                         considered=[f"{c['item'].name} {c['action']} {c['ot']} "
                                     f"(score {c['score']:.3f})" for c in cands])
        if not ok:
            self.notifier.notify("Order blocked", msg, "warning")
            return
        name = f"{best['item'].name} {best['strike']:.0f} {best['ot']}"
        self.wallet.on_entry(best["blocked"], best["symbol"],
                             f"{best['action']} {best['lots']} lot {name} @ {best['premium']}")
        self._auto_blocked[best["symbol"]] = best["blocked"]
        self._save_blocked()
        self.tracker.add(best["symbol"], name, best["action"], best["qty"],
                         best["premium"], best["sl"], best["target"], source="auto")
        self._opt_underlying[best["symbol"]] = best["item"].fyers
        self.notifier.notify(
            "Auto-invest — best pick",
            f"{best['action']} {best['lots']}×lot {name} @ ₹{best['premium']} · "
            f"₹{best['blocked']:,.0f} blocked · SL {best['sl']} · target {best['target']} · "
            f"chosen from {len(cands)} candidate leg(s)", "trade")

    # ── stop-out re-entry ─────────────────────────────────────────────────
    def watch_reentry_for(self, symbol: str, side: str, stopped_price: float):
        """Arm a re-entry watch after a stop-out (auto fills and manual closes).
        Option stop-outs are mapped back to their underlying index: the watch
        references the spot at stop-out time and the view the leg expressed."""
        underlying = self._opt_underlying.get(symbol)
        if underlying:
            is_ce = symbol.endswith("CE")
            was_buy = side == "BUY"
            side = "BUY" if was_buy == is_ce else "SELL"   # BUY CE / SELL PE = bullish view
            comp = next((c for c in self.latest_signals.values() if c.symbol == underlying), None)
            if comp is None:
                return
            stopped_price = comp.snapshot["ltp"]
            symbol = underlying
        item = next((w for w in self.s.watchlist if w.fyers == symbol), None)
        if item is None:
            return
        self._reentry[symbol] = {
            "name": item.name, "side": side, "stopped_at": stopped_price,
            "at": time.time(), "ts": datetime.now().strftime("%H:%M:%S"),
        }
        self.notifier.notify(
            f"🔁 Watching {item.name} for re-entry",
            f"Stopped out @ {stopped_price:.2f} — I keep watching; if the chart turns "
            f"{'up' if side == 'BUY' else 'down'} again I re-enter (max "
            f"{MAX_REENTRIES_PER_DAY}×/day per symbol).", "info")

    def _check_reentries(self):
        if not self._reentry:
            return
        if self.guardian.past_square_off():
            self._reentry.clear()
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._reentry_day != today:
            self._reentry_day, self._reentry_count = today, {}
        items = {w.fyers: w for w in self.s.watchlist}
        comps = {c.symbol: c for c in self.latest_signals.values()}
        for sym, watch in list(self._reentry.items()):
            comp, item = comps.get(sym), items.get(sym)
            if comp is None or item is None:
                continue
            if time.time() - watch["at"] < REENTRY_COOLDOWN_SEC:
                continue
            want = 1 if watch["side"] == "BUY" else -1
            ltp = comp.snapshot["ltp"]
            # "graph turned back": price reclaimed the stop level AND the composite
            # agrees with the original direction at full signal strength
            recovered = ltp > watch["stopped_at"] if want > 0 else ltp < watch["stopped_at"]
            if not (recovered and comp.direction == want
                    and comp.score >= self.s.signal_threshold):
                continue
            taken = self._reentry_count.get(sym, 0)
            if taken >= MAX_REENTRIES_PER_DAY:
                del self._reentry[sym]
                continue
            self._reentry_count[sym] = taken + 1
            del self._reentry[sym]
            self.notifier.notify(
                f"🔁 Re-entry — {item.name}",
                f"Recovered past the stop ({watch['stopped_at']:.2f} → {ltp:.2f}) and the "
                f"{comp.label()} signal is back ({comp.score:.0%}). Re-entering "
                f"({taken + 1}/{MAX_REENTRIES_PER_DAY} today).", "trade")
            self.journal.log("reentry", item.name, watch["side"], comp.score,
                             stopped_at=watch["stopped_at"], ltp=ltp)
            self._paper_execute(item, comp)

    def _settle_auto_exit(self, fill, kind: str = "exit"):
        """Credit the wallet + close the tracked auto trade when the broker exits."""
        if not self.wallet.active:
            return
        t = self.tracker.open_auto_by_symbol(fill.symbol)
        if t is None:
            return
        self.tracker.close(t.id, fill.price)
        blocked = self._auto_blocked.pop(fill.symbol, None)
        if blocked is not None:
            self._save_blocked()
        proceeds = (blocked if blocked is not None else t.qty * t.entry) + (fill.pnl or 0.0)
        self.wallet.on_exit(proceeds, fill.pnl or 0.0, fill.symbol,
                            f"{fill.reason} {t.name} @ {fill.price}", kind=kind)
        w = self.wallet.state()
        self.notifier.notify("Auto-invest exit",
                             f"{t.name} closed ({fill.reason}) · P&L ₹{fill.pnl or 0:,.0f} · "
                             f"wallet ₹{w['value']:,.0f}", "trade" if (fill.pnl or 0) >= 0 else "warning")

    # ── auto-bot: continuous NIFTY/SENSEX trend-riding on the wallet ──────
    def _autobot_focus(self) -> list[str]:
        return self.s.auto_invest_focus or DEFAULT_AUTOBOT_FOCUS

    def _index_comp_for(self, option_symbol: str):
        """The underlying index composite for an auto option position."""
        underlying = self._opt_underlying.get(option_symbol)
        if not underlying:
            return None
        return next((c for c in self.latest_signals.values() if c.symbol == underlying), None)

    def _autobot_step(self, quotes: dict[str, float]) -> None:
        """One cycle of the auto-bot, driven by live ticks (called ~every 2s):
        ride open positions and exit the instant the up-move turns, then redeploy
        idle wallet cash the moment a focus index is trending up again."""
        if not self.wallet.active:
            return
        try:
            self._autobot_manage_exits(quotes)
            self._autobot_maybe_enter(quotes)
        except Exception:
            log.debug("autobot step failed", exc_info=True)

    def _autobot_manage_exits(self, quotes: dict[str, float]) -> None:
        for t in [x for x in self.tracker.open_trades() if x.source == "auto"]:
            ltp = quotes.get(t.symbol) or self.extra_quotes.get(t.symbol)
            if not ltp:
                continue
            peak = max(self._auto_peak.get(t.symbol, t.entry), ltp)
            self._auto_peak[t.symbol] = peak
            gain = ltp / t.entry - 1.0            # auto-bot is long-only (owns CE/PE)
            peak_gain = peak / t.entry - 1.0

            reason = None
            if gain >= AUTOBOT_TARGET_PCT:
                reason = f"TARGET +{gain*100:.0f}%"
            elif gain <= -AUTOBOT_HARD_SL_PCT:
                reason = f"STOP-LOSS {gain*100:.0f}%"
            elif peak_gain >= AUTOBOT_ARM_PROFIT and ltp <= peak * (1 - AUTOBOT_TRAIL_PCT):
                reason = f"TREND-EXIT (fell {(1-ltp/peak)*100:.0f}% from peak, locked +{gain*100:.0f}%)"
            elif self.guardian.past_square_off():
                reason = "SQUARE-OFF"
            else:
                comp = self._index_comp_for(t.symbol)
                if comp is not None and comp.direction < 0 and comp.score >= self.s.signal_threshold:
                    reason = "TREND-FLIP (index turned down)"
            if reason:
                self._autobot_exit(t, ltp, reason)

    def _autobot_exit(self, t, price: float, reason: str) -> None:
        pnl = (price - t.entry) * t.qty        # long-only
        fill = Fill(t.symbol, "SELL", t.qty, price, datetime.now(), pnl=pnl, reason=reason)
        self._auto_peak.pop(t.symbol, None)
        self._settle_auto_exit(fill, kind="square_off" if "SQUARE" in reason else "exit")

    def _autobot_maybe_enter(self, quotes: dict[str, float]) -> None:
        if not self.guardian.market_open() or self.guardian.past_square_off():
            return
        # one position at a time — the bot commits the whole wallet to the best index
        if any(x.source == "auto" for x in self.tracker.open_trades()):
            return
        if time.time() - self._autobot_last_entry < AUTOBOT_ENTRY_COOLDOWN:
            return
        cash = self.wallet.state()["cash"]
        if cash < 1:
            return
        items = {x.name: x for x in self.s.watchlist}
        best = None
        for name in self._autobot_focus():
            comp, item = self.latest_signals.get(name), items.get(name)
            if (comp and item and item.fyers.endswith("-INDEX")
                    and comp.direction > 0 and comp.score >= self.s.signal_threshold):
                if best is None or comp.score > best[0].score:
                    best = (comp, item)
        if best is None:
            return  # no focus index trending up → sit in cash and keep observing
        self._autobot_last_entry = time.time()
        comp, item = best
        chain_fn = getattr(self.feed, "option_chain", None)
        chain = (chain_fn(item.fyers, 5) or {}) if chain_fn else {}
        rows = chain.get("optionsChain", [])
        spot = comp.snapshot["ltp"]
        ce = [r for r in rows if r.get("option_type") == "CE"
              and r.get("strike_price", 0) > 0 and r.get("ltp", 0) > 0]
        if not ce:
            return
        row = min(ce, key=lambda r: abs(r["strike_price"] - spot))   # ATM call = ride the up-move
        prem, lot = float(row["ltp"]), max(1, item.lot_size)
        lots = int(cash * 0.98 / (prem * lot))
        if lots <= 0:
            if time.time() - self._autobot_broke_at > 120:   # don't spam every cycle
                self._autobot_broke_at = time.time()
                self.notifier.notify("Auto-bot needs a top-up",
                                     f"Wallet ₹{cash:,.0f} can't afford 1 lot of {item.name} "
                                     f"{row['strike_price']:.0f} CE (needs ₹{prem*lot:,.0f}). Add money to trade.",
                                     "info")
            return
        qty = lots * lot
        cost = round(prem * qty, 2)
        sym = row.get("symbol", "")
        sl = round(prem * (1 - AUTOBOT_HARD_SL_PCT), 2)
        tgt = round(prem * (1 + AUTOBOT_TARGET_PCT), 2)
        name = f"{item.name} {row['strike_price']:.0f} CE"
        if not self.wallet.on_entry(cost, sym, f"BUY {lots} lot {name} @ {prem}"):
            return
        self._auto_blocked[sym] = cost
        self._save_blocked()
        self.tracker.add(sym, name, "BUY", qty, prem, sl, tgt, source="auto")
        self._opt_underlying[sym] = item.fyers
        self._auto_peak[sym] = prem
        if self.ticks:
            self.ticks.subscribe([sym])
        self.notifier.notify(
            "🤖 Auto-bot bought the uptrend",
            f"BUY {lots}×lot {name} @ ₹{prem} · ₹{cost:,.0f} deployed ({item.name} up "
            f"{comp.score:.0%}) · rides up, exits the moment it turns", "trade")

    # ── auto-invest wallet ────────────────────────────────────────────────
    def activate_wallet(self, amount: float) -> dict:
        res = self.wallet.activate(amount)
        if res and "error" not in res:
            self._last_alert_dir.clear()   # re-arm current strong signals so money deploys next scan
            self.journal.log("wallet", "", "", 0.0, action="activate", amount=amount)
            self.notifier.notify("Auto-invest ON",
                                 f"₹{amount:,.0f} under management — deploying on the next scan", "info")
        return res

    def topup_wallet(self, amount: float) -> dict:
        """Add money to the running auto-invest wallet; it deploys next step."""
        try:
            amount = float(str(amount).replace(",", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            return {"error": f"could not parse amount: {amount!r}"}
        res = self.wallet.add_funds(amount)
        if res and "error" not in res:
            self._last_alert_dir.clear()   # re-arm so the fresh cash can deploy
            self.journal.log("wallet", "", "", 0.0, action="topup", amount=amount)
            self.notifier.notify("Auto-invest topped up",
                                 f"+₹{amount:,.0f} → ₹{res['cash']:,.0f} cash ready to deploy", "info")
        return res

    def stop_wallet(self) -> dict:
        # flatten auto positions first so every rupee is back in cash
        for t in [x for x in self.tracker.open_trades() if x.source == "auto"]:
            close_symbol = getattr(self.broker, "close_symbol", None)
            fill = close_symbol(t.symbol, "WALLET-STOP") if close_symbol else None
            if fill:
                self._settle_auto_exit(fill)
            else:   # live broker or position already gone — settle at entry (flat)
                self.tracker.close(t.id, t.entry)
                blocked = self._auto_blocked.pop(t.symbol, None)
                if blocked is not None:
                    self._save_blocked()
                self.wallet.on_exit(blocked if blocked is not None else t.qty * t.entry,
                                    0.0, t.symbol, f"wallet stop {t.name}")
        res = self.wallet.stop()
        self.journal.log("wallet", "", "", 0.0, action="stop", final=res)
        return res

    # ── capital allocation ("I have ₹X — where do I invest?") ────────────
    def allocate(self, amount: float) -> dict:
        plan = self.allocator.build(amount, list(self.latest_signals.values()),
                                    self.market_sentiment, self.guardian)
        if "error" not in plan:
            self.journal.log("allocation", "", "", 0.0, amount=amount,
                             picks=[p["name"] for p in plan["picks"]],
                             summary=plan["summary"])
        return plan

    def _wallet_view(self, ltp_by_symbol: dict) -> dict | None:
        """Wallet state + where the money is deployed, marked to live prices."""
        if not self.wallet.active:
            return None
        w = self.wallet.state()
        holdings = []
        for t in self.tracker.open_trades():
            if t.source != "auto":
                continue
            ltp = ltp_by_symbol.get(t.symbol)
            blocked = self._auto_blocked.get(t.symbol)
            cost = round(blocked if blocked is not None else t.qty * t.entry, 2)
            pnl = round(t.unrealized(ltp), 2) if ltp else None
            holdings.append({
                "symbol": t.symbol, "name": t.name, "side": t.side, "qty": t.qty,
                "entry": t.entry, "ltp": ltp, "cost": cost, "pnl": pnl,
                "pnl_pct": round(pnl / cost * 100, 2) if pnl is not None and cost else None,
            })
        unrealized = round(sum(h["pnl"] or 0.0 for h in holdings), 2)
        w.update(
            holdings=holdings,
            focus=self.s.auto_invest_focus,
            unrealized_pnl=unrealized,
            total_pnl=round(w["realized_pnl"] + unrealized, 2),
            live_value=round(w["cash"] + sum(h["cost"] + (h["pnl"] or 0.0) for h in holdings), 2),
            ledger=self.wallet.ledger(8),
        )
        return w

    # ── paper-trading capital top-up ──────────────────────────────────────
    def top_up_capital(self, amount: float) -> dict:
        """Add funds to the paper-trading capital (persisted across restarts)."""
        try:
            amount = float(str(amount).replace(",", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            return {"ok": False, "error": f"could not parse amount: {amount!r}"}
        if amount <= 0:
            return {"ok": False, "error": "amount must be positive"}
        new_capital = self.guardian.top_up(amount)
        try:
            CAPITAL_FILE.write_text(json.dumps({"capital": new_capital}), encoding="utf-8")
        except Exception as e:
            log.warning("could not persist capital top-up: %s", e)
        self.notifier.notify("💰 Paper capital topped up",
                             f"+₹{amount:,.0f} → ₹{new_capital:,.0f} available", "info")
        self.journal.log("capital_topup", "PAPER", "ADD", 0.0,
                         amount=amount, new_capital=new_capital)
        return {"ok": True, "added": amount, "capital": new_capital,
                "risk": self.guardian.snapshot()}

    # ── real Fyers account sync (read-only) ───────────────────────────────
    def _sync_fyers_account(self, quotes: dict[str, float]) -> None:
        """Pull the user's live positions/holdings/funds from Fyers and mirror
        open positions into the tracker. Purely read-only — never trades."""
        if not getattr(self.feed, "account_live", False):
            return
        try:
            self.fyers_positions = self.feed.positions()
        except Exception as e:
            log.debug("positions sync failed: %s", e)
        # holdings + funds move slowly — refresh at most once a minute
        if time.time() - self._account_at > 60:
            try:
                self.fyers_holdings = self.feed.holdings()
                self.funds = self.feed.funds()
                self._account_at = time.time()
            except Exception as e:
                log.debug("holdings/funds sync failed: %s", e)
        # feed the real position LTPs into the quote map for instant valuation
        for p in self.fyers_positions:
            if p.get("ltp"):
                quotes[p["symbol"]] = p["ltp"]
        try:
            self.tracker.sync_real(self.fyers_positions, quotes)
        except Exception as e:
            log.debug("tracker sync failed: %s", e)

    @staticmethod
    def _tick_fresh_position(p: dict, ltp_by_symbol: dict) -> dict:
        """Re-value a real position against the freshest tick price."""
        px = ltp_by_symbol.get(p["symbol"]) or p.get("ltp")
        if not px:
            return p
        sign = 1 if p["side"] == "BUY" else -1
        q = dict(p)
        q["ltp"] = px
        q["pnl"] = round((px - p["entry"]) * p["qty"] * sign, 2)
        return q

    # ── dashboard snapshot ───────────────────────────────────────────────
    def snapshot(self) -> dict:
        positions = self.broker.positions()
        ltp_by_symbol = {c.symbol: c.snapshot.get("ltp") for c in self.latest_signals.values()}
        ltp_by_symbol.update(self.extra_quotes)
        if self.ticks:
            ltp_by_symbol.update({k: v for k, v in self.ticks.all().items() if v})
        return {
            "status": self.status,
            "mode": self.s.mode if not isinstance(self.broker, PaperBroker) else "paper",
            "feed": self.feed.name,
            "realtime": self.ticks.stats() if self.ticks else {"connected": False},
            "last_scan": self.last_scan,
            "market_open": self.guardian.market_open(),
            "risk": self.guardian.snapshot(),
            "sentiment": ({
                "bias": self.market_sentiment.bias, "label": self.market_sentiment.label,
                "n": self.market_sentiment.n_headlines, "engine": self.market_sentiment.engine,
                "top_positive": self.market_sentiment.top_positive,
                "top_negative": self.market_sentiment.top_negative,
            } if self.market_sentiment else None),
            "signals": [{
                "name": c.name, "label": c.label(), "score": c.score,
                "ltp": c.snapshot["ltp"], "change_pct": c.snapshot["change_pct"],
                "rsi": c.snapshot["rsi14"], "vwap": c.snapshot["vwap"],
                "supertrend": c.snapshot["supertrend"],
                "votes": [{"s": v.strategy, "d": v.direction, "c": round(v.confidence, 2), "r": v.reason}
                          for v in c.votes],
                "plan": c.plan,
                "advice": self.last_advice.get(c.name, ""),
            } for c in self.latest_signals.values()],
            "positions": [{
                "symbol": p.symbol, "side": p.side, "qty": p.qty, "entry": p.entry,
                "ltp": p.ltp, "sl": p.stop_loss, "target": p.target,
                "pnl": round(p.unrealized, 2),
            } for p in positions],
            "account": {
                "live": bool(getattr(self.feed, "account_live", False)),
                "positions": [self._tick_fresh_position(p, ltp_by_symbol)
                              for p in self.fyers_positions],
                "holdings": self.fyers_holdings,
                "funds": self.funds,
            },
            "wallet": self._wallet_view(ltp_by_symbol),
            "reentry": [{"symbol": s, "name": w["name"], "side": w["side"],
                         "stopped_at": w["stopped_at"], "since": w["ts"]}
                        for s, w in self._reentry.items()],
            "tracked": [{
                "id": t.id, "symbol": t.symbol, "name": t.name, "side": t.side,
                "source": t.source,
                "qty": t.qty, "entry": t.entry, "sl": t.stop_loss, "target": t.target,
                "ltp": ltp_by_symbol.get(t.symbol),
                "pnl": round(t.unrealized(ltp_by_symbol[t.symbol]), 2)
                       if ltp_by_symbol.get(t.symbol) else None,
                "opened_at": t.opened_at,
                "guidance": self.tracker.last_guidance(t.id),
            } for t in self.tracker.open_trades()],
            "headlines": [{"title": h.title, "source": h.source, "sentiment": h.sentiment}
                          for h in self.news.headlines(limit=10)] if self._sentiment_at else [],
        }
