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


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(ROOT / ".env")
    cfg_path = path or ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    risk_raw = raw.get("risk", {})
    llm = raw.get("llm", {})
    eng = raw.get("engine", {})
    alerts = raw.get("alerts", {})
    dash = raw.get("dashboard", {})

    return Settings(
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
        alert_desktop=bool(alerts.get("desktop", True)),
        alert_sound=bool(alerts.get("sound", True)),
        ntfy_topic=str(alerts.get("ntfy_topic", "") or "").strip(),
        dash_host=dash.get("host", "127.0.0.1"),
        dash_port=int(dash.get("port", 8080)),
        auto_open_browser=bool(dash.get("auto_open_browser", True)),
        dash_pin=str(os.getenv("DASHBOARD_PIN", "")).strip(),
        fyers_client_id=os.getenv("FYERS_CLIENT_ID", "").strip(),
        fyers_secret_key=os.getenv("FYERS_SECRET_KEY", "").strip(),
        fyers_redirect_uri=os.getenv("FYERS_REDIRECT_URI", "").strip(),
        live_trading_confirmed=os.getenv("LIVE_TRADING_CONFIRMED", "NO").strip() == "YES",
    )
