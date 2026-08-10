# OMNIX-Trade — Repository Audit

Audit date: **2026-08-10**. Baseline commit: `66e8f46`.
Codebase at audit time: 5,155 lines across 28 Python files + a 1,248-line single-file dashboard.

This document is the Phase 0 deliverable required by master prompt §1. It records what the
system *was* at the baseline commit, every money-moving path, and every defect found —
including nine that the pre-existing audit did not catch.

---

## 1. Module dependency graph

```
main.py
 └── sentinel.config ──────────────► config.yaml + .env
 └── sentinel.broker.fyers.FyersSession
 └── sentinel.engine.TradingEngine
       ├── guardrails.RiskEngine ◄──── THE AUTHORITY
       ├── execution.ExecutionEngine ◄─ THE ONLY ORDER PATH   (added in Phase 1)
       │     ├── execution.intent.OrderIntent   (immutable)
       │     ├── execution.sizing.PositionSizer
       │     ├── execution.costs.CostModel
       │     └── execution.state.OrderStore  ──► journal.db:orders
       ├── broker.paper.PaperBroker  │ token-guarded writes
       ├── broker.fyers.FyersBroker  │
       ├── data.feed  (FyersFeed | YFinanceFeed)
       ├── data.ws.FyersTickStream   (background socket thread)
       ├── data.news.NewsFetcher ──► analysis.sentiment.SentimentEngine
       ├── analysis.strategies ──► analysis.technicals
       ├── tracker.TradeTracker ──► journal.db:tracked_trades
       ├── wallet.Wallet ─────────► journal.db:wallet, wallet_ledger
       ├── journal.Journal ───────► journal.db:events
       ├── allocator.CapitalAllocator   (advisory only, never orders)
       ├── alerts.notifier.Notifier     (toast / beep / ntfy push)
       └── llm.advisor.Advisor ──► llm.tools.ToolExecutor  (READ-ONLY, verified)
dashboard.server.create_app(engine) ──► FastAPI + websocket @ 4 Hz
```

## 2. Execution paths (threads)

| Thread | Cadence | Entry point | What it can do |
|---|---|---|---|
| `engine` | 30 s | `_loop` → `_scan` | full scan, signals, alerts, MTM, square-off |
| `fast-monitor` | 2 s | `_fast_loop` | auto-bot step, tracker review — **only when a Fyers token exists** |
| `fyers-ws` | event | `data.ws._run` | tick ingestion into an in-memory LTP cache |
| advisor | ad-hoc | `_get_advice` | LLM call, writes `last_advice` only |
| notifier ×3 | ad-hoc | toast / beep / ntfy | outbound alerts |
| uvicorn | request | `dashboard.server` | reads snapshot, serves POST endpoints |

**Both `engine` and `fast-monitor` called `_autobot_step()` with no mutual exclusion.**
See defect D15.

## 3. Every place an order or position could be created (baseline)

Search terms: `place_order`, `on_entry`, `tracker.add`, `wallet.on_`, `close_all`,
`close_symbol`, `exit_positions`, `check_entry`.

