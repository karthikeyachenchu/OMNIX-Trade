"""Alerts: console (rich), Windows toast, and distinct sounds per severity."""

from __future__ import annotations

import logging
import threading

from rich.console import Console

log = logging.getLogger("sentinel.alerts")
console = Console()

_ENC = getattr(__import__("sys").stdout, "encoding", None) or "utf-8"


def _console_safe(text: str) -> str:
    """Drop characters the active console encoding can't render (₹, arrows, emoji)."""
    return text.encode(_ENC, errors="replace").decode(_ENC)

# ASCII tags for the console — legacy Windows consoles (cp1252) can't render emoji
_STYLES = {
    "signal": ("bold green", "[SIGNAL]"),
    "warning": ("bold yellow", "[WARN]"),
    "danger": ("bold red", "[STOP]"),
    "info": ("cyan", "[INFO]"),
    "trade": ("bold magenta", "[TRADE]"),
}

_TONES = {  # (frequency Hz, duration ms) sequences
    "signal": [(880, 150), (1175, 200)],
    "warning": [(600, 200), (600, 200)],
    "danger": [(400, 300), (300, 300), (250, 400)],
    "trade": [(1047, 120), (1319, 120), (1568, 180)],
    "info": [(750, 120)],
}


# ntfy.sh push: level → (priority, tags) so phone alerts carry urgency + icon
_NTFY = {
    "signal": ("high", "chart_with_upwards_trend"),
    "warning": ("high", "warning"),
    "danger": ("urgent", "rotating_light"),
    "trade": ("high", "money_with_wings"),
    "info": ("default", "robot"),
}


class Notifier:
    def __init__(self, desktop: bool = True, sound: bool = True, ntfy_topic: str = ""):
        self.desktop = desktop
        self.sound = sound
        self.ntfy_topic = ntfy_topic.strip()

    def notify(self, title: str, message: str, level: str = "info"):
        style, icon = _STYLES.get(level, _STYLES["info"])
        safe = _console_safe(f"{title} - {message}")
        console.print(f"{icon} [{style}]{safe}[/{style}]" if level != "info"
                      else f"{icon} [cyan]{safe}[/cyan]")
        if self.desktop:
            threading.Thread(target=self._toast, args=(title, message), daemon=True).start()
        if self.sound and level in _TONES:
            threading.Thread(target=self._beep, args=(level,), daemon=True).start()
        if self.ntfy_topic:
            threading.Thread(target=self._ntfy, args=(title, message, level), daemon=True).start()

    def _toast(self, title: str, message: str):
        try:
            from plyer import notification
            notification.notify(title=f"OMNIX-Trade: {title}"[:64],
                                message=message[:250], timeout=8)
        except Exception as e:
            log.debug("toast failed: %s", e)

    def _ntfy(self, title: str, message: str, level: str):
        try:
            import requests
            prio, tags = _NTFY.get(level, _NTFY["info"])
            requests.post(f"https://ntfy.sh/{self.ntfy_topic}",
                          data=message.encode("utf-8"),
                          headers={"Title": f"OMNIX-Trade: {title}".encode(),
                                   "Priority": prio, "Tags": tags},
                          timeout=6)
        except Exception as e:
            log.debug("ntfy push failed: %s", e)

    def _beep(self, level: str):
        try:
            import winsound
            for freq, dur in _TONES[level]:
                winsound.Beep(freq, dur)
        except Exception:
            pass
