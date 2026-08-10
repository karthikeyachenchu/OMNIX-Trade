# OMNIX-Trade — Master Context & Improvement Prompt

> Paste this whole document as the opening prompt for any AI coding agent working on
> OMNIX-Trade. It contains the full architecture, the verified current state, every
> defect found in a complete code audit, and the prioritised work plan.
> Audit date: **2026-08-10**. Every file/line reference below was read, not inferred.

---

## 1. What you are working on

**OMNIX-Trade** is a real-time trading advisor and semi-autonomous execution engine for
Indian markets (NSE/BSE index options and F&O equities). It is a single-user desktop
application: a Python engine + FastAPI dashboard, all running locally on Windows, with a
local LLM (Ollama) as the analytical voice.

- **Product name:** OMNIX-Trade (`main.py:34`, `dashboard/server.py:61`, ntfy topic `omnix-trade-karth-x9k42v`)
- **Folder on disk:** `C:\Users\karth\trade-sentinel` — the directory is named after the
  original project name "Trade Sentinel". Internal Python package is `sentinel/`.
  **Do not rename** the package without a full import sweep; do align user-facing strings on OMNIX-Trade.
- **Scale:** ~4,900 lines of Python across 26 modules + a 67 KB single-file dashboard.
- **Not a git repository.** No version control, no tests, no CI.

The core promise stated in `README.md`: *"This is an advisor and alert system, not a money
printer… losses are capped in code: 2% daily drawdown kill-switch, 1% max risk per trade,
mandatory stop-loss on every order — enforced in Python below the LLM, which cannot
override them."*

**Section 6 documents the ways that promise is currently not kept.** Restoring it is the
highest-priority work.

---

## 2. How to run it

```powershell
cd C:\Users\karth\trade-sentinel
pip install -r requirements.txt
ollama create trade-sentinel -f Modelfile     # one-time: builds the trader persona
python main.py                                 # engine + dashboard, paper mode
```

Dashboard: `http://127.0.0.1:8080` (auto-opens).

| Command | Effect |
|---|---|
| `python main.py` | Engine + dashboard + alerts |
| `python main.py --login` | Daily Fyers OAuth (tokens expire every day) |
| `python main.py --once` | One scan cycle, print signals, exit — the smoke test |
| `python main.py --no-dashboard` | Console only |
| `python main.py -v` | Debug logging |

**Environment verified on this machine:** Ollama 0.31.2 running, `trade-sentinel:latest`
(4.9 GB) built and present, plus `llama3.1:8b` fallback. Fyers credentials are present in
`.env`; `LIVE_TRADING_CONFIRMED` is **NO**, so the system is correctly in paper mode.

---

## 3. Architecture

```
main.py                     entry point: arg parsing, wiring, uvicorn boot
config.yaml                 ALL user-tunable settings
Modelfile                   Ollama persona "Sentinel" (llama3.1:8b, temp 0.2, ctx 8192)
journal.db                  SQLite: events, tracked_trades, wallet, wallet_ledger

sentinel/
  config.py       (4.0K)   YAML+.env → frozen RiskLimits, Settings, WatchItem dataclasses
  engine.py      (46.8K)   ★ TradingEngine — the orchestration core. Read this first.
  guardrails.py   (7.7K)   RiskGuardian — the hard safety layer
  tracker.py     (10.8K)   TradeTracker — trades the USER took; emits live guidance
  wallet.py       (7.0K)   Wallet — auto-invest money pot + audit ledger
  allocator.py   (10.4K)   CapitalAllocator — "I have ₹X" → risk-sized plan (pure, no orders)
  journal.py      (2.2K)   Append-only SQLite event log
  analysis/
    technicals.py (4.0K)   EMA, RSI, MACD, ATR, session VWAP, Bollinger, Supertrend
    strategies.py (6.8K)   7 voting strategies → weighted Composite + ATR trade plan
    sentiment.py  (3.7K)   FinBERT (GPU) with VADER+finance-lexicon fallback
  data/
    feed.py       (4.4K)   FyersFeed (live) / YFinanceFeed (delayed) behind one interface
    ws.py         (5.8K)   FyersTickStream — websocket LTP cache, ~2s position monitoring
    news.py       (2.3K)   RSS: MoneyControl ×2, Economic Times, LiveMint
  broker/
    base.py       (1.1K)   Broker ABC + Position/Fill dataclasses
    paper.py      (3.8K)   PaperBroker — default; simulated fills, SL/target enforcement
    fyers.py     (10.5K)   FyersSession (OAuth), FyersData (quotes/history/chain/account),
                           FyersBroker (live orders, triple-gated)
  llm/
    advisor.py    (4.7K)   Ollama tool-calling loop, max 4 rounds
    tools.py     (10.6K)   10 READ-ONLY tools. There is deliberately no place_order tool.
  alerts/
    notifier.py   (3.3K)   Rich console + Windows toast + winsound tones + ntfy.sh push
dashboard/
  server.py      (11.3K)   FastAPI: websocket @4Hz + 16 REST endpoints + optional PIN gate
  static/index.html (67K)  Single-file UI: 9 tabs, custom canvas charting, no framework
.claude/agents/            4 project subagents: market-analyst, risk-auditor,
                           strategy-lab, fyers-integrator
```

