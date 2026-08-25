"""Configuration loading: config.yaml + .env, merged into typed objects."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits. Frozen: nothing (including the LLM) can mutate these at runtime."""

    max_daily_drawdown_pct: float = 2.0
    max_risk_per_trade_pct: float = 1.0
    max_open_positions: int = 3
    max_trades_per_day: int = 6
    consecutive_losses_pause: int = 2
    cooldown_minutes: int = 45
    no_entry_after: str = "14:30"
    square_off_time: str = "15:12"
    min_reward_risk: float = 1.5


@dataclass
class WatchItem:
    name: str
    fyers: str
    yf: str
    options: bool = False
    lot_size: int = 1        # option lot size for options-enabled underlyings


@dataclass
class Settings:
    mode: str = "paper"
    data_source: str = "auto"
    capital: float = 100_000.0
    watchlist: list[WatchItem] = field(default_factory=list)
    # auto-invest: wallet deploys only on these watchlist names ([] = all)
    auto_invest_focus: list[str] = field(default_factory=list)
    risk: RiskLimits = field(default_factory=RiskLimits)
    # llm
    llm_model: str = "trade-sentinel"
    llm_fallback: str = "llama3.1:8b"
    max_tool_rounds: int = 4
    advise_on_signal: bool = True
    # engine
    scan_interval_sec: int = 30
    candle_interval: str = "5m"
    lookback_days: int = 5
    signal_threshold: float = 0.55
    sentiment_refresh_min: int = 10
    # execution realism (§7) — paper must never flatter itself
    paper_spread_pct: float = 0.002      # full bid-ask, half paid per leg
    slippage_pct: float = 0.001          # adverse fill on market orders
    cost_overrides: dict = field(default_factory=dict)   # config.yaml -> costs:
    # alerts
    alert_desktop: bool = True
    alert_sound: bool = True
    ntfy_topic: str = ""
    # dashboard
    dash_host: str = "127.0.0.1"
    dash_port: int = 8080
    auto_open_browser: bool = True
    dash_pin: str = ""
    # fyers (.env)
    fyers_client_id: str = ""
    fyers_secret_key: str = ""
    fyers_redirect_uri: str = ""
    live_trading_confirmed: bool = False
    # Bracket orders were restricted by Indian exchanges; INTRADAY is the
    # safe default. Verify against current Fyers v3 docs before changing.
    live_product_type: str = "INTRADAY"


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    cfg_path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    risk_raw = raw.get("risk", {})
    llm = raw.get("llm", {})
    eng = raw.get("engine", {})
    alerts = raw.get("alerts", {})
    dash = raw.get("dashboard", {})
    execu = raw.get("execution", {})

    settings = Settings(
        mode=raw.get("mode", "paper"),
        data_source=raw.get("data_source", "auto"),
        capital=float(raw.get("capital", 100_000)),
        watchlist=[WatchItem(**w) for w in raw.get("watchlist", [])],
        auto_invest_focus=[str(x) for x in ((raw.get("auto_invest") or {}).get("focus") or [])],
        risk=RiskLimits(**{k: risk_raw[k] for k in risk_raw if k in RiskLimits.__dataclass_fields__}),
        llm_model=llm.get("model", "trade-sentinel"),
        llm_fallback=llm.get("fallback_model", "llama3.1:8b"),
        max_tool_rounds=int(llm.get("max_tool_rounds", 4)),
        advise_on_signal=bool(llm.get("advise_on_signal", True)),
        scan_interval_sec=int(eng.get("scan_interval_sec", 30)),
        candle_interval=eng.get("candle_interval", "5m"),
        lookback_days=int(eng.get("lookback_days", 5)),
        signal_threshold=float(eng.get("signal_threshold", 0.55)),
        sentiment_refresh_min=int(eng.get("sentiment_refresh_min", 10)),
        paper_spread_pct=float(execu.get("paper_spread_pct", 0.002)),
        slippage_pct=float(execu.get("slippage_pct", 0.001)),
        cost_overrides=dict(raw.get("costs", {}) or {}),
        alert_desktop=bool(alerts.get("desktop", True)),
        alert_sound=bool(alerts.get("sound", True)),
        # Anyone who knows the ntfy topic can read your trade alerts and push
        # to your phone, so it behaves like a password. NTFY_TOPIC in .env
        # (git-ignored) wins over config.yaml (tracked) to keep it out of git.
        ntfy_topic=(os.getenv("NTFY_TOPIC", "").strip()
                    or str(alerts.get("ntfy_topic", "") or "").strip()),
        dash_host=dash.get("host", "127.0.0.1"),
        dash_port=int(dash.get("port", 8080)),
        auto_open_browser=bool(dash.get("auto_open_browser", True)),
        dash_pin=str(os.getenv("DASHBOARD_PIN", "")).strip(),
        fyers_client_id=os.getenv("FYERS_CLIENT_ID", "").strip(),
        fyers_secret_key=os.getenv("FYERS_SECRET_KEY", "").strip(),
        fyers_redirect_uri=os.getenv("FYERS_REDIRECT_URI", "").strip(),
        live_trading_confirmed=os.getenv("LIVE_TRADING_CONFIRMED", "NO").strip() == "YES",
        live_product_type=str(raw.get("live_product_type", "INTRADAY")).strip().upper(),
    )
    validate(settings)
    return settings


