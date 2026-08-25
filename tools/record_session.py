"""Record a live trading session to JSONL for hackathon replay.

Attaches to the running dashboard's websocket and appends every snapshot to
a file. Deliberately a *separate process* from the engine: recording must
never be able to slow down or crash live trading.

    python tools/record_session.py                       # -> sessions/<date>.jsonl
    python tools/record_session.py --out sessions/x.jsonl --every 2

Replay it with:  python main.py --replay sessions/<date>.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import io
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _open_append(out: Path) -> io.TextIOBase:
    """Append handle; transparently gzipped when the name ends .gz.

    A full 6-hour session is ~135 MB of raw JSONL — over GitHub's 100 MB file
    limit and painful to move around. The frames are near-identical from one
    tick to the next, so gzip takes it to roughly 8 MB at no fidelity cost.
    """
    if out.suffix == ".gz":
        return gzip.open(out, "at", encoding="utf-8", compresslevel=6)
    return out.open("a", encoding="utf-8")


async def record(url: str, out: Path, every: float, quiet: bool) -> int:
    import websockets

    out.parent.mkdir(parents=True, exist_ok=True)
    frames = 0
    started = time.time()
    stop = asyncio.Event()

    def _sigint(*_):
        stop.set()

    try:
        signal.signal(signal.SIGINT, _sigint)
    except ValueError:
        pass  # not on the main thread

    # Reconnect forever: a 6-hour capture must survive a dropped socket.
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
                if not quiet:
                    print(f"[rec] connected {url}", flush=True)
                last_write = 0.0
                with _open_append(out) as fh:
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                        now = time.time()
                        if now - last_write < every:
                            continue
                        last_write = now
                        try:
                            snap = json.loads(raw)
                        except json.JSONDecodeError:
                            # A frame the browser could not parse either.
                            # Worth knowing about, never worth dying over.
                            print("[rec] WARN unparseable frame skipped", flush=True)
                            continue
                        snap["_rec_ts"] = now
                        fh.write(json.dumps(snap, separators=(",", ":")) + "\n")
                        fh.flush()          # survive a hard kill
                        frames += 1
                        if not quiet and frames % 50 == 0:
                            mins = (now - started) / 60
                            mb = out.stat().st_size / 1e6
                            print(f"[rec] {frames} frames · {mins:.1f} min · "
                                  f"{mb:.1f} MB · last_scan={snap.get('last_scan')}",
                                  flush=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if stop.is_set():
                break
            print(f"[rec] disconnected ({type(e).__name__}: {e}) — retrying in 3s",
                  flush=True)
            await asyncio.sleep(3)

    print(f"[rec] stopped · {frames} frames · {out}", flush=True)
    return frames


def main():
    ap = argparse.ArgumentParser(description="Record a live session for replay")
    ap.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    ap.add_argument("--out", default="")
    ap.add_argument("--every", type=float, default=2.0,
                    help="seconds between recorded frames (default 2)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    out = (Path(a.out) if a.out
           else ROOT / "sessions" / f"{datetime.now():%Y-%m-%d}.jsonl.gz")
    print(f"[rec] recording to {out} (one frame every {a.every}s) — Ctrl+C to stop",
          flush=True)
    try:
        asyncio.run(record(a.url, out, a.every, a.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