### 3.1 The scan loop (`engine.py:192` `_scan`, every 30 s)

1. Refresh news sentiment on its own 10-min timer
2. For each watchlist symbol: fetch candles → `strategies.evaluate()` → `Composite`
3. `_maybe_alert()` — alert only on a **direction change** at score ≥ threshold
4. `broker.mark_to_market(quotes)` — enforce SL/target on paper positions
5. `_sync_fyers_account()` — mirror the user's REAL Fyers book (read-only)
6. Fetch LTPs for tracked off-watchlist symbols (options), overlay socket ticks
7. `_enforce_orphan_autos()` — SL/target for auto trades the broker forgot after a restart
8. `tracker.review()` — guidance for the user's own trades
9. `_check_reentries()` — stop-out recovery
10. `_autobot_step()` — fallback tick for the auto-bot
11. Square-off discipline at `15:12` IST

### 3.2 The fast loop (`engine.py:150` `_fast_loop`, every 2 s)

Runs only when a live Fyers token exists. Uses websocket LTPs so an option SL/target is
caught in ~2 s instead of up to 30 s. Drives `_autobot_step()` as the **primary** auto-bot
clock, and re-runs `tracker.review()`. Alerts are de-duplicated by the tracker, so overlap
with `_scan` is harmless.

### 3.3 The three money paths — this is the part people get wrong

| Path | Trigger | Routes through broker? | Guardrails applied? |
|---|---|---|---|
| **Signal → paper trade** (`_paper_execute`, `engine.py:313`) | strong composite signal | ✅ `broker.place_order` | ✅ full |
| **Auto-bot / wallet** (`_autobot_maybe_enter`, `engine.py:636`) | focus index trending up, ~2 s cadence | ❌ **never** — writes wallet+tracker directly | ❌ **none** |
| **Allocator** (`allocator.py`) | user asks "I have ₹X" | ❌ pure computation, never orders | advisory only |

`_paper_execute` disables itself entirely when the wallet is active (`engine.py:317`), so
**when auto-invest is ON, the only thing trading is the unguarded path.** See defect D1.

### 3.4 Strategy engine

Seven strategies vote `direction ∈ {-1,0,+1}` with `confidence ∈ [0,1]`:

| Strategy | Weight | Logic |
|---|---|---|
| EMA trend | 1.2 | 9/21 cross + price vs 50EMA |
| Supertrend | 1.2 | (10, 3.0); fresh flip = 0.9 confidence |
| VWAP | 1.0 | session VWAP distance, ±0.15% deadband |
| MACD | 1.0 | histogram sign + fresh cross |
| ORB | 1.0 | 09:15–09:30 opening-range breakout |
| RSI | 0.8 | 14-period, 70/30 reversal + 55/45 momentum zones |
| Bollinger | 0.8 | (20, 2.0) band breaks, squeeze bonus if width < 1% |

Composite: `raw = Σ(direction × confidence × weight) / Σweight`, plus a small sentiment
tilt (`strategies.py:146` — algebraically just `0.06 × bias`, written obscurely).
`direction` fires at `|raw| > 0.12`; `score = min(1, |raw| × 1.6)`; alert threshold `0.55`.
Trade plan is ATR-based: SL `1.5×ATR`, target `2.5×ATR`, R:R 1.67.

### 3.5 Data model (`journal.db`)

```sql
events(id, ts, kind, symbol, direction, score, detail JSON)
  kinds: signal | advice | fill | risk | guidance | wallet | allocation |
         tracked | tracked_close | capital_topup | reentry
tracked_trades(id, opened_at, symbol, name, side, qty, entry, stop_loss, target,
               source, status, closed_at, exit_price, pnl)
  source: manual (user's own) | auto (wallet engine) | fyers (mirrored real position)
wallet(id, created_at, deposited, cash, realized_pnl, status, closed_at)
wallet_ledger(id, wallet_id, ts, kind, symbol, amount, balance_after, note)
  kind: deposit | entry | exit | square_off | withdraw
```