class ConfigError(ValueError):
    """Configuration that would be unsafe to trade with. Startup must fail."""


def validate(s: Settings) -> Settings:
    """Reject configurations that are dangerous rather than merely wrong (§69).

    A risk limit that is silently nonsense is worse than a crash: the system
    would run all day believing it was protected. Every check below caused a
    real class of bug, so each one refuses to start rather than warn.
    """
    errors: list[str] = []
    r = s.risk

    if not (0 < r.max_daily_drawdown_pct <= 100):
        errors.append(f"risk.max_daily_drawdown_pct must be in (0, 100], got "
                      f"{r.max_daily_drawdown_pct}")
    if not (0 < r.max_risk_per_trade_pct <= 100):
        errors.append(f"risk.max_risk_per_trade_pct must be in (0, 100], got "
                      f"{r.max_risk_per_trade_pct}")
    if r.max_risk_per_trade_pct > r.max_daily_drawdown_pct:
        errors.append(
            f"risk.max_risk_per_trade_pct ({r.max_risk_per_trade_pct}%) exceeds "
            f"risk.max_daily_drawdown_pct ({r.max_daily_drawdown_pct}%) — a single "
            f"losing trade would breach the daily limit, making the kill switch "
            f"pointless")
    if r.max_risk_per_trade_pct >= 20:
        errors.append(
            f"risk.max_risk_per_trade_pct is {r.max_risk_per_trade_pct}% — refusing "
            f"to start. If this is meant to be a fraction, use percent units: "
            f"1.0 means 1%, not 100%")
    if r.max_open_positions < 1:
        errors.append(f"risk.max_open_positions must be >= 1, got {r.max_open_positions}")
    if r.max_trades_per_day < 1:
        errors.append(f"risk.max_trades_per_day must be >= 1, got {r.max_trades_per_day}")
    if r.min_reward_risk <= 0:
        errors.append(f"risk.min_reward_risk must be positive, got {r.min_reward_risk}")
    if r.cooldown_minutes < 0:
        errors.append("risk.cooldown_minutes must be >= 0")

    from .clock import parse_hhmm
    try:
        no_entry = parse_hhmm(r.no_entry_after)
        square = parse_hhmm(r.square_off_time)
        if no_entry >= square:
            errors.append(
                f"risk.no_entry_after ({r.no_entry_after}) must be earlier than "
                f"risk.square_off_time ({r.square_off_time}) — otherwise a position "
                f"can be opened after the square-off deadline")
    except ValueError as e:
        errors.append(f"risk time value: {e}")

    if s.capital <= 0:
        errors.append(f"capital must be positive, got {s.capital}")
    if not (0 <= s.signal_threshold <= 1):
        errors.append(f"engine.signal_threshold must be in [0, 1], got {s.signal_threshold}")
    if s.scan_interval_sec < 1:
        errors.append("engine.scan_interval_sec must be >= 1")
    if not (0 <= s.paper_spread_pct < 1):
        errors.append("execution.paper_spread_pct must be in [0, 1)")
    if not (0 <= s.slippage_pct < 1):
        errors.append("execution.slippage_pct must be in [0, 1)")
    if s.mode not in ("paper", "live"):
        errors.append(f"mode must be 'paper' or 'live', got {s.mode!r}")

    # ── D3: never bind a money-moving dashboard to the world unauthenticated ──
    if s.dash_host not in ("127.0.0.1", "localhost", "::1") and not s.dash_pin:
        errors.append(
            f"dashboard.host is {s.dash_host!r} (non-loopback) but no DASHBOARD_PIN "
            f"is set in .env. The dashboard exposes money-moving endpoints "
            f"(/api/wallet, /api/capital/topup, /api/track). Either set "
            f"dashboard.host: 127.0.0.1, or set a DASHBOARD_PIN.")

    dupes = {w.name for w in s.watchlist if
             sum(1 for x in s.watchlist if x.name == w.name) > 1}
    if dupes:
        errors.append(f"duplicate watchlist names: {sorted(dupes)}")
    for w in s.watchlist:
        if w.lot_size < 1:
            errors.append(f"watchlist {w.name}: lot_size must be >= 1, got {w.lot_size}")

    unknown_focus = set(s.auto_invest_focus) - {w.name for w in s.watchlist}
    if unknown_focus:
        errors.append(f"auto_invest.focus names not in the watchlist: {sorted(unknown_focus)}")

    if errors:
        raise ConfigError(
            "Refusing to start — unsafe configuration:\n  - " + "\n  - ".join(errors))
    return s