| # | Location | Routed via broker? | Risk-checked? |
|---|---|---|---|
| 1 | `engine.py:328` `_paper_execute` | ✅ `broker.place_order` | ✅ |
| 2 | `engine.py:472` `_wallet_deploy_best` | ✅ `broker.place_order` | ✅ but **never called** (D4) |
| 3 | `engine.py:684` `_autobot_maybe_enter` | ❌ **writes wallet + tracker directly** | ❌ **none** (D1) |
| 4 | `dashboard/server.py:157` `POST /api/track` | n/a (user's own record) | ❌ unauthenticated (D3) |

Exit paths:

| Location | Feeds `guardian.on_exit()`? |
|---|---|
| `paper.py:86` `_close_locked` | ✅ |
| `engine.py:365` `_enforce_orphan_autos` | ✅ |
| `engine.py:630` `_autobot_exit` → `_settle_auto_exit` | ❌ **never** (D1) |
| `fyers.py:252` `FyersBroker.mark_to_market` returns `[]` | ❌ **never** (D2) |

**Conclusion:** there were two unguarded money paths and one dormant guarded one.
When auto-invest was ON, `_paper_execute` disabled itself (`engine.py:317`) — so
**the only thing trading was the unguarded path.**

## 4. Other audited surfaces

- **Timestamps:** 21 uses of naive `datetime.now()`; market logic correctly IST but journal,
  tracker, re-entry day key and throttles all used machine-local time (D8).
- **Network:** Fyers REST (`fyersModel`), Fyers websocket, `feedparser` RSS, `requests` →
  ntfy, Ollama HTTP. No `subprocess`, no `os.system`, no `eval`, no `exec`, no `pickle`.
- **Credentials:** `.env` (client id, secret, redirect), `.fyers_token` (daily OAuth token).
  Both correctly gitignored. `DASHBOARD_PIN` referenced in code but **absent from `.env`**.
- **DB mutations:** `events`, `tracked_trades`, `wallet`, `wallet_ledger` — no migrations,
  no constraints beyond PKs.
- **Dashboard mutation endpoints:** `/api/wallet`, `/api/wallet/topup`, `/api/wallet/stop`,
  `/api/capital/topup`, `/api/track`, `/api/track/close`, `/api/track/stop`, `/api/chat`.
- **LLM tools:** 10 specs, all read-only. **Verified: no order-placing tool exists.** This
  was the one central safety claim the code actually kept.

---

## 5. DEFECTS

Severity: 🔴 critical · 🟠 major · 🟡 moderate · 🟢 minor.
"Prior" = found by the pre-existing audit. "NEW" = found in this audit.

| ID | Sev | Defect | Source | Status |
|---|---|---|---|---|
| D1 | 🔴 | Auto-bot bypasses every guardrail | Prior | **FIXED** |
| D2 | 🔴 | Kill switch inoperative in LIVE mode | Prior | **PARTIAL** |
| D3 | 🔴 | Dashboard unauthenticated on the LAN | Prior | **FIXED** |
| D4 | 🟠 | `_wallet_deploy_best` dead code | Prior | **FIXED** (removed) |
| D5 | 🟠 | `change_pct` measures 5 days, not today | Prior | **FIXED** |
| D6 | 🟠 | No transaction-cost model | Prior | **FIXED** |
| D7 | 🟡 | `Wallet.stop()` never persists zeroed cash | Prior | **FIXED** |
| D8 | 🟡 | Local time vs IST mixed | Prior | **FIXED** |
| D9 | 🟡 | Mirrored real positions get invented stop-losses | Prior | OPEN |
| D10 | 🟡 | `FyersBroker.positions()` reads `qty`/`avgPrice` not `netQty`/`netAvg` | Prior | **FIXED** |
| D11 | 🟡 | `productType: "BO"` unverified | Prior | **MITIGATED** |
| D12 | 🟢 | Assorted smaller items | Prior | PARTIAL |
| **D13** | 🔴 | **Asymmetric risk accounting corrupts `open_positions`** | **NEW** | **FIXED** |
| **D14** | 🟠 | **`reset_day()` exists but is never called** | **NEW** | **FIXED** |
| **D15** | 🔴 | **Auto-bot race between the 2 s and 30 s loops** | **NEW** | **FIXED** |
| **D16** | 🟡 | **`tracker.review()` mutates `_guidance` unlocked from two threads** | **NEW** | OPEN |
| **D17** | 🟡 | **TOCTOU between wallet cash read and `on_entry`** | **NEW** | **FIXED** |
| **D18** | 🟡 | **`snapshot()` runs on the asyncio event loop at 4 Hz** | **NEW** | OPEN |
| **D19** | 🟠 | **Risk state is memory-only — kill switch dies on restart** | **NEW** | **FIXED** |
| **D20** | 🟡 | **No config validation — nonsense limits start silently** | **NEW** | **FIXED** |
| **D21** | 🟡 | **`_settle_auto_exit` proceeds math is long-only** | **NEW** | OPEN |

### New defects in detail

**D13 — Asymmetric risk accounting (🔴 NEW).**
`_enforce_orphan_autos` (`engine.py:365`) called `guardian.on_exit(pnl)` for auto trades
whose entries had *never* called `guardian.on_entry()` (because D1 skipped it). `on_exit`
decrements `open_positions` — clamped at zero — and adds to `daily_pnl`. So the counter
drifted downward relative to reality while P&L was credited for positions the risk engine
never knew existed. Any system where entry and exit accounting go through different paths
will do this. *Fixed by making the ExecutionEngine the sole source of both callbacks.*

**D14 — `reset_day()` is dead (🟠 NEW).**
Defined at `guardrails.py:175`, called from nowhere (verified by search). A process running
overnight carried yesterday's `trades_today`, `daily_pnl` and kill switch into the new
session. *Fixed: `RiskEngine` now rolls over on the IST trading-day boundary automatically.*

**D15 — Auto-bot race (🔴 NEW).**
`_autobot_step` was invoked from `_fast_loop` (2 s) *and* `_scan` (30 s) on two threads.
`_autobot_maybe_enter` did a check-then-act: read "is an auto position open?", then wrote
wallet + tracker. Two threads could both observe "no position" and both enter. The
`_autobot_last_entry` throttle was itself a non-atomic read-modify-write. *Fixed by a
non-blocking auto-bot lock plus serialised, idempotent submission in the ExecutionEngine.*

**D17 — Wallet TOCTOU (🟡 NEW).**
`cash` was read at the top of `_autobot_maybe_enter` and used to size the position many
lines later, after network calls to the option chain. A concurrent exit could change it in
between. *Fixed: sizing is risk-driven and `wallet.on_entry()` is now called only after the
order has actually filled, and returns False rather than overdrawing.*

**D19 — Risk state is memory-only (🔴 NEW).**
`RiskState` lived only in the process. Restarting the app after a 2% drawdown cleared the
kill switch and restored a full trade budget — the exact moment when *not* trading matters
most. Crash-loop plus auto-restart equals unbounded loss. *Fixed: state persists to
`.risk_state.json` and is reloaded at startup; only a genuine IST day change resets it.*

**D21 — Long-only settlement math (🟡 NEW, open).**
`_settle_auto_exit` computes `proceeds = blocked + pnl`. For a short leg, `blocked` includes
exchange margin, so the arithmetic is only correct for longs. Latent today because the
auto-bot is long-only — it becomes a real bug the moment shorts are enabled (§25).

---

## 6. The authoritative order path (after Phase 1)

```
Strategy / AutonomousController
        │  builds
        ▼
   OrderIntent  (frozen; SL mandatory; idempotency key)
        │
        ▼
ExecutionEngine.submit()   ── single RLock, serialises all callers
        │
        ├── duplicate check ──────────────► refused
        ├── RiskEngine.approve(intent) ───► refused, with a named reason
        │
        ▼
   Broker.execute(intent, _token=...)   ← token-guarded; raises without it
        │
        ├── ok    ──► Fill ──► RiskEngine.on_entry()  ──► OrderStore
        ├── error ──► status UNKNOWN, no retry ─────────► OrderStore
        └── reject ─► status REJECTED ──────────────────► OrderStore
```

There is now exactly **one** function in the codebase that calls a broker write API, and it
cannot be reached without an approved intent. `tests/test_no_unsafe_execution_path.py`
enforces this three ways: runtime token refusal, a spy proving `approve()` is consulted,
and a static AST scan of the autonomous entry functions.

---

## 7. Test status

114 tests, all passing. Coverage of the safety-critical modules:

| Module | Coverage |
|---|---|
| `execution/state.py` | 99% |
| `execution/costs.py` | 97% |
| `execution/intent.py` | 97% |
| `broker/paper.py` | 91% |
| `execution/sizing.py` | 90% |
| `clock.py` | 84% |
| `guardrails.py` | 84% |
| `wallet.py` | 83% |
| `execution/engine.py` | 76% |
| **Total** | **88%** |

Short of the 90% target in §2. The gap is concentrated in `execution/engine.py`
(reconciliation and close-path branches that need a broker fixture) and `guardrails.py`
(the legacy `check_entry` shim and top-up branches).

---

## 8. Live-readiness

**NOT READY for live trading.** Paper-ready, with the caveats below.

What now holds: one execution path, deterministic risk approval on every order, a kill
switch that survives restart, realistic paper fills with costs, duplicate-order protection,
and a config validator that refuses unsafe limits.

What does not yet exist: position reconciliation against the broker (§11), restart recovery
of open orders (§38), the backtester (§30), regime detection (§21), the options engine
(§22), expiry safety (§23), and the live-trading checklist gate (§80). D2 is only partially
fixed — live realised P&L still needs to be polled back into the risk engine before the
kill switch is trustworthy in live mode.