Sidecar JSON state files (all restart-safe, all in project root):
`.auto_blocked.json` (cash blocked per auto trade), `.auto_invest.json` (mode/preference),
`.capital.json` (paper capital top-ups), `.fyers_token` (daily OAuth token).

### 3.6 Dashboard

Single-file, framework-free, 9 tabs: Overview, Signals, Charts (custom canvas
candlesticks), Options (live chain + PCR/OI), Portfolio (real Fyers book), Invest
(allocator + auto-invest wallet), Trades, Assistant (LLM chat), Journal. Websocket pushes
`engine.snapshot()` at **4 Hz**. PWA manifest + inline SVG icon; `/api/phone` returns a
segno QR for LAN access.

### 3.7 Config surface (`config.yaml`)

- `mode: paper|live`, `data_source: auto|fyers|yfinance`, `capital: 100000`
- **11 watchlist symbols** with Fyers+yfinance symbol pairs and lot sizes: NIFTY 50 (65),
  BANKNIFTY (30), SENSEX (20), FINNIFTY (60), MIDCPNIFTY (120), BSE (200), MCX (225),
  CDSL (475), ANGELONE (2500), RELIANCE (500), HDFCBANK (650)
- `auto_invest.focus: ["NIFTY 50", "SENSEX"]`
- `risk:` 2% daily drawdown, 1% per trade, 3 open, 6 trades/day, 2-loss→45 min cooldown,
  no entry after 14:30, square-off 15:12, min R:R 1.5
- `engine:` 30 s scan, 5m candles, 5-day lookback, 0.55 threshold
- `dashboard:` host **0.0.0.0**, port 8080

Engine constants that are **hard-coded, not in config** (`engine.py:38–55`) — these should
move to `config.yaml`: `MAX_REENTRIES_PER_DAY=2`, `REENTRY_COOLDOWN_SEC=120`,
`OPTION_SL_PCT=0.40`, `OPTION_SL_FLOOR=0.10`, `STRONG_SCORE=0.70`,
`SHORT_MARGIN_PCT=0.15`, `AUTOBOT_TARGET_PCT=0.30`, `AUTOBOT_HARD_SL_PCT=0.25`,
`AUTOBOT_ARM_PROFIT=0.06`, `AUTOBOT_TRAIL_PCT=0.10`, `AUTOBOT_ENTRY_COOLDOWN=6.0`.

---

## 4. What actually works (verified)

- Seven-strategy composite engine with clean separation and honest confidence reporting
- The `RiskGuardian` gate itself is well written: frozen dataclass limits, thread-safe,
  mandatory-SL validation, SL-direction sanity checks, R:R floor, lot-aware sizing
- Paper broker mirrors live behaviour through the same gate
- Read-only-by-construction LLM tool layer — there is genuinely no order-placing tool
- Real Fyers account mirroring (positions/holdings/funds) is strictly read-only
- Websocket tick stream with graceful REST fallback
- Wallet double-entry ledger — every rupee movement is a row
- Stop-out re-entry logic ("the chart turned back") with per-symbol daily caps
- Alert fan-out: console + Windows toast + tones + phone push via ntfy

### Usage history in `journal.db` (2026-07-09 → 2026-07-14)

478 events: 172 risk, 143 signal, 53 advice, 48 guidance, 22 fill, 19 wallet, 11
allocation. 9 wallet sessions (₹50,000 down to ₹2,000; one still ACTIVE with ₹2,500).
6 tracked trades.

**The most important number here:** all **4 closed auto trades have P&L of exactly 0.00**.
Every one settled flat through the `WALLET-STOP` path — meaning the auto-bot's entry/exit
logic has **never completed a single real round trip**. Only 1 manual trade ever closed
(+₹140.40). Treat the auto-bot as unproven code, not as a working feature.

---

## 5. Known-good invariants — preserve these

1. **The LLM never gets a write path.** No `place_order` tool, ever. It advises; Python decides.
2. **Mandatory stop-loss.** An order without a valid SL is rejected (`guardrails.py:109`).
3. **Triple gate for live trading:** `mode: live` + `LIVE_TRADING_CONFIRMED=YES` + per-order
   `check_entry()`. `FyersBroker.__init__` raises if the first two aren't both set.
