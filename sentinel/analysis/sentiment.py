"""Sentiment engine.

Tries FinBERT (ProsusAI/finbert) if transformers+torch are installed — best
accuracy on financial text, runs comfortably on the RTX 4060. Falls back to
VADER (instant, no downloads) otherwise. Both expose the same interface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..data.news import Headline

log = logging.getLogger("sentinel.sentiment")


@dataclass
class MarketSentiment:
    bias: float          # -1 (bearish) .. +1 (bullish)
    label: str           # BULLISH / BEARISH / NEUTRAL
    n_headlines: int
    engine: str
    top_positive: list[str]
    top_negative: list[str]


class _VaderBackend:
    name = "VADER"

    def __init__(self):
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        self._an = SentimentIntensityAnalyzer()
        # financial vocabulary VADER doesn't know
        self._an.lexicon.update({
            "bullish": 2.5, "bearish": -2.5, "rally": 1.8, "surge": 1.8, "soar": 2.0,
            "plunge": -2.2, "crash": -3.0, "slump": -1.8, "tank": -2.0, "tumble": -1.8,
            "upgrade": 1.5, "downgrade": -1.5, "outperform": 1.5, "underperform": -1.5,
            "beat": 1.2, "miss": -1.2, "profit": 1.0, "loss": -1.0, "record high": 2.0,
            "all-time high": 2.0, "52-week low": -1.8, "selloff": -2.0, "sell-off": -2.0,
            "gaining": 1.2, "sliding": -1.2, "recovery": 1.3, "slowdown": -1.3,
            "inflation": -0.8, "rate cut": 1.5, "rate hike": -1.2, "fii buying": 1.8,
            "fii selling": -1.8,
        })

    def score(self, text: str) -> float:
        return self._an.polarity_scores(text)["compound"]


class _FinBertBackend:
    name = "FinBERT"

    def __init__(self):
        # transformers >= 4.56 refuses to torch.load() a .bin checkpoint on
        # torch < 2.6 (CVE-2025-32434), and ProsusAI/finbert ships .bin only.
        # So on torch < 2.6 this path ALWAYS fails and always falls back to
        # VADER — after paying ~11s to import torch. Read the version from
        # package metadata instead, which costs microseconds and no import.
        from importlib.metadata import PackageNotFoundError, version
        try:
            raw = version("torch")
        except PackageNotFoundError as e:
            raise RuntimeError("torch not installed") from e
        if tuple(int(x) for x in raw.split("+")[0].split(".")[:2]) < (2, 6):
            raise RuntimeError(
                f"torch {raw} < 2.6 cannot load FinBERT's .bin weights "
                "(CVE-2025-32434); using VADER. Upgrade torch for FinBERT.")

        import torch
        from transformers import pipeline
        device = 0 if torch.cuda.is_available() else -1
        self._pipe = pipeline("sentiment-analysis", model="ProsusAI/finbert",
                              device=device, truncation=True)

    def score(self, text: str) -> float:
        r = self._pipe(text[:512])[0]
        sign = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}[r["label"]]
        return sign * r["score"]


class SentimentEngine:
    def __init__(self):
        self._backend = None

    def _ensure_backend(self):
        if self._backend is not None:
            return
        try:
            self._backend = _FinBertBackend()
            log.info("Sentiment backend: FinBERT")
        except Exception as e:
            self._backend = _VaderBackend()
            log.info("Sentiment backend: VADER (%s)", e)

    def analyze(self, headlines: list[Headline]) -> MarketSentiment:
        self._ensure_backend()
        if not headlines:
            return MarketSentiment(0.0, "NEUTRAL", 0, self._backend.name, [], [])
        scored = []
        for h in headlines:
            try:
                h.score = round(self._backend.score(h.title), 3)
            except Exception:
                h.score = 0.0
            h.sentiment = "positive" if h.score > 0.15 else ("negative" if h.score < -0.15 else "neutral")
            scored.append(h)
        avg = sum(h.score for h in scored) / len(scored)
        label = "BULLISH" if avg > 0.12 else ("BEARISH" if avg < -0.12 else "NEUTRAL")
        pos = sorted(scored, key=lambda h: -h.score)[:3]
        neg = sorted(scored, key=lambda h: h.score)[:3]
        return MarketSentiment(
            bias=round(max(-1.0, min(1.0, avg * 2)), 3),
            label=label, n_headlines=len(scored), engine=self._backend.name,
            top_positive=[h.title for h in pos if h.score > 0.15],
            top_negative=[h.title for h in neg if h.score < -0.15],
        )
