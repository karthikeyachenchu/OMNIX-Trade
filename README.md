# ⚡ Trade Sentinel

Real-time trading advisor for Indian markets (NSE options & equities): live
signals from 7 technical strategies, news sentiment analysis, a local LLM
advisor (Ollama, runs entirely on your machine), hard risk guardrails, paper
trading, desktop alerts, and a live web dashboard.

> **This is an advisor and alert system, not a money printer.** No system wins
> every trade. What this one guarantees is that losses are *capped in code*:
> 2% daily drawdown kill-switch, 1% max risk per trade, mandatory stop-loss on
> every order — enforced in Python below the LLM, which cannot override them.

---

## Quick start (works today, no API key needed)

```powershell
cd C:\Users\karth\trade-sentinel
pip install -r requirements.txt
ollama create trade-sentinel -f Modelfile   # one-time: builds the trader persona model
python main.py
```

The dashboard opens at http://127.0.0.1:8080. Without a Fyers key it uses
yfinance data (delayed ~1–15 min) — fine for testing and paper trading.

## Tomorrow: plug in Fyers (live data)

1. Create an app at https://myapi.fyers.in → get **Client ID** and **Secret Key**.
2. `copy .env.example .env` and paste the two values into `.env`.
3. Each trading morning: `python main.py --login` (Fyers tokens expire daily) —
   open the printed URL, log in, paste the `auth_code` back.
4. `python main.py` — it auto-detects the token and switches to live Fyers data.

That's it. Nothing else changes.

## Daily usage

| Command | What it does |
|---|---|
| `python main.py` | Start everything (engine + dashboard + alerts) |
| `python main.py --login` | Morning Fyers login (only when using live data) |
| `python main.py --once` | One scan, print signals to console, exit |
| `python main.py --no-dashboard` | Console-only mode |

On the dashboard: watch the signal cards, read the advisor's take, use the
chat box ("Should I buy NIFTY calls here?", "What's the risk status?"), and
hit **Market briefing** before the open.

## What's inside

```
main.py                  entry point
config.yaml              ← everything you'd want to tweak lives here
Modelfile                LLM trader persona (ollama create trade-sentinel -f Modelfile)
sentinel/
  engine.py              orchestration loop (scan → signal → advise → guard → alert)
  guardrails.py          RiskGuardian — the hard safety layer
  analysis/
    technicals.py        EMA, RSI, MACD, VWAP, ATR, Bollinger, Supertrend
    strategies.py        7 voting strategies → composite signal + ATR trade plan
    sentiment.py         FinBERT (optional) or VADER over Indian financial news
  data/
    feed.py              Fyers live data with yfinance fallback
    news.py              MoneyControl / ET / LiveMint RSS
  llm/
    advisor.py           Ollama tool-calling loop (read-only tools)
    tools.py             get_quote, get_technicals, get_sentiment, get_news,
                         get_risk_status, get_positions
  broker/
    paper.py             paper broker (default) — simulated fills, SL/target enforcement
    fyers.py             live broker — triple-gated (see Safety)
  alerts/notifier.py     Windows toasts + sounds + console
  journal.py             SQLite log of every signal/advice/fill → journal.db
dashboard/               FastAPI + websocket live UI
```

## Safety model (read this once)

Order flow: `strategy signal → LLM opinion → RiskGuardian → broker`.
The LLM's tools are **read-only**; there is no "place order" tool. Every order
must pass `RiskGuardian.check_entry()`, which enforces:

- **2% max daily drawdown** → kill-switch, done for the day
- **1% max risk per trade** (entry-to-SL distance × qty)
- **Mandatory stop-loss** on every order — no SL, no order
- Max 3 open positions, max 6 trades/day
- Cooldown after 2 consecutive losses (45 min)
- No new entries after 14:30, forced square-off at 15:12
- Minimum 1.5 reward:risk on any plan

Live trading additionally requires **both** `mode: live` in `config.yaml`
**and** `LIVE_TRADING_CONFIRMED=YES` in `.env`. Default is paper. Keep it on
paper until the journal shows the system is worth real money.

## Optional: better sentiment (FinBERT on your GPU)

```powershell
pip install -r requirements-finbert.txt
```

~2.5 GB download. Without it, an enhanced VADER (finance lexicon) is used —
lighter and instant.

## Tuning

Everything is in `config.yaml`: watchlist symbols, risk limits, scan interval,
signal threshold (raise to 0.65 for fewer/stronger alerts), candle timeframe.
Review `journal.db` weekly — which strategy's signals actually worked — and
adjust `WEIGHTS` in `sentinel/analysis/strategies.py` accordingly. That
feedback loop beats any fine-tuning.