4. **Real Fyers account access is read-only.** `FyersData.positions/holdings/funds` never write.
5. **Paper == live minus money.** Both brokers route through the identical guardian gate.
6. **The wallet ledger balances.** Every entry/exit/deposit/withdrawal is a signed row with
   `balance_after`.
7. **Secrets stay out of the repo.** `.gitignore` covers `.env`, `.fyers_token`, `*.log`, `journal.db`.

---

## 6. DEFECTS — audit findings, ordered by severity

### 🔴 D1 — The auto-bot bypasses every guardrail (CRITICAL)

`_autobot_maybe_enter` (`engine.py:636–696`) opens positions by calling `wallet.on_entry()`
and `tracker.add()` **directly**. It never calls `broker.place_order()`, therefore never
`RiskGuardian.check_entry()`, never `guardian.on_entry()`. Exits (`_autobot_exit`,
`engine.py:630` → `_settle_auto_exit`, `engine.py:562`) call `wallet.on_exit()` and
`tracker.close()` but **never `guardian.on_exit()`**.

Consequences — for the only path that trades when auto-invest is ON:
- The 2% daily-drawdown **kill-switch can never trigger** (`daily_pnl` stays 0.0)
- `max_trades_per_day` (6), `max_open_positions` (3), `consecutive_losses_pause`,
  `cooldown_minutes` and `no_entry_after` are all **inert**
- The 1% per-trade risk cap is **not applied**. Instead `engine.py:669` sizes with
  `lots = int(cash * 0.98 / (prem * lot))` — it commits **~98% of the wallet to a single
  ATM call** — and `AUTOBOT_HARD_SL_PCT = 0.25` (`engine.py:48`) means one trade can lose
  **~24.5% of the entire wallet**. The README promises 1%.

This single defect voids the product's central safety claim. Fix by routing auto-bot
entries and exits through `broker.place_order()` / `guardian.on_exit()`, or by giving the
wallet its own `RiskGuardian` instance sized to the wallet — but it must be a real gate.

### 🔴 D2 — The kill-switch is inoperative in LIVE mode (CRITICAL)

`FyersBroker.mark_to_market()` returns `[]` unconditionally (`fyers.py:252`) because
exchange-side bracket legs handle SL/target. But that means **nothing ever calls
`guardian.on_exit()` in live mode**. `daily_pnl` stays 0.0 forever, so the 2% drawdown
kill-switch never fires, the consecutive-loss cooldown never arms, and `open_positions`
only ever increments. Live trading is the mode with the least working risk enforcement.

Fix: poll Fyers positions/tradebook for realised P&L each scan and feed it into
`guardian.on_exit()`, or reconcile `daily_pnl` directly from the broker's realised figure.

### 🔴 D3 — Dashboard is unauthenticated on the LAN (CRITICAL)

`config.yaml:114` sets `host: 0.0.0.0`. The PIN gate (`server.py:64–80`) only activates if
`settings.dash_pin` is non-empty, which reads `DASHBOARD_PIN` from `.env` — **and that key
is absent from `.env` entirely**. So the dashboard binds to every interface with no auth,
exposing money-moving `POST` endpoints to anyone on the Wi-Fi: `/api/wallet`,
`/api/wallet/topup`, `/api/wallet/stop`, `/api/capital/topup`, `/api/track`,
`/api/track/close`, `/api/track/stop`, plus `/api/chat` (drives the local LLM).

Fix: bind `127.0.0.1` by default; require a PIN before ever binding `0.0.0.0`; refuse to
start on a non-loopback interface without one. Use `secrets.compare_digest` for the check.

### 🟠 D4 — `_wallet_deploy_best` is dead code

`engine.py:443–492` — defined, never called anywhere in the codebase (verified by search).
It is the *better* implementation: it ranks legs by conviction × return-on-blocked-capital,
supports both BUY-the-move and SELL-the-far-side (theta) legs via `_option_leg_candidates`
(`engine.py:383`), respects `max_risk`, and **routes through `broker.place_order()`** —
exactly the guardrail path D1 is missing. Its helper `_option_leg_candidates` is likewise
only reachable from it. Either wire it up as the auto-invest entry path or delete both;
leaving a guarded implementation dormant beside an unguarded one is the worst option.

### 🟠 D5 — `change_pct` is not today's change

