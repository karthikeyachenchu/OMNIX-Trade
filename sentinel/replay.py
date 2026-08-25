"""Replay a recorded session through the live dashboard.

The hackathon runs Sat–Sun 29–30 Aug 2026; NSE and BSE are shut all weekend,
so there are no live ticks during the demo. This module feeds a recorded
`engine.snapshot()` stream through the *same* websocket the live engine uses,
so the dashboard cannot tell the difference — except that every frame is
stamped `replay: true`, which the UI shows as a visible REPLAY badge.

Honesty rule: replayed data is always labelled as replayed. The badge is
added here, at the source, so no UI path can accidentally drop it.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("sentinel.replay")


def load_frames(path: str | Path) -> list[dict]:
    """Read a recorded session. Tolerates a truncated or still-open file.

    Recording flushes with Z_SYNC_FLUSH after every frame, so a session that
    was hard-killed (or is still being written right now) is readable up to
    its last complete line. A demo must never die because the capture was
    stopped with Ctrl+C, so a short read is normal, not an error.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"session file not found: {p}")

    frames: list[dict] = []
    try:
        with (gzip.open(p, "rt", encoding="utf-8") if p.suffix == ".gz"
              else p.open("r", encoding="utf-8")) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    frames.append(json.loads(line))
                except json.JSONDecodeError:
                    break          # partial trailing line — stop cleanly
    except (EOFError, OSError) as e:
        log.warning("session file ended early (%s) — using %d frames read so far",
                    type(e).__name__, len(frames))

    if not frames:
        raise ValueError(f"no usable frames in {p}")
    return frames


class ReplayEngine:
    """Proxy around a real engine that serves recorded snapshots.

    Everything except `snapshot()` is delegated to the underlying engine, so
    every dashboard endpoint, the risk view and the order store keep working.
    Only the live-data surface is swapped out.
    """

    def __init__(self, engine, frames: list[dict], speed: float = 1.0, loop: bool = True):
        self._engine = engine
        self._frames = frames
        self._speed = max(0.1, speed)
        self._loop = loop
        self._i = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Wall-clock gaps between recorded frames, so playback keeps the
        # original rhythm instead of flipping through frames as fast as it can.
        ts = [f.get("_rec_ts") for f in frames]
        self._gaps = [
            min(max((b - a), 0.05), 5.0) if (isinstance(a, (int, float))
                                             and isinstance(b, (int, float))) else 2.0
            for a, b in zip(ts, ts[1:], strict=False)
        ] or [2.0]

        span = (ts[-1] - ts[0]) if (ts and isinstance(ts[0], (int, float))
                                    and isinstance(ts[-1], (int, float))) else 0
        log.info("replay loaded: %d frames · %.1f min of session · %.1fx speed",
                 len(frames), span / 60, self._speed)

    # ── everything not overridden below belongs to the real engine ────────
    def __getattr__(self, name):
        return getattr(self._engine, name)

    @property
    def status(self) -> str:
        return "replay"

    def snapshot(self) -> dict:
        frame = dict(self._frames[self._i])
        frame.pop("_rec_ts", None)
        # Stamped at the source: no downstream path can lose the label.
        frame["replay"] = True
        frame["replay_pos"] = self._i + 1
        frame["replay_total"] = len(self._frames)
        frame["status"] = "replay"
        return frame

    def _run(self):
        while not self._stop.is_set():
            gap = self._gaps[min(self._i, len(self._gaps) - 1)] / self._speed
            if self._stop.wait(gap):
                return
            nxt = self._i + 1
            if nxt >= len(self._frames):
                if not self._loop:
                    log.info("replay finished — holding on the last frame")
                    return
                nxt = 0
            self._i = nxt

    def start(self):
        log.info("ReplayEngine started (live trading threads NOT started)")
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
