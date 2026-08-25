"""FastAPI dashboard — live state over websocket + advisor chat endpoint."""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
import socket
import time
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

log = logging.getLogger("sentinel.dashboard")
STATIC = Path(__file__).parent / "static"

_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def json_safe(obj):
    """Strip non-finite floats out of a payload.

    Python's json.dumps happily writes bare NaN / Infinity, which are NOT
    valid JSON — JSON.parse() in the browser throws on them and the client
    silently drops the whole message. Indicators legitimately go NaN
    (VWAP before the first intraday bar, ATR during warm-up, any indicator
    when the market is shut), so this has to be handled, not avoided.
    Non-finite becomes null, which every consumer already treats as "no value".
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def lan_ip() -> str:
    """This machine's LAN address (what the phone should dial)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class ChatMessage(BaseModel):
    message: str


class AllocateRequest(BaseModel):
    amount: float


class TrackRequest(BaseModel):
    symbol: str
    name: str
    side: str
    qty: int
    entry: float
    stop_loss: float
    target: float | None = None


class CloseRequest(BaseModel):
    id: int
    exit_price: float


class StopRequest(BaseModel):
    id: int
    stop_loss: float


class KillRequest(BaseModel):
    reason: str = ""


class KillResetRequest(BaseModel):
    confirmation: str