`technicals.py:81`: `close.iloc[last] / close.iloc[0] - 1` measures change across the
**entire lookback window (5 days)**, not the session. It is surfaced to the user and the
LLM as today's change (`tools.py:147`, dashboard signal cards). Every percentage the user
reads is wrong. Fix: compute against the current session's first bar (`day_open`), the way
`day_high`/`day_low` already do at `technicals.py:96–97`.

### 🟠 D6 — No transaction-cost model anywhere

Paper P&L is gross: `(exit − entry) × qty`. For NSE options, brokerage + STT + exchange
turnover + SEBI fees + stamp duty + 18% GST are material (roughly ₹20–60 per lot round
trip, plus STT on the sell side). With `AUTOBOT_TARGET_PCT = 0.30` on small premiums,
costs can eat a large share of the edge. Every backtest, journal P&L, wallet valuation and
allocator "profit_if_target" is systematically optimistic. Add a cost model and subtract it
in `PaperBroker._close_locked`, the allocator, and the wallet.

### 🟡 D7 — `Wallet.stop()` never persists the zeroed cash

`wallet.py:102–115`: sets `self._cash = 0.0` and writes a ledger row, but never calls
`_persist()`. The `wallet` row keeps its stale `cash`. Live evidence: wallet id=6 is
`STOPPED` with `cash = 560.60` and `deposited = 2000`. Harmless today only because stopped
wallets are never reloaded — but it corrupts any future P&L reporting over wallet history.

### 🟡 D8 — Local time vs IST are mixed

Market logic is correctly IST (`guardrails.py:16,57`). But `datetime.now()` (machine-local)
is used for journal timestamps, `tracked_trades.opened_at`, the re-entry day key
(`engine.py:528`), auto-bot throttles, and `last_scan`. On a machine not set to IST, the
daily re-entry counter rolls over at the wrong moment and journal times won't line up with
market times. Make one IST-aware `now()` helper and use it everywhere.

### 🟡 D9 — Mirrored real positions get invented stop-losses

`tracker.sync_real()` (`tracker.py:117`) assigns every mirrored Fyers position a default
`sl_pct=0.35` / `tgt_pct=0.40`. `review()` then emits *"🛑 STOP-LOSS HIT … Exit NOW"*
(`tracker.py:215`) against levels the user never chose, with full toast + phone-push
urgency. Either mark these as provisional and suppress SL/target guidance until the user
confirms levels, or derive them from the actual order's SL if Fyers exposes it.

### 🟡 D10 — `FyersBroker.positions()` reads the wrong field

`fyers.py:241–242` filters on `p.get("qty", 0)` and reads `p["symbol"]`/`p["avgPrice"]`,
while the correct normalisation right above it in `FyersData.positions()` (`fyers.py:143`)
uses `netQty` / `netAvg`. Fyers v3 returns `netQty`, so the broker's `positions()` returns
an empty list (or raises on `avgPrice`). In live mode `engine.snapshot()["positions"]` is
therefore always empty. Reuse `FyersData.positions()`.

### 🟡 D11 — `productType: "BO"` needs verification

`fyers.py:223` places live orders as bracket orders. Indian exchanges restricted/withdrew
BO products; Fyers API v3 may reject or have migrated this. Verify against the current
Fyers API docs before any live use, and have a fallback (`INTRADAY` + separate SL order).

### 🟢 D12 — Smaller items

- `requests` is imported in `notifier.py:76` but not declared in `requirements.txt`
  (currently satisfied transitively — fragile).
- Fyers token freshness is checked only at startup (`main.py:47`, `engine.py:72`); a
  session running past midnight degrades into silent `log.warning`s and empty candles
  rather than a visible "please re-login" state in the UI.
- `_last_alert_dir` (`engine.py:97`) is never cleared at day rollover, so a signal that
  persisted overnight won't re-alert in the morning.
- Nearest expiry is always taken (`allocator.py:47`, `engine.py:660`) with no 0-DTE guard —
  on expiry day the bot buys options that can go to zero within hours.
- `Journal.recent()` (`journal.py:65`) merges the JSON detail blob over the row dict, so a
  detail key named `ts`/`kind`/`symbol` would silently overwrite the real column.
- The auto-bot is **long-only** — it only ever buys CE (`engine.py:663–667`), despite
  comments saying "owns CE/PE". In a downtrend it simply sits in cash.
- No tests, no git, no CI. A 4,900-line system that moves money has zero automated checks.

---

## 7. Work plan

