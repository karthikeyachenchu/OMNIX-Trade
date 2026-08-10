"""Trade Sentinel — single entry point.

  python main.py            start engine + dashboard (paper mode by default)
  python main.py --login    daily Fyers login (run each morning once you have the API key)
  python main.py --once     one scan cycle, print results, exit (smoke test)
  python main.py --no-dashboard   engine + console alerts only
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

from rich.console import Console
from rich.logging import RichHandler

console = Console()


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )
    for noisy in ("urllib3", "httpx", "yfinance", "peewee", "watchfiles", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main():
    ap = argparse.ArgumentParser(description="OMNIX-Trade — real-time trading advisor")
    ap.add_argument("--login", action="store_true", help="run the daily Fyers login flow")
    ap.add_argument("--once", action="store_true", help="single scan cycle then exit")
    ap.add_argument("--no-dashboard", action="store_true", help="skip the web dashboard")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    setup_logging(args.verbose)

    from sentinel.config import load_settings
    settings = load_settings()

    # ── Fyers session (only if credentials are present) ───────────────────
    fyers_session = None
    if settings.fyers_client_id and settings.fyers_secret_key:
        from sentinel.broker.fyers import FyersSession
        fyers_session = FyersSession(settings)

    if args.login:
        if not fyers_session:
            console.print("[red]Add FYERS_CLIENT_ID and FYERS_SECRET_KEY to .env first "
                          "(copy .env.example to .env).[/red]")
            sys.exit(1)
        sys.exit(0 if fyers_session.login_interactive() else 1)

    # ── engine ────────────────────────────────────────────────────────────
    from sentinel.engine import TradingEngine
    from sentinel.llm.advisor import Advisor
    from sentinel.llm.tools import ToolExecutor

    engine = TradingEngine(settings, fyers_session)
    try:
        engine.advisor = Advisor(settings, ToolExecutor(engine))
        console.print(f"[cyan]LLM advisor ready: {engine.advisor.model}[/cyan]")
    except Exception as e:
        console.print(f"[yellow]LLM advisor unavailable ({e}) — signals & alerts still work.[/yellow]")

    if args.once:
        console.print("[bold]Running one scan cycle...[/bold]")
        engine._scan()
        for name, comp in engine.latest_signals.items():
            console.print(f"\n[bold]{name}[/bold]  {comp.label()} ({comp.score:.0%})  "
                          f"LTP {comp.snapshot['ltp']}")
            for v in comp.votes:
                arrow = "+" if v.direction > 0 else ("-" if v.direction < 0 else ".")
                console.print(f"   {arrow} {v.strategy:<12} {v.reason}")
            if comp.plan:
                console.print(f"   plan: {comp.plan}")
        if engine.market_sentiment:
            ms = engine.market_sentiment
            console.print(f"\n[bold]Sentiment[/bold]: {ms.label} ({ms.bias:+.2f}) "
                          f"from {ms.n_headlines} headlines via {ms.engine}")
        return

    engine.start()

    if args.no_dashboard:
        console.print("[green]Engine running (console mode). Ctrl+C to stop.[/green]")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            engine.stop()
        return

    import uvicorn

    from dashboard.server import create_app

    app = create_app(engine)
    local_host = "127.0.0.1" if settings.dash_host == "0.0.0.0" else settings.dash_host
    url = f"http://{local_host}:{settings.dash_port}"
    console.print(f"[bold green]Dashboard: {url}[/bold green]")
    if settings.auto_open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=settings.dash_host, port=settings.dash_port, log_level="warning")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