def create_app(engine) -> FastAPI:
    app = FastAPI(title="OMNIX-Trade")

    @app.exception_handler(Exception)
    async def _json_errors(request: Request, exc: Exception):
        """Last-resort net: an unhandled error returns JSON, never an HTML 500.

        Every fetch() in the dashboard does response.json(). FastAPI's default
        500 body is plain text, so an unhandled backend error surfaced in the
        UI as an unrelated JSON parse error and hid what actually broke.
        """
        log.error("unhandled error on %s %s", request.method, request.url.path,
                  exc_info=exc)
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}", "path": request.url.path},
            status_code=500)

    pin = engine.s.dash_pin
    # Defect D3: this dashboard exposes money-moving POST endpoints. It used to
    # bind 0.0.0.0 with the PIN check disabled (DASHBOARD_PIN was never set),
    # so anyone on the Wi-Fi could start/stop the wallet. Config validation now
    # refuses a non-loopback bind without a PIN; this gate does the rest.
    failures: dict[str, list[float]] = defaultdict(list)

    def _rate_limited(host: str) -> bool:
        """Crude but effective: 10 bad PINs per 5 minutes per host."""
        now = time.time()
        recent = [t for t in failures[host] if now - t < 300]
        failures[host] = recent
        return len(recent) >= 10

    @app.middleware("http")
    async def pin_gate(request: Request, call_next):
        """PIN for non-loopback clients. Localhost is always allowed."""
        host = request.client.host if request.client else ""
        if not pin or host in _LOCAL:
            return await call_next(request)

        if _rate_limited(host):
            log.warning("rate-limited PIN attempts from %s", host)
            return JSONResponse({"error": "too many attempts, try again later"},
                                status_code=429)

        supplied = (request.query_params.get("pin")
                    or request.cookies.get("omnix_pin", ""))
        # Constant-time comparison — a naive == leaks the PIN one byte at a
        # time to anyone who can measure response latency.
        if not (supplied and secrets.compare_digest(supplied, pin)):
            failures[host].append(time.time())
            return HTMLResponse(
                "<body style='background:#08080a;color:#eee;font-family:sans-serif;"
                "display:grid;place-items:center;height:100vh'><form>"
                "<h2>OMNIX-Trade</h2><input name='pin' type='password' placeholder='PIN'"
                " style='padding:10px;border-radius:8px;border:0'>"
                "<button style='padding:10px'>Enter</button></form></body>",
                status_code=401)

        response = await call_next(request)
        response.set_cookie("omnix_pin", supplied, max_age=86400 * 30,
                            httponly=True, samesite="strict")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/manifest.json")
    async def manifest():
        return JSONResponse({
            "name": "OMNIX-Trade", "short_name": "OMNIX-Trade",
            "start_url": "/", "display": "standalone",
            "background_color": "#08080a", "theme_color": "#08080a",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml",
                       "purpose": "any"}],
        })

    @app.get("/sw.js")
    async def sw():
        return HTMLResponse("self.addEventListener('fetch', () => {});",
                            media_type="application/javascript")

    @app.get("/icon.svg")
    async def icon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
               '<rect width="512" height="512" rx="96" fill="#08080a"/>'
               '<g transform="translate(256 236)" fill="none" stroke="#c8b6ff" '
               'stroke-width="30" stroke-linecap="round">'
               '<path d="M -110 0 C -110 -80 -37 -80 0 0 C 37 80 110 80 110 0 '
               'C 110 -80 37 -80 0 0 C -37 80 -110 80 -110 0 Z"/></g>'
               '<text x="256" y="420" text-anchor="middle" fill="#f4f2ff" '
               'font-family="sans-serif" font-size="72" font-weight="700" '
               'letter-spacing="8">OMNIX</text></svg>')
        return HTMLResponse(svg, media_type="image/svg+xml")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        host = websocket.client.host if websocket.client else ""
        if pin and host not in _LOCAL and websocket.cookies.get("omnix_pin", "") != pin:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                # 4 Hz push so charts/numbers track the live socket ticks with
                # no perceptible lag (ticks themselves arrive in <0.4s).
                await websocket.send_json(json_safe(engine.snapshot()))
                await asyncio.sleep(0.25)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.debug("ws closed: %s", e)

    def _advisor_offline(exc: Exception) -> dict:
        """The advisor is optional; the rest of the product is not.

        `engine.advisor is None` is NOT a sufficient guard: the Advisor object
        constructs fine against a fallback model and only fails later, inside
        ollama.chat(), when the daemon is not running. That raised a bare
        ConnectionError out of the handler and the browser got a 500 with an
        HTML body, which the frontend cannot parse — so the Assistant tab and
        every "Why this? / Ask advisor" button appeared broken rather than
        merely unavailable. Degrade, never 500.
        """
        log.warning("advisor call failed: %s: %s", type(exc).__name__, exc)
        return {"reply": "⚠ Advisor offline — start Ollama (`ollama serve`) to enable "
                         "the AI assistant. Signals, risk limits and alerts are "
                         "unaffected.",
                "advisor_offline": True}

    @app.post("/api/chat")
    async def chat(msg: ChatMessage):
        if engine.advisor is None:
            return _advisor_offline(RuntimeError("advisor not configured"))
        try:
            reply = await asyncio.to_thread(engine.advisor.chat, msg.message.strip())
        except Exception as e:
            return _advisor_offline(e)
        return {"reply": reply}

    @app.post("/api/briefing")
    async def briefing():
        if engine.advisor is None:
            return _advisor_offline(RuntimeError("advisor not configured"))
        try:
            reply = await asyncio.to_thread(engine.advisor.morning_briefing)
        except Exception as e:
            return _advisor_offline(e)
        return {"reply": reply}

    @app.get("/api/journal")
    async def journal(limit: int = 50):
        return {"events": engine.journal.recent(limit=limit)}

    # ── capital allocator + tracked-trade assistant ───────────────────────
    @app.post("/api/allocate")
    async def allocate(req: AllocateRequest):
        return engine.allocate(req.amount)

    @app.post("/api/track")
    async def track(req: TrackRequest):
        t = engine.tracker.add(req.symbol, req.name, req.side, req.qty,
                               req.entry, req.stop_loss, req.target)
        engine.journal.log("tracked", t.name, t.side, 0.0, trade_id=t.id,
                           qty=t.qty, entry=t.entry, sl=t.stop_loss, target=t.target)
        return {"ok": True, "id": t.id}

    @app.post("/api/track/close")
    async def track_close(req: CloseRequest):
        t = engine.tracker.close(req.id, req.exit_price)
        if t is None:
            return {"ok": False, "error": "trade not found"}
        engine.journal.log("tracked_close", t.name, t.side, 0.0, trade_id=t.id,
                           exit_price=t.exit_price, pnl=t.pnl)
        if (t.pnl or 0) < 0:   # closed at a loss → keep watching for the V-recovery
            engine.watch_reentry_for(t.symbol, t.side, req.exit_price)
        return {"ok": True, "pnl": t.pnl}

    @app.post("/api/track/stop")
    async def track_stop(req: StopRequest):
        return {"ok": engine.tracker.update_stop(req.id, req.stop_loss)}

    # ── paper-trading capital top-up ──────────────────────────────────────
    @app.post("/api/capital/topup")
    async def capital_topup(req: AllocateRequest):
        return engine.top_up_capital(req.amount)

    # ── auto-invest wallet ────────────────────────────────────────────────
    @app.post("/api/wallet")
    async def wallet_start(req: AllocateRequest):
        return engine.activate_wallet(req.amount)

    @app.post("/api/wallet/stop")
    async def wallet_stop():
        return engine.stop_wallet()

    @app.post("/api/wallet/topup")
    async def wallet_topup(req: AllocateRequest):
        return engine.topup_wallet(req.amount)

    @app.get("/api/wallet/ledger")
    async def wallet_ledger(limit: int = 50):
        return {"ledger": engine.wallet.ledger(limit)}

    # ── options chain ─────────────────────────────────────────────────────
    @app.get("/api/watchlist")
    async def watchlist():
        return {"items": [{"fyers": w.fyers, "name": w.name, "options": w.options,
                           "lot_size": w.lot_size} for w in engine.s.watchlist]}

    @app.get("/api/optionchain")
    async def optionchain(symbol: str = "NSE:NIFTY50-INDEX", count: int = 8):
        chain_fn = getattr(engine.feed, "option_chain", None)
        try:
            raw = chain_fn(symbol, count) if chain_fn else {}
        except Exception as e:
            # Expired token / rate limit / no Fyers session. The Options tab
            # must show a message, not a dead 500.
            log.warning("option chain failed for %s: %s", symbol, e)
            raw = {}
        if not raw:
            return {"error": "Option chain unavailable (needs live Fyers data)"}
        rows = raw.get("optionsChain", [])
        spot = next((r.get("ltp") for r in rows
                     if r.get("option_type") not in ("CE", "PE") and r.get("ltp")), None)
        by_strike: dict[float, dict] = {}
        for r in rows:
            ot = r.get("option_type")
            if ot not in ("CE", "PE") or r.get("strike_price", 0) <= 0:
                continue
            d = by_strike.setdefault(r["strike_price"], {"strike": r["strike_price"]})
            d[ot.lower()] = {"symbol": r.get("symbol", ""), "ltp": r.get("ltp", 0),
                             "oi": r.get("oi", 0), "volume": r.get("volume", 0),
                             "ltpch": r.get("ltpchp", 0)}
        item = next((w for w in engine.s.watchlist if w.fyers == symbol), None)
        call_oi, put_oi = raw.get("callOi", 0), raw.get("putOi", 0)
        return {
            "symbol": symbol, "name": item.name if item else symbol, "spot": spot,
            "expiry": [e.get("date") for e in (raw.get("expiryData") or [])][:4],
            "lot_size": item.lot_size if item else 1,
            "call_oi": call_oi, "put_oi": put_oi,
            "pcr": round(put_oi / call_oi, 2) if call_oi else None,
            "strikes": sorted(by_strike.values(), key=lambda d: d["strike"]),
        }

    # ── price history for charts (real Fyers candles) ─────────────────────
    @app.get("/api/history")
    async def history(symbol: str = "NSE:NIFTY50-INDEX", interval: str = "5m", days: int = 2):
        def _fetch():
            try:
                return engine.feed.candles(symbol, interval, days)
            except Exception as e:
                log.debug("history fetch failed: %s", e)
                return None
        df = await asyncio.to_thread(_fetch)
        if df is None or df.empty:
            return {"symbol": symbol, "candles": [], "error": "No history available"}
        candles = [{
            "t": int(ts.timestamp()),
            "o": round(float(r.open), 2), "h": round(float(r.high), 2),
            "l": round(float(r.low), 2), "c": round(float(r.close), 2),
            "v": int(r.volume) if r.volume == r.volume else 0,
        } for ts, r in df.iterrows()]
        live = engine.ticks.ltp(symbol) if engine.ticks else None
        return {"symbol": symbol, "interval": interval, "candles": candles, "live": live}

    # ── risk / kill switch (§5) ───────────────────────────────────────────
    @app.get("/api/risk")
    async def risk_state():
        return engine.guardian.snapshot()

    @app.post("/api/risk/kill")
    async def kill(req: KillRequest):
        """Manual kill switch. Anyone can trip it; only 'RESET' clears it."""
        engine.guardian.trip_kill_switch(req.reason or "manually tripped from dashboard")
        return engine.guardian.snapshot()

    @app.post("/api/risk/reset")
    async def kill_reset(req: KillResetRequest):
        ok, msg = engine.guardian.reset_kill_switch(req.confirmation)
        return {"ok": ok, "message": msg, "risk": engine.guardian.snapshot()}

    @app.get("/api/orders")
    async def orders(limit: int = 50):
        """Full order history — every intent, its risk decision, and its fate."""
        return {"orders": engine.order_store.recent(limit)}

    # ── phone connect info ────────────────────────────────────────────────
    @app.get("/api/phone")
    async def phone():
        url = f"http://{lan_ip()}:{engine.s.dash_port}"
        qr = None
        try:
            import segno
            qr = segno.make(url).svg_data_uri(scale=5, dark="#c8b6ff", light=None)
        except Exception:
            pass
        return {"url": url, "qr": qr, "ntfy_topic": engine.s.ntfy_topic,
                "pin_protected": bool(pin)}

    return app