### Phase 0 — Foundation (do this before touching behaviour)
1. `git init`, commit the current state as a baseline, add the existing `.gitignore`.
2. Add `pytest` + a `tests/` package. Minimum first suite:
   - `RiskGuardian`: every rejection branch, sizing math, kill-switch arming, cooldown
   - `PaperBroker`: SL/target/square-off fills, P&L signs for BUY and SELL
   - `Wallet`: ledger conservation — deposits − withdrawals == Σ signed movements
   - `strategies.evaluate`: golden-file composites from fixed candle fixtures
   - `technicals`: each indicator against hand-computed values
3. Freeze a candle fixture set so tests never touch the network.

### Phase 1 — Restore the safety promise (D1, D2, D3)
4. Route **all** auto-bot entries/exits through `broker.place_order()` and
   `guardian.on_exit()`; or give the wallet its own guardian. Add a regression test that
   asserts no code path creates a position without a `check_entry()` call.
5. Feed live realised P&L back into the guardian so the kill-switch works in live mode.
6. Bind loopback by default; require `DASHBOARD_PIN` before binding `0.0.0.0`;
   constant-time comparison; document it in the README.
7. Cap auto-bot position size by the per-trade risk rule instead of `cash × 0.98`.

### Phase 2 — Correctness (D4–D11)
8. Decide `_wallet_deploy_best`: wire it in as the guarded auto-invest path, or delete it.
9. Fix `change_pct` to session-relative.
10. Add the transaction-cost model (brokerage, STT, exchange, SEBI, stamp, GST) and apply
    it to paper fills, allocator projections and wallet valuation.
11. Fix `Wallet.stop()` persistence; add a ledger-reconciliation test.
12. Single IST-aware clock helper, used everywhere.
13. Provisional-SL handling for mirrored Fyers positions.
14. Fix `FyersBroker.positions()`; verify the `BO` product type.

### Phase 3 — Make the README true
15. Rewrite `README.md` — it currently omits `allocator.py`, `tracker.py`, `wallet.py` and
    `data/ws.py`, and describes safety guarantees that D1/D2 break. Add an architecture
    diagram and an honest "what is and isn't enforced" table.
16. Move the hard-coded engine constants (§3.7) into `config.yaml`.

### Phase 4 — New capability (only after Phases 0–2)
17. **Backtesting harness.** The single highest-value addition: replay historical candles
    through `strategies.evaluate` + the guardian + a cost model, and report per-strategy
    hit rate, expectancy, max drawdown, and R-multiple distribution. Nothing about this
    system's edge is currently measurable.
18. **Strategy-weight learning from `journal.db`.** The README already suggests tuning
    `WEIGHTS` by hand from the journal — automate it with walk-forward validation.
19. **Use the option-chain data already being fetched.** PCR and OI are pulled and
    displayed but feed no signal. Add OI-change and PCR-extreme strategies.
20. **Expiry-aware option selection:** DTE filter, 0-DTE guard, liquidity floor (OI/volume
    minimums), bid-ask spread check before entry.
21. **IV / Greeks:** at minimum IV rank for buy-vs-sell decisions — the Modelfile persona
    already claims to weigh "theta/IV" but no IV data reaches the model.
22. **Multi-timeframe confirmation** (5m signal must agree with 15m/1h trend).
23. **Partial exits / scaling out** — currently every position is all-or-nothing.
24. **Post-trade analytics tab:** equity curve, per-strategy attribution, MAE/MFE.

---

## 8. Constraints for whoever does the work

- **Windows-first.** PowerShell, `winsound`, `plyer` toasts. Keep it working there.
- **Local-only LLM.** Ollama, no cloud model calls, no API keys beyond Fyers.
- **Never widen the LLM's authority.** Read-only tools only.
- **Paper is the default and must stay the default.** Never flip `mode: live` or
  `LIVE_TRADING_CONFIRMED` as a side effect of any change.
- **Don't commit `.env`, `.fyers_token`, or `journal.db`.**
- **Real-account calls stay read-only** unless the user explicitly asks for live execution
  work, and even then the triple gate stays.
- Match the existing style: `from __future__ import annotations`, dataclasses, type hints,
  module docstrings that explain *why*, `# ── section ──` comment banners, threading with
  explicit locks.
- When you change risk behaviour, **say so loudly** in the response and update the README
  table in the same change.

## 9. Definition of done

A change is complete when: tests cover the new behaviour and pass; `python main.py --once`
runs clean; the dashboard loads with no console errors; the README reflects reality; and —
for anything touching money or risk — you can name the guardrail that stops the worst case,
and point at the test that proves it.
